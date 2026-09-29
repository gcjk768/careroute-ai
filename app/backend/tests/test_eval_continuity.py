"""[Agentic] E14 — multi-turn continuity of the clarification handshake.

Drives the REAL pipeline three times per case (turn 1, turn 2 with the
patient's answer, and the empty-answer control), with the LLM disabled by the
suite-wide kill switch. Slower than a unit test and deliberately so: continuity
is a property of two turns of the actual system, and a hand-built CaseState
cannot observe it.
"""
import pytest

from app.evals import continuity

pytestmark = [pytest.mark.eval, pytest.mark.hitl]


@pytest.fixture(scope="module")
def report():
    return continuity.evaluate()


def test_the_fixture_still_reaches_the_ask_branch(report):
    """A continuity corpus that stopped triggering the handshake would score
    whatever the pipeline does instead, and pass. That is a broken evaluation,
    not a green one."""
    assert report["asked_rate"] == 1.0


def test_the_handshake_never_asks_the_same_question_twice(report):
    """Loop safety: the transcript rides on the request, so a question cannot
    be repeated even if server state is lost."""
    assert report["by_property"]["no_repeat_question"] == 1.0


def test_a_resumed_turn_still_reasons_about_the_original_complaint(report):
    assert report["by_property"]["context_retained"] == 1.0


def test_a_resumed_turn_always_terminates(report):
    assert report["by_property"]["terminates"] == 1.0


def test_continuity_does_not_regress_below_what_was_measured(report):
    """0.75 on 2026-09-17: three of four properties hold. This floor catches a
    regression in those three; the fourth has its own test below."""
    assert report["continuity"] >= 0.75


def test_the_patients_answer_changes_the_outcome(report):
    """Closed 2026-09-26 (was a strict xfail from 2026-09-17: the answer was
    carried but never read). Intake now folds each answer into the classified
    text, so a case answered "yes, fever and struggling to breathe" decides
    differently from one answered with silence. A DENIAL adds no symptom, so
    the denying rows legitimately match their control: the bar is "the
    confirming answers are used", i.e. every `implies: confirms` row."""
    confirms = [r for r in report["cases"] if r["implies"] == "confirms"]
    assert confirms
    assert all(r["properties"]["answer_used"] for r in confirms), [
        (r["id"], r["properties"]) for r in confirms if not r["properties"]["answer_used"]
    ]
    assert report["by_property"]["answer_used"] > 0.0
