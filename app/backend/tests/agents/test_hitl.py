"""Human-in-the-Loop agent — owner: Heriz Yusoff.   Run with: pytest -m hitl"""
import pytest

from app.agents import HumanInTheLoopAgent
from app.agents.hitl import GAIN_THRESHOLD
from app.agents.messaging import AgentMessage, MessageBus
from harness import emit_and_check, make_case, run_and_check

pytestmark = pytest.mark.hitl


def test_hitl_emits_review_decision_message():
    msg = emit_and_check(HumanInTheLoopAgent(), make_case(confidence=0.30))
    assert msg.intent == "review.decision"
    assert msg.recipient == "reflection"
    assert msg.payload["escalated"] is True


def test_hitl_escalates_low_confidence():
    result, state = run_and_check(HumanInTheLoopAgent(), make_case(confidence=0.30))
    assert state.escalated is True
    assert result["escalated"] is True


def test_hitl_escalates_when_safety_triggered():
    _result, state = run_and_check(
        HumanInTheLoopAgent(),
        make_case(confidence=0.95, safety_triggered=True, safety_rule="chest_pain"),
    )
    assert state.escalated is True


def test_hitl_does_not_escalate_a_confident_safe_case():
    _result, state = run_and_check(
        HumanInTheLoopAgent(),
        make_case(confidence=0.95, safety_triggered=False),
    )
    assert state.escalated is False


# --------------------------------------------------------------------------
# [A2A] Escalate / ask / proceed — docs/vault/HITL Clarification Handshake.md.
#
# `_deliver_to_hitl` publishes a classifier `acuity.classified` message with a
# hand-set `clarification` payload and hands HITL its inbox exactly as the
# Supervisor does (bus.publish -> hitl.consume(bus.inbox(hitl))), so these
# exercise the same `_consumed`-guarded receive path `run()` actually uses,
# not a shortcut. Compare tests/agents/test_classifier.py's own
# `_deliver_to_hitl` (his file, his side of the same handoff) -- this is the
# HITL-side counterpart.
# --------------------------------------------------------------------------

def _deliver_to_hitl(state, clarification):
    """Publish an `acuity.classified` message carrying `clarification` and
    return a HITL agent that has consumed it -- the same two calls the
    Supervisor makes for every worker (see supervisor._deliver)."""
    bus = MessageBus()
    bus.publish(AgentMessage(
        sender="classifier", recipient="broadcast", intent="acuity.classified",
        payload={
            "acuity_code": state.acuity_code,
            "confidence": state.confidence,
            "evidence": state.evidence,
            "clarification": clarification,
        },
    ))
    hitl = HumanInTheLoopAgent()
    hitl.consume(bus.inbox(hitl))
    return hitl


def _proposal(gain=GAIN_THRESHOLD):
    return {
        "feature": "cold_symptoms", "label": "cold symptoms",
        "question": "Are you having any difficulty breathing?",
        "gain": gain,
    }


@pytest.mark.parametrize("clarification", ["junk", {"gain": None}, {"question": "x?"}, 7])
def test_a_malformed_proposal_is_declined_not_raised(clarification):
    """A proposal that is not a dict, or whose gain is missing or not a
    number, used to raise out of run(); the worker runner only catches
    AgentUnavailableError, so a malformed payload from a remote classifier
    took the whole triage stream down. It is treated as no usable question."""
    state = make_case(confidence=0.427, acuity_code="P3_URGENT")
    hitl = _deliver_to_hitl(state, clarification)

    result, state = run_and_check(hitl, state)

    assert result["action"] in {"escalate", "proceed"}
    assert state.escalated is True


def test_hitl_asks_when_a_proposal_clears_the_gain_threshold():
    """The one case asking may replace: low confidence alone, nothing else
    forcing review, a proposal at or above GAIN_THRESHOLD. The question
    returned is the classifier's, not one HITL invented."""
    proposal = _proposal(gain=GAIN_THRESHOLD)
    state = make_case(confidence=0.427, acuity_code="P3_URGENT")
    hitl = _deliver_to_hitl(state, proposal)

    result, state = run_and_check(hitl, state)

    assert result["action"] == "ask"
    assert result["question"] == proposal["question"]
    assert result["clarification"] == proposal
    assert state.escalated is False
    assert state.escalation_reason is None
    assert "declinedQuestion" not in result


def test_hitl_ignores_the_proposal_when_safety_triggered():
    """A hard floor wins outright; the question is never even considered."""
    proposal = _proposal(gain=5.0)
    state = make_case(
        confidence=0.427, acuity_code="P3_URGENT",
        safety_triggered=True, safety_rule="chest_pain",
    )
    hitl = _deliver_to_hitl(state, proposal)

    result, state = run_and_check(hitl, state)

    assert result["action"] == "escalate"
    assert state.escalated is True
    assert "question" not in result
    assert "declinedQuestion" not in result


def test_hitl_escalates_outright_at_p1_or_p2_even_with_a_good_proposal():
    """Off-limits acuity: too acute to delay with a follow-up question, even
    when the proposal itself clears the gain bar."""
    proposal = _proposal(gain=5.0)
    state = make_case(confidence=0.427, acuity_code="P1_RESUSCITATION")
    hitl = _deliver_to_hitl(state, proposal)

    result, state = run_and_check(hitl, state)

    assert result["action"] == "escalate"
    assert state.escalated is True
    assert result["declinedQuestion"] == proposal


