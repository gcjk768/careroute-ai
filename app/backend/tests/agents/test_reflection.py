"""Reflection / Critic agent — owner: platform (James).   Run with: pytest -m reflection"""
import pytest

from app.agents import ReflectionAgent
from app.agents.capability import ORCHESTRATOR_SLUG
from harness import emit_and_check, make_case, run_and_check

pytestmark = pytest.mark.reflection


def test_reflection_emits_decision_reviewed_message():
    msg = emit_and_check(
        ReflectionAgent(),
        make_case(acuity_code="P2_EMERGENT", care_tier="Emergency Department", escalated=False),
    )
    assert msg.intent == "decision.reviewed"
    # Addressed to whoever holds the orchestrator role, not a hard-coded name.
    # That used to be a separate Supervisor agent; it is now Symptom-Intake.
    assert msg.recipient == ORCHESTRATOR_SLUG
    assert "passed" in msg.payload


def test_reflection_forces_escalation_on_unescalated_high_acuity():
    result, state = run_and_check(
        ReflectionAgent(),
        make_case(acuity_code="P2_EMERGENT", care_tier="Emergency Department", escalated=False),
    )
    assert result["passed"] is False
    assert state.escalated is True
    assert "Reflection" in (state.escalation_reason or "")


def test_reflection_passes_a_consistent_low_acuity_case():
    result, state = run_and_check(
        ReflectionAgent(),
        make_case(
            acuity_code="P5_SELF_CARE",
            care_tier="Telehealth",
            escalated=False,
            confidence=0.9,
            evidence=["mild cold symptoms"],
        ),
    )
    assert result["passed"] is True
    assert state.escalated is False


# ---------------------------------------------------------------------------
# Consistency check 1: routing tier must match the final acuity.
# ---------------------------------------------------------------------------
def test_reflection_reroutes_a_tier_that_disagrees_with_acuity():
    """A P1 case that somehow carries a GP tier is inconsistent; Reflection
    re-routes it to the clinical-policy floor and records the correction."""
    result, state = run_and_check(
        ReflectionAgent(),
        make_case(acuity_code="P1_RESUSCITATION", care_tier="GP", escalated=True, confidence=0.95),
    )
    assert result["passed"] is False
    assert any("inconsistent with acuity" in issue for issue in result["issues"])
    assert state.care_tier == "Emergency Department"
    assert "Re-routed to Emergency Department." in result["corrections"]


def test_reflection_never_lowers_a_tier_that_is_already_cautious_enough():
    """The tier check is symmetric on paper but the Emergency Department floor
    for P1/P2 means a consistent high tier is never pulled down."""
    result, state = run_and_check(
        ReflectionAgent(),
        make_case(acuity_code="P2_EMERGENT", care_tier="Emergency Department", escalated=True, confidence=0.9),
    )
    assert result["passed"] is True
    assert state.care_tier == "Emergency Department"
    assert result["corrections"] == []


def test_reflection_leaves_a_tier_that_is_more_cautious_than_the_acuity_floor():
    """The real asymmetry: a P4 case sitting on an Emergency Department tier.

    `tier_for_acuity(P4)` is GP, so a plain equality check called this
    "inconsistent" and re-routed — DOWN, from Emergency Department to GP. That
    contradicts this agent's escalation-only invariant (docstring / CAPABILITY):
    an upstream decision to be more cautious than the acuity floor is exactly
    what a critic must not undo, and it is not an issue to report either.
    """
    result, state = run_and_check(
        ReflectionAgent(),
        make_case(
            acuity_code="P4_NON_URGENT", care_tier="Emergency Department", escalated=True,
            clinic="Singapore General Hospital", wait_time_min=0,
            confidence=0.9, evidence=["itchy rash"],
        ),
    )
    assert state.care_tier == "Emergency Department"
    assert state.clinic == "Singapore General Hospital"
    assert result["corrections"] == []
    assert not any("inconsistent with acuity" in issue for issue in result["issues"])
    assert result["passed"] is True


