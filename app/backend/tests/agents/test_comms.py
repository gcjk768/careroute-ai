"""Agent-to-agent (A2A) communication — the messaging framework's guard.

Verifies the bus mechanics, the least-privilege COMMS enforcement, pub/sub
delivery, and that EVERY agent only publishes intents it declared. Run with:
pytest -m comms
"""
import asyncio
import json

import pytest

from app.agents import (
    AgentMessage,
    CaseState,
    ConsumesMessages,
    ReflectionAgent,
    CareRoutingAgent,
    CommsAccessError,
    MessageBus,
    SafetyOverrideAgent,
    SeverityClassifierAgent,
    SymptomIntakeAgent,
    enforce_comms,
)
from app.tools.clinic_lookup import Clinic
from app.tools.gpgowhere import HoursProfile
from harness import AGENT_CLASSES, emit_and_check, make_case

pytestmark = pytest.mark.comms


# --------------------------------------------------------------------------
# Bus mechanics
# --------------------------------------------------------------------------
def test_bus_assigns_monotonic_seq_and_records_history():
    bus = MessageBus()
    bus.publish(AgentMessage(sender="a", recipient="b", intent="x", payload={}))
    bus.publish(AgentMessage(sender="b", recipient="c", intent="y", payload={}))
    seqs = [m["seq"] for m in bus.history()]
    assert seqs == [0, 1]
    assert len(bus) == 2


def test_bus_inbox_delivers_only_subscribed_intents():
    bus = MessageBus()
    # Classifier's acuity broadcast: routing subscribes to it, intake does not.
    bus.publish(AgentMessage(sender="classifier", recipient="broadcast", intent="acuity.classified", payload={}))
    routing_inbox = [m.intent for m in bus.inbox(CareRoutingAgent())]
    intake_inbox = [m.intent for m in bus.inbox(SymptomIntakeAgent())]
    assert "acuity.classified" in routing_inbox
    assert "acuity.classified" not in intake_inbox


def test_bus_inbox_respects_directed_recipient():
    bus = MessageBus()
    # A message directed to 'classifier' must not land in routing's inbox even if
    # routing subscribed to the intent.
    bus.publish(AgentMessage(sender="intake", recipient="classifier", intent="acuity.classified", payload={}))
    assert bus.inbox(CareRoutingAgent()) == []


# --------------------------------------------------------------------------
# Least-privilege enforcement
# --------------------------------------------------------------------------
def test_enforce_comms_rejects_undeclared_intent():
    with pytest.raises(CommsAccessError):
        enforce_comms(SafetyOverrideAgent(), "acuity.classified")  # safety publishes safety.override only


def test_enforce_comms_allows_declared_intent():
    enforce_comms(SeverityClassifierAgent(), "acuity.classified")  # declared — must not raise


# --------------------------------------------------------------------------
# Every agent only publishes intents it declared (per-agent COMMS interface)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("slug", sorted(AGENT_CLASSES))
def test_agent_emits_only_declared_intents(slug):
    message = emit_and_check(AGENT_CLASSES[slug](), make_case())
    assert message.sender == slug


# ---------------------------------------------------------------------------
# [A2A] The bus must be LOAD-BEARING, not decorative.
#
# These are the tests that answer "do the agents actually communicate?" — they
# fail if an agent's behaviour stops depending on a message it received.
# ---------------------------------------------------------------------------

@pytest.mark.comms
@pytest.mark.parametrize("slug,cls", sorted(AGENT_CLASSES.items()))
def test_every_worker_can_receive(slug, cls):
    """Every worker implements the receive half of the protocol, not just emit()."""
    agent = cls()
    assert isinstance(agent, ConsumesMessages)
    agent.consume([])
    assert agent.received == []
    assert agent._consumed is True


@pytest.mark.comms
def test_received_payload_enforces_the_subscription_declaration():
    """Reading an undeclared intent fails as loudly as publishing one does."""
    agent = ReflectionAgent()
    agent.consume([])
    with pytest.raises(CommsAccessError):
        agent.received_payload("symptoms.normalised")   # not in its subscribes


@pytest.mark.comms
def test_reflection_flags_a_routing_decision_that_was_never_announced():
    """Withhold the message and Reflection raises an issue it cannot otherwise see."""
    state = CaseState(raw_text="x")
    state.care_tier = "GP"
    agent = ReflectionAgent()
    agent.consume([])                       # bus ran, no care.routed arrived
    assert agent.verify_announcements(state) != []


