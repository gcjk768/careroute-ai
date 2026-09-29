"""[MLOps][Responsible-AI] Offline TRIAGE-EVAL gate — a gold-vignette benchmark.

Where test_pipeline.py asserts the *agentic* guarantees on a handful of hand-
picked cases, this drives the SAME deterministic pipeline over a small gold
evaluation set (tests/fixtures/triage_vignettes.json) and asserts model-quality
floors that gate a release:

  [MLOps]          overall acuity accuracy on the gold set stays above a floor,
  [Responsible-AI] red-flag RECALL is 1.0 on the must-escalate cases — every
                   unambiguous emergency is escalated to a human (a missed
                   escalation here is the safety-critical failure), and
  [Responsible-AI] citation-GROUNDEDNESS — every final decision carries at least
                   one retrieved citation (no ungrounded advice).

Runs with the LLM forced unreachable (kill-switch path), so the eval is fully
deterministic and never depends on an LLM being present — mirroring the setup in
test_pipeline.py. Emits JUnit XML under `pytest --junitxml=...` (see .gitlab-ci.yml).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import llm
from app.main import _triage_event_stream
from app.models import TriageRequest

# Release floors for the deterministic triage pipeline. Accuracy is set with
# margin below the observed exact-match rate; red-flag recall is SAFETY-critical
# and must be perfect.
MIN_ACUITY_ACCURACY = 0.75
MIN_RED_FLAG_RECALL = 1.0

_FIXTURE = Path(__file__).parent / "fixtures" / "triage_vignettes.json"


@pytest.fixture(autouse=True)
def dead_llm(monkeypatch):
    """Force the deterministic fallback path (no LLM) so the eval is reproducible
    with or without the LLM — identical to test_pipeline.py's setup. Patching
    `llm.complete` disables the LLM for every worker at once."""

    async def _fail(*_args, **_kwargs):
        raise llm.LLMUnavailableError("LLM disabled in tests")

    monkeypatch.setattr(llm, "complete", _fail)


@pytest.fixture(autouse=True)
def fast_pipeline(monkeypatch):
    """Skip the inter-step UI animation delays so the eval sweep is fast."""
    import app.main as main_mod

    async def _no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main_mod, "_step_delay", _no_delay)


def _load_fixture() -> dict:
    with _FIXTURE.open(encoding="utf-8") as fh:
        return json.load(fh)


def _final_of(text: str) -> dict | None:
    """Drive the real pipeline and return its single `final` event (or None)."""

    async def _collect() -> dict | None:
        req = TriageRequest(text=text, language="en", isVoice=False)
        final = None
        async for chunk in _triage_event_stream(req):
            line = chunk.strip()
            if line.startswith("data:"):
                event = json.loads(line[len("data:"):].strip())
                if event.get("event") == "final":
                    final = event
        return final

    return asyncio.run(_collect())


@pytest.fixture(scope="module")
def predictions() -> list[dict]:
    """Run the whole gold set ONCE and cache (text, gold, predicted) rows."""
    fixture = _load_fixture()
    severe = set(fixture["severe_codes"])
    rows: list[dict] = []
    for v in fixture["vignettes"]:
        final = _final_of(v["text"])
        assert final is not None, f"vignette {v['id']} produced no final decision"
        pred_code = final["acuity"]["code"]
        correct = pred_code == v["gold_acuity"] or (v.get("severe_ok") and pred_code in severe)
        rows.append(
            {
                "id": v["id"],
                "must_escalate": v["must_escalate"],
                "gold": v["gold_acuity"],
                "pred": pred_code,
                "correct": bool(correct),
                "escalated": bool(final["escalated"]),
                "citations": final.get("citations") or [],
            }
        )
    return rows


def test_gold_set_acuity_accuracy_above_floor(predictions):
    correct = sum(r["correct"] for r in predictions)
    accuracy = correct / len(predictions)
    misses = [f"{r['id']}: gold={r['gold']} pred={r['pred']}" for r in predictions if not r["correct"]]
    assert accuracy >= MIN_ACUITY_ACCURACY, (
        f"acuity accuracy {accuracy:.3f} below floor {MIN_ACUITY_ACCURACY}; misses={misses}"
    )


def test_red_flag_recall_is_perfect_on_must_escalate(predictions):
    """[Responsible-AI] Every unambiguous red-flag MUST escalate to a human and
    be assessed at a severe tier — a missed escalation is the safety-critical
    failure this gate exists to catch."""
    must = [r for r in predictions if r["must_escalate"]]
    assert must, "fixture defines no must-escalate cases"
    escalated = [r for r in must if r["escalated"]]
    recall = len(escalated) / len(must)
    missed = [r["id"] for r in must if not r["escalated"]]
    assert recall >= MIN_RED_FLAG_RECALL, f"red-flag recall {recall:.3f} < {MIN_RED_FLAG_RECALL}; missed={missed}"
    # A red-flag must also land in the severe acuity tier (P1/P2), never lower.
    not_severe = [r["id"] for r in must if not r["correct"]]
    assert not not_severe, f"must-escalate cases assessed below severe tier: {not_severe}"


def test_every_decision_is_citation_grounded(predictions):
    """[Responsible-AI] No ungrounded advice: every final decision must surface at
    least one retrieved citation with a real source/title."""
    ungrounded = []
    for r in predictions:
        grounded = any(
            (c.get("source") or c.get("title") or c.get("snippet"))
            for c in r["citations"]
        )
        if not grounded:
            ungrounded.append(r["id"])
    assert not ungrounded, f"decisions with no grounded citation: {ungrounded}"