def test_reflection_clears_navigation_state_when_it_reroutes():
    """A re-route abandons the clinic the route was computed for.

    `route_instructions` / geometry / clinic coordinates all describe the OLD
    destination, and the API response and the map render straight from them —
    so leaving them behind sends the patient turn-by-turn directions to a clinic
    the pipeline just decided against.
    """
    _result, state = run_and_check(
        ReflectionAgent(),
        make_case(
            acuity_code="P1_RESUSCITATION", care_tier="GP", escalated=True, confidence=0.95,
            clinic="Sunrise Family Clinic", wait_time_min=12,
            travel_estimate_source="onemap_route",
            route_instructions=["Walk to Sunrise Family Clinic: about 900 m (12 min)."],
            route_available=True,
            route_geometry=[[1.3, 103.8], [1.31, 103.81]],
            clinic_latitude=1.31, clinic_longitude=103.81,
            effective_transport_mode="walk",
            routing_plan={"requested_transport": "walk", "replanned": False},
            alternative_clinics=[{"clinic_id": "chas-100001-sunrise", "name": "Other Clinic"}],
            routing_clarification={"kind": "transport_mode", "question": "How will you travel?"},
        ),
    )
    assert state.care_tier == "Emergency Department"
    assert state.clinic != "Sunrise Family Clinic"
    assert state.route_available is False
    assert state.route_instructions == []
    assert state.route_geometry == []
    assert state.clinic_latitude is None
    assert state.clinic_longitude is None
    assert state.travel_estimate_source == "not_applicable"
    assert state.effective_transport_mode is None
    assert state.routing_plan == {}
    assert state.alternative_clinics == []
    assert state.routing_clarification is None


# ---------------------------------------------------------------------------
# Consistency check 2: escalation is monotone and the upstream reason is kept.
# ---------------------------------------------------------------------------
def test_reflection_appends_to_an_existing_escalation_reason():
    """Forcing escalation must not erase what an upstream agent already said."""
    _, state = run_and_check(
        ReflectionAgent(),
        make_case(
            acuity_code="P1_RESUSCITATION",
            care_tier="Emergency Department",
            escalated=False,
            escalation_reason="Safety: chest pain red flag.",
        ),
    )
    assert state.escalated is True
    assert state.escalation_reason.startswith("Safety: chest pain red flag.")
    assert "Reflection:" in state.escalation_reason


def test_reflection_never_unescalates_a_low_acuity_case():
    """A P5 case escalated upstream stays escalated: the critic is escalation-only."""
    result, state = run_and_check(
        ReflectionAgent(),
        make_case(
            acuity_code="P5_SELF_CARE", care_tier="Telehealth", escalated=True,
            escalation_reason="HITL: patient asked for a clinician.", confidence=0.9,
            evidence=["mild cold symptoms"],
        ),
    )
    assert result["passed"] is True
    assert state.escalated is True
    assert state.escalation_reason == "HITL: patient asked for a clinician."


# ---------------------------------------------------------------------------
# Evaluator-Optimizer signal: rerun_suggested is advisory and never mutates.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("evidence", "confidence"),
    [
        ([], 0.9),                                  # no evidence at all
        (["no clear severity signal"], 0.9),        # explicit "nothing found" marker
        (["chest pain"], 0.3),                      # very low confidence
    ],
    ids=["empty-evidence", "no-signal-marker", "low-confidence"],
)
def test_reflection_suggests_a_rerun_for_thin_evidence_or_low_confidence(evidence, confidence):
    result, state = run_and_check(
        ReflectionAgent(),
        make_case(
            acuity_code="P4_NON_URGENT", care_tier="GP", escalated=False,
            evidence=evidence, confidence=confidence,
        ),
    )
    assert result["rerun_suggested"] is True
    # A rerun suggestion is a signal to the Supervisor, not a decision: the
    # case is still internally consistent and nothing about it was changed.
    assert result["passed"] is True
    assert state.acuity_code == "P4_NON_URGENT"
    assert state.escalated is False


def test_reflection_does_not_suggest_a_rerun_for_a_confident_well_evidenced_case():
    result, _ = run_and_check(
        ReflectionAgent(),
        make_case(
            acuity_code="P4_NON_URGENT", care_tier="GP", escalated=False,
            evidence=["itchy rash"], confidence=0.82,
        ),
    )
    assert result["rerun_suggested"] is False