@pytest.mark.comms
def test_reflection_accepts_a_routing_decision_that_matches_its_announcement():
    state = CaseState(raw_text="x")
    state.care_tier = "Urgent Care"
    agent = ReflectionAgent()
    agent.consume([AgentMessage(
        sender="routing", recipient="broadcast", intent="care.routed",
        payload={"care_tier": "Urgent Care", "clinic": "C", "wait_time_min": 10},
    )])
    assert agent.verify_announcements(state) == []


def test_routing_obeys_the_received_safety_override_message():
    """Routing uses the safety A2A handoff instead of stale GP state."""
    agent = CareRoutingAgent(use_onemap=False)
    state = CaseState(raw_text="synthetic", acuity_code="P4_NON_URGENT")
    safety_message = AgentMessage(
        sender="safety", recipient="broadcast", intent="safety.override",
        payload={"triggered": True, "forced_acuity": "P1_RESUSCITATION"},
    )
    bus = MessageBus()
    bus.publish(safety_message)
    agent.consume(bus.inbox(agent))

    result = asyncio.run(agent.run(state))

    # Synthetic trace for manual A2A verification. It contains no patient text,
    # credentials, or external API response: only the routing inputs and the
    # deterministic safety-floor output.
    print("\n[A2A SAFETY -> CARE ROUTING TRACE]")
    print(json.dumps({
        "input_state_before_routing": {
            "acuity_code": "P4_NON_URGENT",
            "location_provided": False,
            "transport_mode": state.transport_mode,
        },
        "received_a2a_message": safety_message.to_dict(),
        "routing_reasoning": (
            "Safety override is authoritative: forced P1 replaces the stale P4 state. "
            "Emergency routing skips GP clinic lookup, OneMap, and LLM selection."
        ),
        "routing_output": result,
        "expected": {
            "care_tier": "Emergency Department",
            "source": "safety_floor",
            "external_tools_called": False,
        },
    }, indent=2, default=str))

    assert result["care_tier"] == "Emergency Department"
    assert result["source"] == "safety_floor"


class _TraceLookup:
    """Synthetic directory so this A2A test never calls an external service."""
    def find_candidate_clinics(self, *_args, **_kwargs):
        return [Clinic("Trace Clinic", "1 Test Road", "100001", "60000001", 1.301, 103.801, ["CHAS"])]


class _TraceHours:
    def match(self, *, postal, name):
        return HoursProfile(postal=postal, name=name, hours={}, twenty_four_hours=True, verified_at="synthetic")


class _TraceMaps:
    def route(self, *_args, **_kwargs):
        return {"distance": 1200, "time": 720, "instructions": [["Straight", "", "Head east"]]}


@pytest.mark.parametrize(
    "acuity_code,expected_tier,has_location",
    [
        ("P1_RESUSCITATION", "Emergency Department", False),
        ("P2_EMERGENT", "Emergency Department", False),
        ("P3_URGENT", "Urgent Care", False),
        ("P4_NON_URGENT", "GP", True),
        ("P5_SELF_CARE", "Telehealth", False),
    ],
)
def test_routing_a2a_trace_for_each_final_acuity(monkeypatch, acuity_code, expected_tier, has_location):
    """Classifier + non-triggered safety handoff yields the fixed final care tier."""
    import app.llm as llm

    async def offline_llm(*_args, **_kwargs):
        raise llm.LLMUnavailableError("synthetic A2A test: use deterministic routing")

    # The purpose here is A2A/tier behaviour, not a paid provider call.
    monkeypatch.setattr(llm, "complete", offline_llm)
    state = CaseState(
        raw_text="synthetic",
        acuity_code=acuity_code,
        latitude=1.3 if has_location else None,
        longitude=103.8 if has_location else None,
        transport_mode="walk",
        max_travel_time_min=45 if has_location else None,
    )
    classifier_message = AgentMessage(
        sender="classifier", recipient="broadcast", intent="acuity.classified",
        payload={"acuity_code": acuity_code, "confidence": 0.8, "evidence": ["synthetic"]},
    )
    safety_message = AgentMessage(
        sender="safety", recipient="broadcast", intent="safety.override",
        payload={"triggered": False, "forced_acuity": None},
    )
    agent = CareRoutingAgent(lookup=_TraceLookup(), hours=_TraceHours(), maps=_TraceMaps())
    bus = MessageBus()
    bus.publish(classifier_message)
    bus.publish(safety_message)
    agent.consume(bus.inbox(agent))
    result = asyncio.run(agent.run(state))

    print("\n[A2A FINAL ACUITY -> CARE ROUTING TRACE]")
    print(json.dumps({
        "input_state": {
            "acuity_code": acuity_code,
            "location": {"latitude": state.latitude, "longitude": state.longitude} if has_location else None,
            "transport_mode": state.transport_mode,
            "max_travel_time_min": state.max_travel_time_min,
        },
        "received_a2a_messages": [classifier_message.to_dict(), safety_message.to_dict()],
        "routing_reasoning": (
            "Safety did not override the classifier, so routing uses the final acuity's fixed tier. "
            "Only P4_GP uses the synthetic clinic, hours, and route tools in this test."
        ),
        "routing_output": result,
        "expected_care_tier": expected_tier,
    }, indent=2, default=str))

    assert result["care_tier"] == expected_tier
    if acuity_code == "P4_NON_URGENT":
        assert result["clinic"] == "Trace Clinic"
        assert result["travel_estimate_source"] == "onemap_route"
        assert result["route_available"] is True
    else:
        assert result["source"] == "safety_floor"

