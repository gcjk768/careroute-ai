"""[Responsible-AI][MLOps] E5 — Human-in-the-Loop trigger accuracy.

*** REFERENCE IMPLEMENTATION for the reviewer's Point 2. ***
The other four evaluation owners should copy this file's shape: a JSON dataset
under tests/fixtures/, the REAL pipeline driven end-to-end with the LLM disabled,
metrics computed once and cached, and acceptance criteria that come from
app/evals/plan.py rather than being invented here.

Spec: app/evals/plan.E5_HITL_TRIGGER.

WHY THIS EXISTS
---------------
test_triage_eval.py already asserts red-flag recall of 1.0. That is necessary and
insufficient: recall alone is maximised by escalating every single case, which
would flood the clinician queue and make the "AI assists, clinician decides"
principle meaningless in practice. This file adds the missing other half —
SPECIFICITY on cases a clinician must NOT be interrupted by — so the two
criteria together actually constrain the system.

Run with: pytest -m eval
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import llm
from app.evals import by_id
from app.main import _triage_event_stream
from app.models import TriageRequest

pytestmark = pytest.mark.eval

SPEC = by_id("E5-hitl-trigger")

# Acceptance criteria, mirrored from the spec prose into the numbers this file
# enforces. Recall is absolute; specificity is set at 0.80 because the
# deterministic confidence gate is intentionally cautious and a small amount of
# over-escalation on ambiguous minor complaints is an acceptable trade.
MIN_RECALL = 1.0
MIN_SPECIFICITY = 0.80

_FIXTURE = Path(__file__).parent / "fixtures" / "hitl_cases.json"


@pytest.fixture(autouse=True)
def dead_llm(monkeypatch):
    """Force the deterministic path so the evaluation is reproducible with or
    without the LLM. Patching `llm.complete` disables the LLM for every worker AND
    every reasoning layer at once (see agents/reasoning.py)."""

    async def _fail(*_args, **_kwargs):
        raise llm.LLMUnavailableError("LLM disabled in tests")

    monkeypatch.setattr(llm, "complete", _fail)


@pytest.fixture(autouse=True)
def fast_pipeline(monkeypatch):
    """Skip the inter-step UI animation delays so the sweep is fast."""
    import app.main as main_mod

    async def _no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main_mod, "_step_delay", _no_delay)


def _final_of(text: str) -> dict | None:
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
def outcomes() -> list[dict]:
    """Drive every gold case through the real pipeline ONCE."""
    with _FIXTURE.open(encoding="utf-8") as fh:
        fixture = json.load(fh)

    rows: list[dict] = []
    for case in fixture["cases"]:
        final = _final_of(case["text"])
        assert final is not None, f"case {case['id']} produced no final decision"
        rows.append({
            "id": case["id"],
            "must_escalate": bool(case["must_escalate"]),
            "escalated": bool(final["escalated"]),
            "reason": final.get("escalationReason") or final.get("escalation_reason") or "",
            "acuity": final["acuity"]["code"],
        })
    return rows


def test_dataset_covers_both_classes(outcomes):
    """A dataset with only positive cases cannot measure over-escalation. This
    guard is the reason the evaluation is meaningful — do not remove it when
    extending the fixture."""
    positives = [r for r in outcomes if r["must_escalate"]]
    negatives = [r for r in outcomes if not r["must_escalate"]]
    assert positives, "fixture defines no must-escalate cases"
    assert negatives, (
        "fixture defines no must-NOT-escalate cases — without them, recall is trivially "
        "satisfiable by escalating everything and this evaluation proves nothing."
    )


def test_recall_on_must_escalate_is_perfect(outcomes):
    """A missed escalation is the unacceptable failure. Floor: 1.0."""
    must = [r for r in outcomes if r["must_escalate"]]
    missed = [r["id"] for r in must if not r["escalated"]]
    recall = (len(must) - len(missed)) / len(must)
    assert recall >= MIN_RECALL, f"HITL recall {recall:.3f} < {MIN_RECALL}; missed={missed}"


def test_specificity_on_must_not_escalate(outcomes):
    """THE CRITERION THE PREVIOUS PLAN LACKED. Over-escalation is a real failure:
    it floods the clinician queue and erodes trust in every alert."""
    must_not = [r for r in outcomes if not r["must_escalate"]]
    over = [r["id"] for r in must_not if r["escalated"]]
    specificity = (len(must_not) - len(over)) / len(must_not)
    assert specificity >= MIN_SPECIFICITY, (
        f"HITL specificity {specificity:.3f} < {MIN_SPECIFICITY}; over-escalated={over}. "
        f"Every case here is one a clinician should not have been interrupted by."
    )


def test_every_escalation_is_attributable(outcomes):
    """An escalation with no reason cannot be triaged by the clinician receiving
    it, and cannot be attributed to a rule during review."""
    unexplained = [r["id"] for r in outcomes if r["escalated"] and not str(r["reason"]).strip()]
    assert not unexplained, f"escalations with no reason string: {unexplained}"


def test_spec_and_implementation_agree():
    """Guards against the plan and the code drifting apart — the failure mode
    that produced this review in the first place."""
    assert SPEC.status == "implemented"
    assert "tests/test_eval_hitl.py" in SPEC.implemented_by
    assert SPEC.dataset_exists
