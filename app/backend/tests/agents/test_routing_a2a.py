"""Readable, deterministic A2A verification for the Care Routing handoff.

Run only this suite with:
    python -m pytest tests/agents/test_routing_a2a.py -s -v

The agents and A2A protocol are real. Directory, hours, and map adapters are
small deterministic fixtures so the tests are fast, repeatable, and do not
spend money or expose patient data to external providers. The complementary
``test_safety_routing_a2a_live.py`` exercises real CHAS/OneMap/OpenAI.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from app.agents import (
    AgentMessage,
    CareRoutingAgent,
    CaseState,
    MessageBus,
    ReflectionAgent,
    SafetyOverrideAgent,
)
from app.agents.supervisor import Supervisor
from app.tools.clinic_lookup import Clinic
from app.tools.gpgowhere import HoursProfile

pytestmark = [pytest.mark.comms, pytest.mark.routing]


@dataclass
class DirectoryFixture:
    clinics: list[Clinic]
    calls: int = 0

    def find_candidate_clinics(self, *_args, **_kwargs) -> list[Clinic]:
        self.calls += 1
        return self.clinics


class AlwaysOpenHours:
    def match(self, *, postal: str, name: str) -> HoursProfile:
        return HoursProfile(postal=postal, name=name, hours={}, twenty_four_hours=True, verified_at="synthetic")


@dataclass
class MapFixture:
    calls: int = 0

    def route(self, *_args, **_kwargs) -> dict:
        self.calls += 1
        return {
            "distance": 1200,
            "time": 720,
            "instructions": [
                ["Straight", "Thomson Road", 0, "", 0, "120m", "East", "North", "walk", "Head east"],
                ["Left", "Marymount Lane", 0, "", 0, "75m", "North", "West", "walk", "Turn left"],
                ["Left", "", 0, "", 0, "0m", "West", "West", "walk", "Destination on the left"],
            ],
            "geometry": [[1.3, 103.8], [1.30025, 103.8002], [1.3006, 103.8005]],
        }


def _clinics(count: int = 6, *, latitude: float = 1.3005) -> list[Clinic]:
    return [
        Clinic(
            f"Trace Clinic {number}", f"{number} Test Road", f"1000{number:02d}", "60000001",
            latitude + number / 10_000, 103.8005, ["CHAS"],
        )
        for number in range(1, count + 1)
    ]


def _trace(*, scenario: str, state: CaseState, bus: MessageBus, safety: dict, protocol_issues: list[str],
           routing: dict, directory: DirectoryFixture, maps: MapFixture) -> None:
    """Print a safe, synthetic, easy-to-compare A2A execution record."""
    print("\n[A2A CARE ROUTING TRACE]")
    print(json.dumps({
        "scenario": scenario,
        "input_state": {
            "initial_acuity": state.prior_acuity_code,
            "final_acuity_after_safety": state.acuity_code,
            "location": {"latitude": state.latitude, "longitude": state.longitude}
            if state.latitude is not None else None,
            "transport_mode": state.transport_mode,
            "max_travel_time_min": state.max_travel_time_min,
        },
        "a2a_message_history": bus.history(),
        "safety_reasoning": {
            "triggered": safety["triggered"],
            "rule": safety["rule"],
            "forced_acuity": safety["forced_acuity"],
            "protocol_issues": protocol_issues,
        },
        "routing_reasoning": {
            "source": routing["source"],
            "candidate_count": routing.get("candidate_count", 0),
            "directory_calls": directory.calls,
            "map_calls": maps.calls,
            "travel_estimate_source": routing.get("travel_estimate_source"),
        },
        "routing_output": routing,
    }, indent=2, default=str))


def _run_real_safety_to_routing(
    *, case_text: str, classifier_acuity: str, latitude: float | None = None,
    longitude: float | None = None, max_travel: int | None = None,
    clinics: list[Clinic] | None = None,
) -> tuple[CaseState, MessageBus, dict, list[str], dict, DirectoryFixture, MapFixture]:
    """Execute the same Safety request/response gate used by Supervisor."""
    state = CaseState(
        raw_text=case_text,
        acuity_code=classifier_acuity,
        confidence=0.8,
        latitude=latitude,
        longitude=longitude,
        transport_mode="walk",
        max_travel_time_min=max_travel,
    )
    directory = DirectoryFixture(clinics if clinics is not None else _clinics())
    maps = MapFixture()
    bus = MessageBus()
    supervisor = Supervisor()

    classifier_message = bus.publish(AgentMessage(
        sender="classifier", recipient="broadcast", intent="acuity.classified",
        payload={"acuity_code": classifier_acuity, "confidence": state.confidence, "evidence": ["synthetic QA"]},
    ))
    safety_request = bus.publish(supervisor._safety_request(state, classifier_message.seq))
    safety_agent = SafetyOverrideAgent()
    safety_agent.consume(bus.inbox(safety_agent))
    safety_result = safety_agent.run(state)
    bus.publish(safety_agent.emit(state))
    protocol_issues = supervisor._verify_safety_response(bus, state, safety_request.seq)

    routing_agent = CareRoutingAgent(lookup=directory, hours=AlwaysOpenHours(), maps=maps)
    routing_agent.consume(bus.inbox(routing_agent))
    routing_result = asyncio.run(routing_agent.run(state))
    bus.publish(routing_agent.emit(state))
    return state, bus, safety_result, protocol_issues, routing_result, directory, maps


@pytest.mark.parametrize(
    "acuity,expected_tier",
    [
        ("P1_RESUSCITATION", "Emergency Department"),
        ("P2_EMERGENT", "Emergency Department"),
        ("P3_URGENT", "Urgent Care"),
        ("P5_SELF_CARE", "Telehealth"),
    ],
)
def test_a2a_non_gp_acuities_skip_gp_tools(acuity, expected_tier):
    """P1/P2/P3/P5 preserve the final tier and never call GP/map tools."""
    result = _run_real_safety_to_routing(case_text="Synthetic QA: no red-flag phrase.", classifier_acuity=acuity)
    state, bus, safety, issues, routing, directory, maps = result
    _trace(scenario=f"{acuity}_non_gp", state=state, bus=bus, safety=safety, protocol_issues=issues,
           routing=routing, directory=directory, maps=maps)

    assert issues == []
    assert safety["triggered"] is False
    assert routing["care_tier"] == expected_tier
    assert routing["source"] == "safety_floor"
    assert directory.calls == 0
    assert maps.calls == 0


def test_a2a_safety_override_replaces_stale_gp_before_routing():
    """A real chest-pain red flag forces P1 and prevents GP/OneMap calls."""
    result = _run_real_safety_to_routing(
        case_text="Synthetic QA: sudden crushing chest pain spreading to the left arm with sweating.",
        classifier_acuity="P4_NON_URGENT",
    )
    state, bus, safety, issues, routing, directory, maps = result
    _trace(scenario="safety_override_p4_to_p1", state=state, bus=bus, safety=safety, protocol_issues=issues,
           routing=routing, directory=directory, maps=maps)

    assert issues == []
    assert safety["triggered"] is True
    assert safety["forced_acuity"] == "P1_RESUSCITATION"
    assert routing["care_tier"] == "Emergency Department"
    assert routing["source"] == "safety_floor"
    assert directory.calls == 0
    assert maps.calls == 0


def test_a2a_gp_location_shortlists_five_candidates_and_returns_selected_route():
    """A P4 location case uses five routed candidates, then returns one safe destination."""
    result = _run_real_safety_to_routing(
        case_text="Synthetic QA: mild rash for two days, no fever.",
        classifier_acuity="P4_NON_URGENT", latitude=1.3, longitude=103.8, max_travel=45,
    )
    state, bus, safety, issues, routing, directory, maps = result
    _trace(scenario="p4_location_top_five", state=state, bus=bus, safety=safety, protocol_issues=issues,
           routing=routing, directory=directory, maps=maps)

    assert issues == []
    assert safety["triggered"] is False
    assert routing["care_tier"] == "GP"
    assert routing["candidate_count"] == 5
    assert directory.calls == 1
    assert maps.calls == 5
    assert routing["clinic"] == "Trace Clinic 1"
    assert routing["route_available"] is True
    assert routing["clinic_latitude"] == pytest.approx(1.3006)
    assert routing["clinic_longitude"] == pytest.approx(103.8005)
    assert routing["one_map_url"] == "https://www.onemap.gov.sg/?lat=1.300600&lng=103.800500&zoom=17"
    assert routing["route_geometry"] == [[1.3, 103.8], [1.30025, 103.8002], [1.3006, 103.8005]]
    assert routing["requested_transport"] == "walk"
    assert routing["effective_transport"] == "walk"
    assert routing["routing_plan"]["replanned"] is False
    assert len(routing["alternative_clinics"]) == 2
    assert {"clinic_id", "name", "travel_time_min", "open_status"} <= routing["alternative_clinics"][0].keys()
    assert routing["route_instructions"] == [
        "Walk to Trace Clinic 1: about 1.20 km (12 min).",
        "Head east on Thomson Road and continue for 120m.",
        "Turn left onto Marymount Lane and continue for 75m.",
        "Arrive at Trace Clinic 1 — 1 Test Road.",
    ]
    assert routing["travel_estimate_source"] == "onemap_route"

    # The outgoing routing message is intentionally logistics-only. Reflection
    # receives it and confirms it agrees with the final shared state.
    published = bus.history()[-1]
    assert published["intent"] == "care.routed"
    assert "mild rash" not in repr(published["payload"])
    reflection = ReflectionAgent()
    reflection.consume(bus.inbox(reflection))
    assert reflection.verify_announcements(state) == []


def test_a2a_gp_without_location_returns_explicit_safe_fallback():
    """A P4 case without coordinates never claims a nearest clinic or calls maps."""
    result = _run_real_safety_to_routing(
        case_text="Synthetic QA: mild rash for two days, no fever.", classifier_acuity="P4_NON_URGENT",
    )
    state, bus, safety, issues, routing, directory, maps = result
    _trace(scenario="p4_missing_location", state=state, bus=bus, safety=safety, protocol_issues=issues,
           routing=routing, directory=directory, maps=maps)

    assert issues == []
    assert routing["source"] == "fallback"
    assert routing["travel_estimate_source"] == "unavailable"
    assert directory.calls == 0
    assert maps.calls == 0


def test_a2a_gp_travel_limit_prevents_out_of_range_clinic_selection():
    """A P4 travel cap filters clinics before any map or LLM action can occur."""
    far_clinics = _clinics(1, latitude=1.35)
    result = _run_real_safety_to_routing(
        case_text="Synthetic QA: mild rash for two days, no fever.", classifier_acuity="P4_NON_URGENT",
        latitude=1.3, longitude=103.8, max_travel=1, clinics=far_clinics,
    )
    state, bus, safety, issues, routing, directory, maps = result
    _trace(scenario="p4_travel_limit", state=state, bus=bus, safety=safety, protocol_issues=issues,
           routing=routing, directory=directory, maps=maps)

    assert issues == []
    assert routing["source"] == "fallback"
    assert maps.calls == 0
