"""[Microservices] The wire format between the intake-gateway and agent containers."""
from __future__ import annotations

import json

import pytest

from app.agents.base import CaseState
from app.agents.messaging import AgentMessage
from app.microservices import wire


def test_state_round_trips_through_json():
    state = CaseState(raw_text="chest pain", intake_keywords=["chest"], route_geometry=[[1.3, 103.8]])
    data = json.loads(json.dumps(wire.state_to_wire(state)))
    assert wire.state_from_wire(data) == state


def test_unknown_state_field_is_rejected():
    data = wire.state_to_wire(CaseState(raw_text="x"))
    data["not_a_field"] = 1
    with pytest.raises(wire.WireError):
        wire.state_from_wire(data)


def test_message_round_trips():
    msg = AgentMessage(sender="hitl", recipient="reflection", intent="case.escalated", payload={"a": 1}, seq=4)
    assert wire.message_from_wire(json.loads(json.dumps(wire.message_to_wire(msg)))) == msg


def test_state_patch_holds_only_changed_fields():
    state = CaseState(raw_text="x")
    before = wire.state_to_wire(state)
    state.escalated = True
    state.escalation_reason = "why"
    assert wire.state_patch(before, state) == {"escalated": True, "escalation_reason": "why"}


def test_apply_patch_writes_inside_the_lane():
    state = CaseState(raw_text="x")
    wire.apply_patch(state, {"escalated": True}, allowed=frozenset({"escalated"}), agent="hitl")
    assert state.escalated is True


def test_apply_patch_outside_the_lane_changes_nothing():
    state = CaseState(raw_text="x")
    with pytest.raises(wire.WireError, match="outside its declared lane"):
        wire.apply_patch(state, {"escalated": True, "acuity_code": "P1_RESUSCITATION"},
                         allowed=frozenset({"escalated"}), agent="hitl")
    assert state.escalated is False
    assert state.acuity_code == "P3_URGENT"
