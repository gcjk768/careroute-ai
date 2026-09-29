"""[Agentic] P3_URGENT gets a concrete destination, never a placeholder.

Until 2026-09-24 only P4 got a real clinic: P3 returned "Nearest urgent-care
facility (directory unavailable)". P3 now gets the nearest option that is
SAFE to send an urgent patient to: a CHAS GP whose reviewed hours say open now
AND were verified recently, or else a reviewed public 24-hour ED (always open).
Unknown or stale hours are not good enough for urgent care. P1/P2/P4 are
unchanged, and the tier is never lowered.
"""
import asyncio
from datetime import datetime

import pytest

from app.agents import CareRoutingAgent, CaseState
from app.services.onemap import SINGAPORE_TIMEZONE
from app.tools.clinic_lookup import Clinic
from app.tools.gpgowhere import HoursProfile

pytestmark = pytest.mark.routing

TODAY = datetime.now(SINGAPORE_TIMEZONE).date().isoformat()
# 1.3521,103.8198 (central Singapore): TTSH (~5 km) is the nearest reviewed public ED.
NEAR = (1.3521, 103.8198)
NEAR_GP = Clinic("Near Clinic", "1 Test Road", "100001", "60000001", 1.3525, 103.8200, ["CHAS"])


class _Lookup:
    def __init__(self, clinics=(NEAR_GP,), fail=False):
        self.clinics, self.fail, self.calls = list(clinics), fail, 0

    def find_candidate_clinics(self, *_args, **_kwargs):
        self.calls += 1
        if self.fail:
            raise ConnectionError("directory down")
        return self.clinics


class _Hours:
    def __init__(self, *, open_now=True, verified_at=TODAY):
        self.open_now, self.verified_at = open_now, verified_at

    def match(self, *, postal, name):
        return HoursProfile(postal=postal, name=name, hours={}, twenty_four_hours=self.open_now,
                            verified_at=self.verified_at)


def _state(code="P3_URGENT", **extra):
    values = {"raw_text": "synthetic urgent case", "acuity_code": code,
              "latitude": NEAR[0], "longitude": NEAR[1], "transport_mode": "walk"}
    values.update(extra)
    return CaseState(**values)


def _run(state, lookup=None, hours=None):
    agent = CareRoutingAgent(lookup=lookup or _Lookup(), hours=hours or _Hours(), use_onemap=False)
    return asyncio.run(agent.run(state))


def test_p3_selects_a_nearer_verified_open_gp():
    state = _state()
    result = _run(state)
    assert result["care_tier"] == "Urgent Care"
    assert result["clinic"] == "Near Clinic"
    assert result["clinic_latitude"] == NEAR_GP.latitude
    plan = result["routing_plan"]["urgent_destination"]
    assert plan["kind"] == "gp_verified_open" and plan["hours_freshness"] == "fresh"
    # A travel estimate for the chosen place, not the 45-minute placeholder.
    assert result["travel_estimate_source"] == "geodesic_speed_estimate"
    assert result["wait_time_min"] == state.wait_time_min >= 1
    assert result["one_map_url"]


def test_p3_skips_a_closed_gp_and_falls_to_the_nearest_public_ed():
    result = _run(_state(), hours=_Hours(open_now=False))
    assert result["clinic"] == "Tan Tock Seng Hospital"
    assert result["routing_plan"]["urgent_destination"]["kind"] == "public_24h_ed"
    assert result["care_tier"] == "Urgent Care"


def test_p3_does_not_trust_stale_or_unknown_hours():
    for verified_at in ("2020-01-01", "not-a-date"):
        result = _run(_state(), hours=_Hours(verified_at=verified_at))
        assert result["routing_plan"]["urgent_destination"]["kind"] == "public_24h_ed", verified_at


def test_p3_directory_outage_still_gives_a_public_ed():
    lookup = _Lookup(fail=True)
    result = _run(_state(), lookup=lookup)
    assert lookup.calls == 1
    assert result["clinic"] == "Tan Tock Seng Hospital"


def test_p3_nearer_ed_beats_a_farther_open_gp():
    far_gp = Clinic("Far Clinic", "9 Far Road", "700001", "60000009", 1.44, 103.70, ["CHAS"])
    result = _run(_state(), lookup=_Lookup([far_gp]))
    assert result["clinic"] == "Tan Tock Seng Hospital"


def test_p3_without_transport_asks_one_logistics_question_but_keeps_the_destination():
    result = _run(_state(transport_mode="unknown"))
    assert result["clinic"] == "Near Clinic"
    assert result["routing_clarification"]["kind"] == "transport_mode"
    assert result["travel_estimate_source"] == "not_applicable"


def test_p3_without_location_keeps_the_honest_placeholder():
    result = _run(_state(latitude=None, longitude=None))
    assert result["clinic"] == "Nearest urgent-care facility (directory unavailable)"
    assert result["wait_time_min"] == 45
    assert "urgent_destination" not in result["routing_plan"]


@pytest.mark.parametrize("code,tier,clinic", [
    ("P1_RESUSCITATION", "Emergency Department", "Call 995 for SCDF emergency dispatch"),
    ("P2_EMERGENT", "Emergency Department", "Tan Tock Seng Hospital"),
])
def test_p1_p2_keep_their_emergency_path(code, tier, clinic):
    lookup = _Lookup()
    result = _run(_state(code), lookup=lookup)
    assert result["care_tier"] == tier and result["clinic"] == clinic
    assert lookup.calls == 0, "emergency tiers never consult the GP directory"
    assert "urgent_destination" not in result["routing_plan"]


def test_a_safety_override_to_p2_is_never_downgraded_to_the_p3_gp():
    from app.agents import AgentMessage

    agent = CareRoutingAgent(lookup=_Lookup(), hours=_Hours(), use_onemap=False)
    agent.consume([AgentMessage(sender="safety", recipient="broadcast", intent="safety.override",
                                payload={"triggered": True, "forced_acuity": "P2_EMERGENT"})])
    result = asyncio.run(agent.run(_state()))
    assert result["care_tier"] == "Emergency Department"
    assert result["clinic"] == "Tan Tock Seng Hospital"


def test_p4_is_unchanged_and_still_selects_a_gp():
    result = _run(_state("P4_NON_URGENT"))
    assert result["care_tier"] == "GP" and result["clinic"] == "Near Clinic"
    assert "urgent_destination" not in result["routing_plan"]