# ---------------------------------------------------------------------------
# Output contract and the A2A attribution check inside run().
# ---------------------------------------------------------------------------
def test_reflection_result_carries_the_declared_contract_and_mirrors_state():
    result, state = run_and_check(
        ReflectionAgent(),
        make_case(acuity_code="P3_URGENT", care_tier="Urgent Care", escalated=False, confidence=0.8),
    )
    assert set(result) == {"source", "passed", "issues", "corrections", "rerun_suggested"}
    assert result["source"] == "deterministic"
    assert state.reflection == {k: v for k, v in result.items() if k != "source"}


def test_reflection_run_reports_an_unannounced_routing_decision_as_an_issue():
    """With a bus inbox but no `care.routed` message, run() fails the case even
    though CaseState alone looks consistent — attribution is part of the verdict."""
    from app.agents import AgentMessage

    agent = ReflectionAgent()
    agent.consume([])
    result, _ = run_and_check(
        agent, make_case(acuity_code="P3_URGENT", care_tier="Urgent Care", escalated=False, confidence=0.8),
    )
    assert result["passed"] is False
    assert any("never announced" in issue for issue in result["issues"])

    # And the same case passes when Care-Routing did announce the tier it set.
    announced = ReflectionAgent()
    announced.consume([AgentMessage(
        sender="routing", recipient="broadcast", intent="care.routed",
        payload={"care_tier": "Urgent Care", "clinic": "C", "wait_time_min": 10},
    )])
    result2, _ = run_and_check(
        announced, make_case(acuity_code="P3_URGENT", care_tier="Urgent Care", escalated=False, confidence=0.8),
    )
    assert result2["passed"] is True


def test_malformed_loop_caps_fall_back_to_the_defaults_instead_of_killing_import(monkeypatch):
    """The two loop caps are read from the environment at IMPORT time.

    Parsed with a bare `int()`/`float()`, a typo in a compose file — `"one"`, a
    trailing space, an empty string — raised ValueError while `app.agents` was
    being imported, so the entire API failed to start over a demo knob whose
    default is already the safe value. Same fallback behaviour as config.py.
    """
    import importlib

    from app.agents import reflection as reflection_module

    monkeypatch.setenv("CAREROUTE_REFLECTION_MAX_ITERS", "one")
    monkeypatch.setenv("CAREROUTE_REFLECTION_BUDGET_MS", "")
    try:
        reloaded = importlib.reload(reflection_module)
        assert reloaded.REFLECTION_MAX_ITERS == 1
        assert reloaded.REFLECTION_BUDGET_MS == 4000.0
    finally:
        monkeypatch.undo()
        importlib.reload(reflection_module)


def test_reflection_emit_payload_lists_the_issues_it_found():
    msg = emit_and_check(
        ReflectionAgent(),
        make_case(acuity_code="P1_RESUSCITATION", care_tier="GP", escalated=False),
    )
    assert msg.payload["passed"] is False
    assert len(msg.payload["issues"]) >= 2   # tier mismatch + missing escalation


import asyncio as _asyncio

from app.agents import ReflectionAgent as _ReflectionAgent
from app.agents.base import CaseState as _CaseState


@pytest.mark.asyncio
async def test_the_critic_is_bounded_by_the_timeout_it_is_given(monkeypatch):
    """The orchestrator's reflection budget is a few seconds; the critic's own
    ceiling is 90 s and was the only bound, so one reflection step could take
    two critiques. `areason` accepts the remaining budget."""
    agent = _ReflectionAgent()
    monkeypatch.setattr(agent, "critic_enabled", lambda: True)

    async def _slow(_state):
        await _asyncio.sleep(5)
        return {"action": "accept", "critique": "", "issues": []}

    monkeypatch.setattr(agent, "_critique", _slow)
    state = _CaseState(raw_text="mild headache")
    loop = _asyncio.get_running_loop()
    started = loop.time()
    assert await agent.areason(state, timeout_s=0.05) is None
    assert loop.time() - started < 2


def test_the_critique_text_stays_out_of_the_escalation_reason():
    """The critique is model text grounded in the patient's words; it belongs
    in state.reflection, not in the audited escalation reason."""
    agent = _ReflectionAgent()
    state = _CaseState(raw_text="mild headache", normalised_symptoms="mild headache",
                       acuity_code="P4_NON_URGENT", confidence=0.6, evidence=["headache"])  # below the critic grounding gate
    state.critic = {"action": "escalate", "critique": "the patient's own words verbatim", "issues": []}
    agent.run(state)
    assert state.escalated is True
    assert "own words verbatim" not in (state.escalation_reason or "")
