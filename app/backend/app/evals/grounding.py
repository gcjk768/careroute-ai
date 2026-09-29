"""[LLMSecOps] E15 — GROUNDEDNESS: term overlap, and the judge it cannot replace.

The decks' LLM output-quality gate (hallucination / relevance / groundedness,
DeepEval or LLM-as-judge) and the LLMSecOps faithfulness row (>= 0.85) are the
same missing measurement. CareRoute already checks faithfulness twice — E1
scores invented symptom keywords, E8 scores planted decoy terms — and both are
TERM-OVERLAP checks: they ask whether the answer contains vocabulary the input
did not.

That catches an answer that invents a fact. It cannot catch an answer that
reuses the context's own words to say something the context did not: a flipped
negation, a changed number, a fact moved from one person to another. Every word
is present, and every term-overlap scorer on earth calls it grounded.

So this module measures BOTH layers against the same labelled corpus:

  overlap    deterministic, runs everywhere including CI. Unsupported-claim
             rate over content terms, plus a verdict.
  judge      an LLM grading (context, answer) pairs for faithfulness. Routed as
             `eval.grounding` — deep tier, cacheable, because a judge that
             disagrees with itself cannot grade anything. Returns None when no
             provider is reachable, and the evaluation reports SKIPPED rather
             than passing vacuously.

The corpus is built to expose both failure directions:

  verbatim / paraphrase   faithful. Paraphrases use clinical synonyms, so they
                          measure the overlap scorer's FALSE POSITIVES — the
                          direction a "does every word appear?" check gets wrong
                          on any real clinical summary.
  invented                unfaithful with new vocabulary. Overlap should catch these.
  semantic                unfaithful in the context's own words. Overlap cannot
                          catch these, and reporting that it does would be the
                          lie this evaluation exists to prevent.

Run it:  python -m app.evals.grounding
"""
from __future__ import annotations

import json
import os
import re
import sys

_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tests", "fixtures", "grounding_cases.json",
)

_WORD = re.compile(r"[a-z0-9]+")

#: Words that carry no clinical claim. Narrative scaffolding a faithful summary
#: is free to introduce ("patient reports", "per guidance") plus ordinary
#: English function words. Kept deliberately small: every word added here is a
#: word the scorer stops checking, and a scorer with a large allowlist scores
#: nothing.
_NON_CLAIM = frozenset((
    # function words
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "that", "this", "these", "those",
    "with", "without", "within", "of", "in", "on", "at", "for", "from", "to", "by", "as", "per",
    "via", "so", "such", "very", "more", "most", "less", "least", "also", "too", "only", "just",
    "about", "over", "under", "after", "before", "during", "while", "when", "where", "which",
    "who", "whom", "whose", "what", "how", "why",
    # pronouns and auxiliaries. NOTE: the negations "not", "no", "nor" are here
    # because they are not vocabulary an answer can invent — and that is exactly
    # why a flipped negation is invisible to this scorer. See the `semantic` family.
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did", "has", "have",
    "had", "having", "it", "its", "their", "his", "her", "they", "he", "she", "we", "you", "i",
    "me", "my", "our", "your", "not", "no", "nor",
    # narrative scaffolding a faithful summary is free to introduce
    "patient", "patients", "reports", "report", "reported", "reporting", "presents", "presenting",
    "presented", "describes", "described", "describing", "states", "stated", "notes", "noted",
    "history", "summary", "assessment", "triage", "case", "guidance", "indicates", "indicated",
    "suggests", "suggested", "warrants", "warranted", "appropriate", "recommend", "recommended",
    "referral", "refer", "referred", "review", "advise", "advised", "action", "plan", "clinical",
    "clinician", "nurse", "doctor",
))