@pytest.mark.comms
def test_reflection_detects_a_tier_changed_without_being_announced():
    """The clobber is invisible in CaseState and visible on the bus."""
    state = CaseState(raw_text="x")
    state.care_tier = "GP"                  # someone rewrote it downstream
    agent = ReflectionAgent()
    agent.consume([AgentMessage(
        sender="routing", recipient="broadcast", intent="care.routed",
        payload={"care_tier": "Emergency Department", "clinic": "C", "wait_time_min": 0},
    )])
    issues = agent.verify_announcements(state)
    assert len(issues) == 1
    assert "without being announced" in issues[0]


@pytest.mark.comms
def test_no_bus_means_no_bus_dependent_checks():
    """Standalone run() (no Supervisor, no bus) is unaffected — additive only."""
    state = CaseState(raw_text="x")
    assert ReflectionAgent().verify_announcements(state) == []


# --------------------------------------------------------------------------
# [Phase 2] Supervisor <-> Safety request/response protocol.
#
# Implements the communication tests in
# docs/plans/safety-override-supervisor-phase-2-handoff.md. The Safety side of
# this (validation, correlation, enriched payload) is Aaron's and is covered in
# test_safety.py; these cover the SUPERVISOR side: that it is permitted to make
# the request, that the request is well-formed and PHI-free, and that a broken
# response fails closed rather than silently proceeding to routing.
# --------------------------------------------------------------------------
from app.agents.supervisor import Supervisor  # noqa: E402


def _request(state=None, seq=2):
    return Supervisor()._safety_request(state or make_case(acuity_code="P3_URGENT"), seq)


def test_supervisor_may_publish_the_safety_request():
    """The declaration must permit it, or enforce_comms would reject it at
    runtime. This is least-privilege for messaging, same as the tool allow-list."""
    assert "safety.assessment.requested" in Supervisor.COMMS.publishes
    assert "safety.override" in Supervisor.COMMS.subscribes
    msg = _request()
    assert msg.intent == "safety.assessment.requested"
    assert msg.recipient == SafetyOverrideAgent.SLUG


def test_safety_subscribes_to_both_halves_of_the_exchange():
    """Safety must hear the classifier's announcement AND the directed request;
    it validates them against each other."""
    subs = SafetyOverrideAgent.COMMS.subscribes
    assert {"acuity.classified", "safety.assessment.requested"} <= subs


def test_request_correlates_to_the_classifier_message():
    """`classifierMessageSeq` is what lets Safety prove the request refers to the
    classification it actually saw, rather than a stale one."""
    msg = _request(seq=7)
    assert msg.payload["classifierMessageSeq"] == 7
    assert msg.payload["classifierAcuity"] == "P3_URGENT"
    assert "classifierConfidence" in msg.payload
    assert "fastPathDetected" in msg.payload


def test_request_carries_no_patient_text():
    """[Privacy] Message payloads are hash-chain audited and streamed to the
    browser, so PHI on the bus is an exposure, not untidiness. Evidence strings
    are excluded too — they quote the patient's own words."""
    state = make_case(raw_text="zzrawmarker crushing chest pain")
    state.normalised_symptoms = "zznormmarker crushing chest pain."
    state.evidence = ["zzevidencemarker"]
    blob = str(_request(state).payload).lower()
    for marker in ("zzrawmarker", "zznormmarker", "zzevidencemarker"):
        assert marker not in blob, f"{marker} reached the safety request payload"