def test_hitl_keeps_asking_until_the_question_budget_is_spent():
    """The interview (2026-09-26): the cap is INTERVIEW_MAX_QUESTIONS answers,
    riding on the request (state.clarifications), not server state. One answer
    does not end it; a full transcript does, and then the 0.5 floor decides
    exactly as it always did."""
    from app.agents.base import INTERVIEW_MAX_QUESTIONS

    proposal = _proposal(gain=5.0)
    state = make_case(
        confidence=0.427, acuity_code="P3_URGENT",
        clarifications=[{"feature": "high_fever", "answer": "no", "question": "Any fever?"}],
    )
    result, state = run_and_check(_deliver_to_hitl(state, proposal), state)
    assert result["action"] == "ask"
    assert state.clarification_asked is True

    spent = make_case(
        confidence=0.427, acuity_code="P3_URGENT",
        clarifications=[{"feature": f"d{i}", "answer": "not sure", "question": f"q{i}?"}
                        for i in range(INTERVIEW_MAX_QUESTIONS)],
    )
    result, spent = run_and_check(_deliver_to_hitl(spent, proposal), spent)
    assert result["action"] == "escalate"
    assert spent.escalated is True
    assert spent.clarification_asked is False
    assert "declinedQuestion" not in result  # nothing was proposed once the budget is gone


def test_hitl_never_repeats_a_question_already_in_the_transcript():
    proposal = _proposal(gain=5.0)
    state = make_case(
        confidence=0.427, acuity_code="P3_URGENT",
        clarifications=[{"feature": "cold_symptoms", "answer": "no", "question": proposal["question"]}],
    )
    result, state = run_and_check(_deliver_to_hitl(state, proposal), state)
    assert result["action"] == "escalate"
    assert result["declinedQuestion"] == proposal


def test_hitl_asks_a_dimension_question_without_a_gain_number():
    """Onset / duration / severity questions carry no model-measured gain, so
    the template gain bar does not apply to them; they are asked on the
    interview's own terms (budget, target, floors)."""
    proposal = {"feature": "onset", "label": "onset", "gain": 0.0, "source": "fallback",
                "question": "When did this start, and did it come on suddenly or gradually?"}
    state = make_case(confidence=0.7, acuity_code="P4_NON_URGENT")
    result, state = run_and_check(_deliver_to_hitl(state, proposal), state)
    assert result["action"] == "ask"
    assert state.escalated is False


def test_hitl_stops_the_interview_at_the_confidence_target():
    from app.agents.base import INTERVIEW_CONFIDENCE_TARGET

    proposal = _proposal(gain=5.0)
    state = make_case(confidence=INTERVIEW_CONFIDENCE_TARGET, acuity_code="P4_NON_URGENT")
    result, state = run_and_check(_deliver_to_hitl(state, proposal), state)
    assert result["action"] == "proceed"
    assert state.clarification_asked is False
    assert "declinedQuestion" not in result


def test_a_mid_confidence_case_is_asked_rather_than_decided():
    """0.7 used to proceed silently; it is now one question away from a
    firmer call."""
    proposal = _proposal(gain=5.0)
    state = make_case(confidence=0.7, acuity_code="P4_NON_URGENT")
    result, _state = run_and_check(_deliver_to_hitl(state, proposal), state)
    assert result["action"] == "ask"


def test_hitl_escalates_and_records_the_decline_below_gain_threshold():
    """A proposal that exists but isn't informative enough is escalated, and
    the audit trail keeps it as `declinedQuestion` rather than dropping it."""
    proposal = _proposal(gain=GAIN_THRESHOLD - 0.5)
    state = make_case(confidence=0.427, acuity_code="P3_URGENT")
    hitl = _deliver_to_hitl(state, proposal)

    result, state = run_and_check(hitl, state)

    assert result["action"] == "escalate"
    assert state.escalated is True
    assert result["declinedQuestion"] == proposal


def test_hitl_escalates_unchanged_when_classifier_declines_to_propose():
    """`clarification: None` is the classifier's own considered decision not
    to ask -- escalate, exactly as before this agent could ask anything, and
    no `declinedQuestion` (nothing was ever proposed to decline)."""
    state = make_case(confidence=0.427, acuity_code="P3_URGENT")
    hitl = _deliver_to_hitl(state, None)

    result, state = run_and_check(hitl, state)

    assert result["action"] == "escalate"
    assert state.escalated is True
    assert "declinedQuestion" not in result


def test_hitl_behaviour_is_unchanged_with_no_bus_at_all():
    """A standalone `run()` call (no Supervisor, no consume(), no inbox) is the
    unit-test shape every other test in this file uses. It must behave exactly
    as it did before asking existed -- proof this is an added path, not a
    changed one."""
    result, state = run_and_check(HumanInTheLoopAgent(), make_case(confidence=0.427))

    assert result["action"] == "escalate"
    assert state.escalated is True
    assert "question" not in result
    assert "declinedQuestion" not in result


def test_hitl_escalates_review_only_semantic_finding_without_acuity_trigger():
    _result, state = run_and_check(
        HumanInTheLoopAgent(),
        make_case(
            confidence=0.95,
            safety_triggered=False,
            safety_requires_human_review=True,
            safety_review_reason="Uncertain semantic safety finding requires clinician review.",
        ),
    )

    assert state.escalated is True
    assert "Uncertain semantic" in (state.escalation_reason or "")