def load_cases(path: str = _FIXTURE) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def claim_terms(text: str) -> set[str]:
    """Content terms an answer asserts. Numbers count — a changed dose or
    temperature is a claim, and dropping digits would hide a whole family."""
    return {w for w in _WORD.findall((text or "").lower())
            if len(w) > 2 and w not in _NON_CLAIM} | {
        w for w in _WORD.findall((text or "").lower()) if w.isdigit()
    }


def overlap_score(answer: str, context: str) -> dict:
    """Fraction of the answer's claim terms that appear in the context.

    1.0 does NOT mean faithful. It means nothing was said in words the context
    did not already contain, which is a necessary condition and nothing more.
    """
    claims = claim_terms(answer)
    if not claims:
        # An answer that asserts nothing cannot be unfaithful and is also
        # useless; E8 already gates emptiness, so this reports rather than judges.
        return {"score": 1.0, "unsupported": [], "claims": 0}
    supported = claim_terms(context)
    unsupported = sorted(claims - supported)
    return {
        "score": round((len(claims) - len(unsupported)) / len(claims), 4),
        "unsupported": unsupported,
        "claims": len(claims),
    }


def overlap_verdict(answer: str, context: str, *, tolerance: float = 1.0) -> str:
    """`faithful` / `unfaithful` from term overlap alone.

    `tolerance` is the fraction of claim terms that must be supported. It
    defaults to 1.0 — every claim term present in the context — because any
    lower bar is a licence for exactly one invented fact per answer, and one
    invented fact reaching a clinician is the failure being guarded.
    """
    return "faithful" if overlap_score(answer, context)["score"] >= tolerance else "unfaithful"


JUDGE_SYSTEM = (
    "You grade whether a clinical summary is FAITHFUL to the context it was written from. "
    "Faithful means every claim in the summary is supported by the context: no invented facts, "
    "no changed numbers, no flipped negations, no fact attributed to the wrong person, and "
    "nothing from retrieved guidance restated as if it were a finding about this patient. "
    "Rewording, clinical synonyms and summarising are faithful. Judge only faithfulness to the "
    "context — not whether the clinical advice is correct."
)

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "faithful": {"type": "boolean"},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["faithful", "reason"],
}


async def judge(answer: str, context: str, complete=None) -> dict | None:
    """LLM-as-judge verdict, or None when no provider is reachable.

    None is not a pass. Callers report it as SKIPPED — an unreachable judge and
    a judge that found nothing are the same empty result, and only one of them
    is evidence.
    """
    from .. import llm

    complete = complete or llm.complete
    prompt = (
        f"CONTEXT (everything the summary was allowed to use):\n{context}\n\n"
        f"SUMMARY:\n{answer}\n\n"
        'Return JSON: {"faithful": true|false, "unsupported_claims": [...], "reason": "one sentence"}'
    )
    try:
        raw = await complete(JUDGE_SYSTEM, prompt, json_mode=True, task="eval.grounding")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - no provider, or an unparseable verdict; both mean "not judged"
        return None
    if not isinstance(data, dict) or not isinstance(data.get("faithful"), bool):
        return None
    return {
        "faithful": data["faithful"],
        "unsupported_claims": [str(c)[:120] for c in (data.get("unsupported_claims") or [])][:10],
        "reason": str(data.get("reason", ""))[:300],
    }


def evaluate_overlap(cases: list[dict] | None = None, *, tolerance: float = 1.0) -> dict:
    """Score the deterministic layer, per family and overall."""
    cases = cases if cases is not None else load_cases()
    rows = []
    for case in cases:
        detail = overlap_score(case["answer"], case["context"])
        verdict = "faithful" if detail["score"] >= tolerance else "unfaithful"
        rows.append({
            "id": case["id"], "family": case["family"], "label": case["label"],
            "verdict": verdict, "correct": verdict == case["label"],
            "score": detail["score"], "unsupported": detail["unsupported"],
        })

    unfaithful = [r for r in rows if r["label"] == "unfaithful"]
    faithful = [r for r in rows if r["label"] == "faithful"]

    def _rate(selected, predicate):
        return round(sum(1 for r in selected if predicate(r)) / len(selected), 4) if selected else 0.0

    return {
        "n": len(rows),
        "tolerance": tolerance,
        "accuracy": _rate(rows, lambda r: r["correct"]),
        "detection_rate": _rate(unfaithful, lambda r: r["verdict"] == "unfaithful"),
        "false_positive_rate": _rate(faithful, lambda r: r["verdict"] == "unfaithful"),
        "by_family": {
            f: {"n": sum(1 for r in rows if r["family"] == f),
                "accuracy": _rate([r for r in rows if r["family"] == f], lambda r: r["correct"])}
            for f in sorted({r["family"] for r in rows})
        },
        "rows": rows,
    }