def test_missing_response_triggers_the_failsafe():
    """No response on the bus => force review, never lower acuity."""
    sup, bus = Supervisor(), MessageBus()
    state = make_case()
    state.prior_acuity_code, state.acuity_code = "P3_URGENT", "P1_RESUSCITATION"
    issues = sup._verify_safety_response(bus, state, request_seq=3)
    assert issues, "a missing safety.override must be a protocol issue"
    sup._apply_safety_failsafe(state, issues)
    assert state.escalated is True
    assert state.acuity_code == "P1_RESUSCITATION", "fail-safe must not lower acuity"


def test_stale_response_is_detected():
    """A response correlated to a DIFFERENT request is stale — the case may have
    moved on since, so honouring it would apply an out-of-date verdict."""
    sup, bus = Supervisor(), MessageBus()
    state = make_case()
    state.prior_acuity_code = "P3_URGENT"
    bus.publish(AgentMessage(
        sender="safety", recipient="broadcast", intent="safety.override",
        payload={"requestSeq": 99, "priorAcuity": "P3_URGENT", "triggered": False},
    ))
    issues = sup._verify_safety_response(bus, state, request_seq=3)
    assert any("requestSeq" in i for i in issues), issues


def test_de_escalating_response_is_rejected():
    """Safety is escalation-only. A response that makes the case LESS urgent is a
    protocol violation, not a decision to honour."""
    sup, bus = Supervisor(), MessageBus()
    state = make_case()
    state.prior_acuity_code = "P1_RESUSCITATION"
    bus.publish(AgentMessage(
        sender="safety", recipient="broadcast", intent="safety.override",
        payload={"requestSeq": 3, "priorAcuity": "P1_RESUSCITATION",
                 "forcedAcuity": "P5_SELF_CARE", "triggered": True, "rule": "cardiac_chest_pain"},
    ))
    issues = sup._verify_safety_response(bus, state, request_seq=3)
    assert any("LESS urgent" in i for i in issues), issues


def test_unknown_acuity_code_is_rejected():
    """Model metadata must not be able to introduce an acuity code outside the
    known set. Caught while writing these tests: a plausible-looking but
    non-existent code (`P5_NON_URGENT` — the real one is `P5_SELF_CARE`) is
    rejected before the rank comparison, which would otherwise silently score it
    out of range."""
    sup, bus = Supervisor(), MessageBus()
    state = make_case()
    state.prior_acuity_code = "P3_URGENT"
    bus.publish(AgentMessage(
        sender="safety", recipient="broadcast", intent="safety.override",
        payload={"requestSeq": 3, "priorAcuity": "P3_URGENT",
                 "forcedAcuity": "P5_NON_URGENT", "rule": "cardiac_chest_pain"},
    ))
    issues = sup._verify_safety_response(bus, state, request_seq=3)
    assert any("not a known acuity code" in i for i in issues), issues


def test_rule_outside_the_closed_vocabulary_is_rejected():
    """The forced acuity must come from the rule table, so a rule name that is
    not in RED_FLAG_RULES means something invented one."""
    sup, bus = Supervisor(), MessageBus()
    state = make_case()
    state.prior_acuity_code = "P3_URGENT"
    bus.publish(AgentMessage(
        sender="safety", recipient="broadcast", intent="safety.override",
        payload={"requestSeq": 3, "priorAcuity": "P3_URGENT", "rule": "made_up_emergency"},
    ))
    issues = sup._verify_safety_response(bus, state, request_seq=3)
    assert any("RED_FLAG_RULES" in i for i in issues), issues


def test_sound_response_produces_no_issues():
    """The happy path must stay quiet, or the fail-safe would fire on every case
    and flood the clinician queue — the E5 over-escalation trap."""
    sup, bus = Supervisor(), MessageBus()
    state = make_case()
    state.prior_acuity_code = "P3_URGENT"
    bus.publish(AgentMessage(
        sender="safety", recipient="broadcast", intent="safety.override",
        payload={"requestSeq": 3, "priorAcuity": "P3_URGENT",
                 "forcedAcuity": "P1_RESUSCITATION", "triggered": True,
                 "rule": "cardiac_chest_pain"},
    ))
    assert sup._verify_safety_response(bus, state, request_seq=3) == []
