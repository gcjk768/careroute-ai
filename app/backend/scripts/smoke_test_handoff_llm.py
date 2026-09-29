"""Manually exercise ClinicianHandoffAgent's REAL LLM path (not the fallback),
and score it against E8-handoff-faithfulness's own four metrics.

    cd backend && python scripts/smoke_test_handoff_llm.py

Why this exists as a script and not a pytest test: the whole automated suite
(root conftest.py) intentionally force-fails `llm.complete` for every test, so
`tests/agents/test_handoff.py` and `tests/test_eval_handoff.py` only ever
exercise the deterministic fallback template. That's correct for CI (fast,
reproducible, no network/credentials) but means the LLM-authored path has
never actually run, and E8's own metrics -- hallucination rate, grounding
coverage, citation fidelity, retrieval-gating correctness -- have only ever
been measured against a template that is faithful BY CONSTRUCTION. They have
never once been measured against what actually generates the text a clinician
reads. This script is that measurement.

Reuses the same gold cases as tests/fixtures/handoff_faithfulness.json and the
EXACT SAME metric definitions tests/test_eval_handoff.py computes against the
fallback -- not a new scoring shape, so the two numbers are directly
comparable. A summary that fails these checks against the real model is a
real defect (prompt needs tightening), not a fixture problem.

Metrics are computed ONLY over rows where the real LLM actually answered
(source == "llm"). A row that silently fell back to the template is reported
separately and excluded from the rates, so a transient provider hiccup can't
make the real-model numbers look better than they are by hiding behind the
fallback's constructed faithfulness.

Nothing here touches any file outside this script + the fixture it reads.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

from app import llm, rag                              # noqa: E402
from app.agents.base import CaseState                  # noqa: E402
from app.agents.handoff import ClinicianHandoffAgent    # noqa: E402

_FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "handoff_faithfulness.json"
_CORPUS_KEYS = {(d.title, d.source) for d in rag.CORPUS}

# Mirrors tests/test_eval_handoff.py's acceptance criteria exactly, so a run
# against the real model is checked against the same bar as the fallback.
_ACCEPTANCE = {
    "hallucination rate": 0.0,
    "grounding coverage": 1.0,
    "citation fidelity": 1.0,
    "retrieval-gating correctness": 1.0,
}


def _state_from_case(case: dict) -> CaseState:
    state = CaseState(raw_text=case["raw_text"])
    state.normalised_symptoms = case["normalised_symptoms"]
    state.acuity_code = case["acuity_code"]
    state.confidence = case["confidence"]
    state.evidence = list(case["evidence"])
    state.safety_triggered = bool(case["safety_triggered"])
    state.safety_rule = case["safety_rule"]
    state.safety_reason = case["safety_reason"]
    state.escalated = True
    state.escalation_reason = case["escalation_reason"]
    return state


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0  # vacuously true, matches the pytest checks


async def main() -> int:
    health = await llm.health()
    print(f"LLM provider check: {health}\n")
    if not health["available"]:
        print("No LLM provider is reachable (no OPENAI_API_KEY). "
              "Every case below will silently use the fallback template -- "
              "set one up first, or this script just re-proves the fallback path.\n")

    with _FIXTURE.open(encoding="utf-8") as fh:
        fixture = json.load(fh)

    agent = ClinicianHandoffAgent()
    rows: list[dict] = []

    for case in fixture["cases"]:
        state = _state_from_case(case)
        result = await agent.run(state)
        source = result.get("source")
        summary = str(result.get("summary") or "")
        summary_l = summary.lower()
        citations = result.get("citations") or []

        hallucinated = [t for t in case["decoy_terms"] if t.lower() in summary_l]
        missing = [t for t in case["required_terms"] if t.lower() not in summary_l]
        bad_citations = [
            (c.get("title"), c.get("source")) for c in citations
            if (c.get("title"), c.get("source")) not in _CORPUS_KEYS
        ]
        gated_correctly = bool(citations) == bool(case["safety_triggered"])
        ok = not hallucinated and not missing and not bad_citations and gated_correctly

        rows.append({
            "id": case["id"], "source": source, "ok": ok,
            "hallucinated": hallucinated, "missing": missing,
            "bad_citations": bad_citations, "gated_correctly": gated_correctly,
            "citation_count": len(citations), "bad_citation_count": len(bad_citations),
        })

        print(f"[{case['id']}] source={source} {'OK' if ok else 'FAIL'}")
        print(f"  summary: {summary}")
        print(f"  citations: {len(citations)}"
              + (f" ({len(bad_citations)} not traceable to rag.CORPUS!)" if bad_citations else ""))
        print(f"  follow_up_questions: {result.get('follow_up_questions')}")
        if hallucinated:
            print(f"  !! HALLUCINATED (decoy terms present): {hallucinated}")
        if missing:
            print(f"  !! MISSING required terms: {missing}")
        if not gated_correctly:
            print(f"  !! GATING WRONG: safety_triggered={case['safety_triggered']} "
                  f"but citations={'present' if citations else 'absent'}")
        print()

    llm_rows = [r for r in rows if r["source"] == "llm"]
    fallback_rows = [r for r in rows if r["source"] != "llm"]

    print("=== E8-handoff-faithfulness, live-model run ===")
    print(f"{len(llm_rows)}/{len(rows)} rows actually answered by the real model"
          f"{f'; {len(fallback_rows)} fell back and are EXCLUDED from the rates below' if fallback_rows else ''}.")

    if not llm_rows:
        print("\nNo row was answered by a real model -- nothing to score. Check the health line above.")
        return 1

    total_citations = sum(r["citation_count"] for r in llm_rows)
    total_bad_citations = sum(r["bad_citation_count"] for r in llm_rows)
    metrics = {
        "hallucination rate": _rate(sum(1 for r in llm_rows if r["hallucinated"]), len(llm_rows)),
        "grounding coverage": _rate(sum(1 for r in llm_rows if not r["missing"]), len(llm_rows)),
        "citation fidelity": _rate(total_citations - total_bad_citations, total_citations),
        "retrieval-gating correctness": _rate(sum(1 for r in llm_rows if r["gated_correctly"]), len(llm_rows)),
    }

    print()
    all_pass = True
    for name, value in metrics.items():
        target = _ACCEPTANCE[name]
        passed = value == target
        all_pass = all_pass and passed
        print(f"  {name:<30} {value:.3f}   (target {target}, {'PASS' if passed else 'FAIL'})")

    overall = sum(1 for r in llm_rows if r["ok"]) / len(llm_rows)
    print(f"\n  overall pass rate                {overall:.3f}   ({sum(1 for r in llm_rows if r['ok'])}/{len(llm_rows)} rows clean)")

    print(f"\n=== {'ALL METRICS PASS' if all_pass else 'AT LEAST ONE METRIC FAILED'} against the real model ===")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