def sweep_tolerance(
    tolerances: tuple[float, ...] = (1.0, 0.95, 0.9, 0.85, 0.8, 0.7, 0.6, 0.5, 0.4),
    cases: list[dict] | None = None,
) -> list[dict]:
    """Accuracy / detection / false-positives against the tolerance.

    The point of publishing the whole curve rather than one number: no setting
    of it produces a usable gate. Measured 2026-09-17 — best accuracy 0.625 at
    0.85-0.90, still rejecting 43% of faithful summaries. Same shape as the
    DENSE_FLOOR finding in rag.py: when no threshold separates the classes, the
    honest move is to say so rather than to pick the least embarrassing one.
    """
    return [
        {k: v for k, v in evaluate_overlap(cases, tolerance=t).items() if k != "rows"}
        for t in tolerances
    ]


async def evaluate_judge_async(cases: list[dict] | None = None, complete=None) -> dict:
    """Score the LLM judge on the same corpus, and its agreement with overlap."""
    cases = cases if cases is not None else load_cases()
    rows, skipped = [], 0
    for case in cases:
        verdict = await judge(case["answer"], case["context"], complete)
        if verdict is None:
            skipped += 1
            continue
        judged = "faithful" if verdict["faithful"] else "unfaithful"
        rows.append({
            "id": case["id"], "family": case["family"], "label": case["label"],
            "verdict": judged, "correct": judged == case["label"],
            "overlap_verdict": overlap_verdict(case["answer"], case["context"]),
            "reason": verdict["reason"],
        })

    if not rows:
        return {"available": False, "skipped": skipped, "n": 0,
                "note": "no LLM provider reachable — judged nothing, which is not the same as passing"}

    def _rate(selected, predicate):
        return round(sum(1 for r in selected if predicate(r)) / len(selected), 4) if selected else 0.0

    unfaithful = [r for r in rows if r["label"] == "unfaithful"]
    faithful = [r for r in rows if r["label"] == "faithful"]
    return {
        "available": True,
        "n": len(rows),
        "skipped": skipped,
        "accuracy": _rate(rows, lambda r: r["correct"]),
        "detection_rate": _rate(unfaithful, lambda r: r["verdict"] == "unfaithful"),
        "false_positive_rate": _rate(faithful, lambda r: r["verdict"] == "unfaithful"),
        # How often the judge and the cheap scorer agree. Where they agree, the
        # cheap one is enough; the gap is what the judge is being paid for.
        "agreement_with_overlap": _rate(rows, lambda r: r["verdict"] == r["overlap_verdict"]),
        "by_family": {
            f: {"n": sum(1 for r in rows if r["family"] == f),
                "accuracy": _rate([r for r in rows if r["family"] == f], lambda r: r["correct"])}
            for f in sorted({r["family"] for r in rows})
        },
        "rows": rows,
    }


def main() -> int:
    import asyncio

    overlap = evaluate_overlap()
    judged = asyncio.run(evaluate_judge_async())
    report = {
        "overlap": {k: v for k, v in overlap.items() if k != "rows"},
        "tolerance_sweep": sweep_tolerance(),
        "judge": {k: v for k, v in judged.items() if k != "rows"},
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
