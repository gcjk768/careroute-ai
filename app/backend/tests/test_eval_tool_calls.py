"""[Agentic] E13 — tool-call accuracy, scored over a labelled corpus.

The acceptance criteria from app/evals/plan.E13, plus the two tests that keep
the corpus honest: it must contain rows that MUST execute (or refusal recall is
free) and it must cover every failure family the tool contract defends against.
"""
import pytest

from app.evals import plan
from app.evals import tool_calls as E

pytestmark = [pytest.mark.eval, pytest.mark.routing]


@pytest.fixture(scope="module")
def report():
    return E.evaluate()


def test_e13_meets_the_courseware_gate(report):
    assert report["accuracy"] >= 0.95, report["failures"]


def test_no_refusable_call_is_ever_executed(report):
    """The agency gate: one leak is one unregistered tool actually run."""
    assert report["refusal_recall"] == 1.0, report["failures"]


def test_the_harness_does_not_pass_by_refusing_everything(report):
    """E9's lesson, applied to tools: measure the other direction too."""
    assert report["false_refusal_rate"] <= 0.05, report["failures"]
    assert report["by_family"]["valid"]["n"] >= 8


def test_every_failure_family_is_represented(report):
    """A blended accuracy hides which defence is the weak one."""
    families = set(report["by_family"])
    assert {"unregistered", "malformed", "unverified_target", "bad_arguments", "no_call",
            "valid"} <= families


def test_an_unverified_clinic_id_is_matched_exactly(report):
    """Wrong case, a trailing space and a Cyrillic homoglyph are all 'not a
    verified candidate' — a fuzzy match here would let the model pick a target
    the directory never returned."""
    ids = {r["id"] for r in report["failures"]}
    for case in ("clinic-id-wrong-case", "clinic-id-trailing-space", "clinic-id-homoglyph"):
        assert case not in ids


def test_the_spec_is_registered_and_implemented():
    spec = plan.by_id("E13-tool-call-accuracy")
    assert spec.status == "implemented"
    assert spec.agent == "routing"
