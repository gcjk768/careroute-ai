"""[MLOps][Responsible-AI] E1 — Symptom-intake field-extraction accuracy.

Owner: Sham Goh.  Spec: app/evals/plan.E1_INTAKE_EXTRACTION.
Dataset: tests/fixtures/intake_gold.json (44 rows, en/es/zh/ms/fr/ta + voice).

Follows the shape of the E5 reference implementation (tests/test_eval_hitl.py):
a JSON gold set, the real code driven once, metrics cached, acceptance criteria
imported from the spec rather than invented here.

TWO DEPARTURES FROM THE E5 TEMPLATE, both deliberate:

1. It exercises `SymptomIntakeAgent` directly rather than the whole pipeline.
   E5 measures an END-OF-PIPELINE outcome (did a clinician get interrupted?), so
   it must drive everything. E1 measures the three fields THIS agent writes, and
   they are final the moment intake returns — every later stage only reads them.
   Driving the full pipeline would add the ML model, RAG and five other workers
   as dependencies of a test that is not measuring any of them.

2. The two paths are scored SEPARATELY, because the spec requires it and because
   scoring them together would be dishonest in both directions:
     * The deterministic `_fallback()` is English-only BY DESIGN and performs no
       translation. Scoring its keyword output on Chinese input would report a
       documented architectural decision as a defect.
     * The LLM path is the one that earns the multilingual claim, so it is scored
       on every row — but it needs a live provider, so it SKIPS rather than
       silently passing when none is reachable.
   Language detection is deterministic and language-independent, so it is scored
   on every row on the deterministic path.

The non-English deterministic result is not thrown away: `test_non_english_
coverage_gap_is_quantified` records it as a number, because "the fallback cannot
read Tamil" is a claim the evaluation should measure rather than assert.

Run with:  pytest -m eval
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from app import llm
from app.agents.base import CaseState
from app.agents.intake import SymptomIntakeAgent, _extract_keywords
from app.evals import by_id

pytestmark = [pytest.mark.eval, pytest.mark.intake]

SPEC = by_id("E1-intake-extraction")

# Acceptance criteria, mirrored from the spec prose into the numbers enforced
# here. Kept as module constants so a change to the bar is a visible diff.
MIN_LANGUAGE_ACCURACY = 0.90
MIN_KEYWORD_F1 = 0.80
MIN_CONTENT_RECALL = 0.95
MAX_HALLUCINATION_RATE = 0.0
# [NEW] Under-triage is the primary harm, so recall on the SEVERE keywords is
# held higher than overall F1 — missing "chest pain" and missing "cough" are not
# the same error. Mirrors the weighting the team committed to in the reviewer
# reply (recall on severe classes above accuracy, throughout).
MIN_SEVERE_RECALL = 0.95

#: The keywords whose omission is an under-triage. Deliberately the red-flag
#: adjacent subset — the same categories `redflags.RED_FLAG_RULES` forces acuity
#: on — so this metric is anchored to the safety table rather than to taste.
SEVERE_KEYWORDS = frozenset({
    "chest pain", "breathlessness", "stroke signs", "severe bleeding",
    "allergic reaction", "self-harm risk", "seizure", "severe pain",
})

_FIXTURE = Path(__file__).parent / "fixtures" / "intake_gold.json"


def _load() -> list[dict]:
    with _FIXTURE.open(encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def _run_agent(case: dict) -> dict:
    """Run intake on one gold row and return what it produced."""
    state = CaseState(
        raw_text=case["text"],
        language=case["declared_language"],
        is_voice=bool(case["is_voice"]),
    )
    result = asyncio.run(SymptomIntakeAgent().run(state))
    return {
        "id": case["id"],
        "gold_language": case["gold_language"],
        "gold_keywords": set(case["gold_keywords"]),
        "deterministic_expected": bool(case["deterministic_expected"]),
        "non_english": case["gold_language"] != "en",
        "source": result["source"],
        "detected_language": result["detected_language"],
        "predicted_keywords": set(result["keywords"]),
        # Clinical-content recall is measured by re-extracting from the
        # NORMALISED SENTENCE: the question is whether normalisation preserved
        # the clinical content, not whether the keyword list was copied across.
        "recoverable_keywords": set(_extract_keywords(result["normalised_symptoms"])),
        "normalised": result["normalised_symptoms"],
    }


# ---------------------------------------------------------------------------
# Metric helpers — shared by both paths so the two are computed identically.
# ---------------------------------------------------------------------------
def _micro_f1(rows: list[dict]) -> tuple[float, float, float]:
    tp = sum(len(r["gold_keywords"] & r["predicted_keywords"]) for r in rows)
    fp = sum(len(r["predicted_keywords"] - r["gold_keywords"]) for r in rows)
    fn = sum(len(r["gold_keywords"] - r["predicted_keywords"]) for r in rows)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _content_recall(rows: list[dict]) -> float:
    """Fraction of gold keywords still recoverable from the normalised sentence.

    Guards the safety-relevant failure: an agent that reports the right keyword
    list but drops the symptom from the sentence it hands downstream, where the
    English-only red-flag regex will never see it again."""
    total = sum(len(r["gold_keywords"]) for r in rows)
    kept = sum(len(r["gold_keywords"] & r["recoverable_keywords"]) for r in rows)
    return kept / total if total else 1.0


def _hallucinations(rows: list[dict]) -> list[tuple[str, set[str]]]:
    """Predicted keywords with no support in the gold set — each one asserts a
    symptom the patient did not report."""
    return [
        (r["id"], r["predicted_keywords"] - r["gold_keywords"])
        for r in rows
        if r["predicted_keywords"] - r["gold_keywords"]
    ]


def _severe_recall(rows: list[dict]) -> tuple[float, list[str]]:
    expected = missed = 0
    misses: list[str] = []
    for row in rows:
        wanted = row["gold_keywords"] & SEVERE_KEYWORDS
        expected += len(wanted)
        gap = wanted - row["predicted_keywords"]
        if gap:
            missed += len(gap)
            misses.append(f"{row['id']}:{sorted(gap)}")
    recall = (expected - missed) / expected if expected else 1.0
    return recall, misses


# ---------------------------------------------------------------------------
# PATH A — the deterministic fallback. This is the CI gate: it needs no network
# and is what almost every real request takes when the kill switch is engaged.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def deterministic() -> list[dict]:
    """Every gold row through the agent with the LLM hard-disabled.

    The root conftest already patches `llm.complete`, but this fixture is
    module-scoped and must not depend on a function-scoped autouse fixture, so
    it disables the LLM itself and restores the original afterwards."""
    original = llm.complete

    async def _fail(*_args, **_kwargs):
        raise llm.LLMUnavailableError("LLM disabled for the deterministic E1 pass")

    llm.complete = _fail
    try:
        return [_run_agent(case) for case in _load()]
    finally:
        llm.complete = original


def _scored(rows: list[dict]) -> list[dict]:
    """Rows the deterministic path is designed to handle.

    Excludes non-English input (English-only vocabulary, no translation) and the
    misspelling rows, which exist precisely to demonstrate that the deterministic
    pass CANNOT read them — that is what the empty-keyword LLM trigger is for.
    Scoring either group here would report a documented design decision as a
    defect; both are scored on the LLM path instead."""
    return [r for r in rows if r["deterministic_expected"]]


def test_dataset_covers_every_declared_language(deterministic):
    """A dataset missing a language the agent claims to detect cannot measure
    that claim. Guards against the fixture drifting away from _LANGUAGE_HINTS."""
    from app.agents.intake import _LANGUAGE_HINTS

    covered = {r["gold_language"] for r in deterministic}
    missing = set(_LANGUAGE_HINTS) - covered
    assert not missing, (
        f"_LANGUAGE_HINTS declares {sorted(missing)} but the gold set has no rows for it. "
        f"Either add rows or stop claiming the language."
    )


def test_dataset_covers_voice_and_misspelling(deterministic):
    """The two noise classes the agent explicitly claims to handle."""
    cases = _load()
    assert any(c["is_voice"] for c in cases), "no voice rows — transcription noise is unmeasured"
    assert any("typo" in c["id"] for c in cases), "no misspelling rows"


def test_fallback_always_produces_a_result(deterministic):
    """The availability guarantee: no row may come back empty-sourced, and every
    row must carry a language. An intake failure must never reach the pipeline."""
    assert all(r["source"] for r in deterministic)
    assert all(r["detected_language"] for r in deterministic)


def test_language_accuracy(deterministic):
    """Scored on EVERY row — detection is deterministic and does not depend on
    the English vocabulary, so the non-English rows are fair game here."""
    wrong = [
        f"{r['id']}: got {r['detected_language']!r}, want {r['gold_language']!r}"
        for r in deterministic
        if r["detected_language"] != r["gold_language"]
    ]
    accuracy = (len(deterministic) - len(wrong)) / len(deterministic)
    assert accuracy >= MIN_LANGUAGE_ACCURACY, (
        f"language accuracy {accuracy:.3f} < {MIN_LANGUAGE_ACCURACY}\n  " + "\n  ".join(wrong)
    )


def test_keyword_f1(deterministic):
    precision, recall, f1 = _micro_f1(_scored(deterministic))
    assert f1 >= MIN_KEYWORD_F1, (
        f"keyword micro-F1 {f1:.3f} < {MIN_KEYWORD_F1} (P={precision:.3f} R={recall:.3f})"
    )


def test_severe_keyword_recall(deterministic):
    """Weighted above overall F1 on purpose: missing 'chest pain' is an
    under-triage, missing 'cough' is an inconvenience."""
    recall, misses = _severe_recall(_scored(deterministic))
    assert recall >= MIN_SEVERE_RECALL, (
        f"severe-keyword recall {recall:.3f} < {MIN_SEVERE_RECALL}; missed={misses}"
    )


def test_clinical_content_recall(deterministic):
    """The safety-relevant failure: a symptom dropped during normalisation is
    invisible to every downstream agent."""
    recall = _content_recall(_scored(deterministic))
    assert recall >= MIN_CONTENT_RECALL, (
        f"clinical-content recall {recall:.3f} < {MIN_CONTENT_RECALL} — normalisation is "
        f"dropping symptoms the input contained."
    )


def test_no_hallucinated_symptoms(deterministic):
    """A fabricated symptom must never reach the classifier. Bar is exactly 0."""
    bad = _hallucinations(_scored(deterministic))
    rate = len(bad) / len(_scored(deterministic))
    assert rate <= MAX_HALLUCINATION_RATE, (
        f"hallucination rate {rate:.3f} > {MAX_HALLUCINATION_RATE}; "
        + "; ".join(f"{cid} invented {sorted(extra)}" for cid, extra in bad)
    )


def test_non_english_coverage_gap_is_quantified(deterministic):
    """NOT a pass/fail bar — a measurement.

    The deterministic path is English-only by design, so it is EXPECTED to
    return almost nothing for non-English input. This test records how much
    clinical content that costs, so the number appears in the evaluation instead
    of the limitation living only in a docstring.

    Coverage is NOT asserted to be zero. The first run of this evaluation found
    it is not: Spanish "Estoy vomitando" is caught by the English surface form
    "vomit", because matching is left-boundary with an open right edge. That is
    an accidental COGNATE hit, not translation — unplanned, but correct, and
    Latin-script languages will keep producing a few of them.

    What IS asserted is that whatever leaks through is RIGHT. A cognate that
    produced a wrong keyword would be worse than producing none, because a
    silently mistranslated symptom is indistinguishable downstream from a real
    one."""
    non_english = [r for r in deterministic if r["non_english"]]
    with_keywords = [r for r in non_english if r["predicted_keywords"]]
    coverage = len(with_keywords) / len(non_english)

    wrong = [
        (r["id"], sorted(r["predicted_keywords"] - r["gold_keywords"]))
        for r in with_keywords
        if r["predicted_keywords"] - r["gold_keywords"]
    ]
    assert not wrong, (
        f"cognate matches produced keywords absent from the gold set: {wrong}. A wrong keyword "
        f"from an untranslated language is worse than no keyword — downstream it is "
        f"indistinguishable from a real extraction."
    )
    # Recorded for the report. The bar is loose on purpose: this measures a gap,
    # it does not gate one. A large jump means the fallback quietly gained
    # translation, which would contradict the DESIGN DECISIONS block in
    # intake.py and the Safety-Override rationale in the capability audit.
    assert coverage <= 0.25, (
        f"non-English deterministic coverage is {coverage:.3f} — too high to still be cognate "
        f"accidents. If the fallback gained translation, update intake.py's DESIGN DECISIONS "
        f"and docs/vault/Agent Capability Audit.md, which cites this gap as the reason "
        f"Safety-Override's semantic layer exists."
    )


# ---------------------------------------------------------------------------
# PATH B — the LLM path, scored on EVERY row. Needs a live provider, so it skips
# rather than passing vacuously. This is the half that earns the multilingual
# claim, and it is the half CI cannot prove.
# ---------------------------------------------------------------------------
def _llm_reachable() -> bool:
    if os.environ.get("CAREROUTE_SKIP_LLM_EVAL"):
        return False
    try:
        return bool(asyncio.run(llm.health()).get("available"))
    except Exception:
        return False


@pytest.fixture(scope="module")
def llm_path() -> list[dict]:
    if not _llm_reachable():
        pytest.skip(
            "no LLM provider reachable — the E1 LLM-path scores cannot be produced. "
            "This is a SKIP, not a pass: the multilingual claim is unproven in this run."
        )
    return [_run_agent(case) for case in _load()]


def test_llm_path_recovers_non_english_keywords(llm_path):
    """The whole point of the LLM path. Every non-English row must yield the
    keywords its deterministic pass could not."""
    non_english = [r for r in llm_path if r["non_english"]]
    empty = [r["id"] for r in non_english if not r["predicted_keywords"]]
    recovered = (len(non_english) - len(empty)) / len(non_english)
    assert recovered >= MIN_LANGUAGE_ACCURACY, (
        f"LLM path recovered keywords for only {recovered:.3f} of non-English rows; "
        f"still empty: {empty}"
    )


def test_llm_path_keyword_f1(llm_path):
    precision, recall, f1 = _micro_f1(llm_path)
    assert f1 >= MIN_KEYWORD_F1, (
        f"LLM-path keyword micro-F1 {f1:.3f} < {MIN_KEYWORD_F1} (P={precision:.3f} R={recall:.3f})"
    )


def test_llm_path_severe_keyword_recall(llm_path):
    recall, misses = _severe_recall(llm_path)
    assert recall >= MIN_SEVERE_RECALL, (
        f"LLM-path severe-keyword recall {recall:.3f} < {MIN_SEVERE_RECALL}; missed={misses}"
    )


def test_llm_path_no_hallucinated_symptoms(llm_path):
    """The risk the deterministic path does not have. The model rewrites the
    sentence, so this is where an invented symptom would enter."""
    bad = _hallucinations(llm_path)
    rate = len(bad) / len(llm_path)
    assert rate <= MAX_HALLUCINATION_RATE, (
        f"LLM-path hallucination rate {rate:.3f} > {MAX_HALLUCINATION_RATE}; "
        + "; ".join(f"{cid} invented {sorted(extra)}" for cid, extra in bad)
    )


def test_llm_path_clinical_content_recall(llm_path):
    """Catches the LLM dropping a symptom while rewriting — the failure mode the
    deterministic path cannot have, because it never rewrites."""
    recall = _content_recall(llm_path)
    assert recall >= MIN_CONTENT_RECALL, (
        f"LLM-path clinical-content recall {recall:.3f} < {MIN_CONTENT_RECALL} — the model is "
        f"dropping symptoms while normalising."
    )


# ---------------------------------------------------------------------------
def test_spec_and_implementation_agree():
    """Guards against the plan and the code drifting apart — the failure mode
    that produced the proposal review in the first place."""
    assert SPEC.status == "implemented", "E1 is implemented; update its status in app/evals/plan.py"
    assert "tests/test_eval_intake.py" in SPEC.implemented_by
    assert SPEC.dataset_exists, f"spec points at {SPEC.dataset}, which does not exist"
