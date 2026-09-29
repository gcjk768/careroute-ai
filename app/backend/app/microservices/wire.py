"""[Microservices] Wire format between the intake-gateway and agent containers.

CaseState and AgentMessage are plain dataclasses, so the wire format is their
fields as JSON. Both ends import this module, so they cannot drift apart.

`apply_patch` is the network-side twin of tests/agents/test_contracts.py: a
reply may only write the CaseState fields in that agent's CONTRACT.writes. It
validates the whole patch before touching the state, so a rejected reply
changes nothing.
"""
from __future__ import annotations

from dataclasses import asdict, fields

from ..agents.base import CaseState
from ..agents.messaging import AgentMessage

_CASE_FIELDS = frozenset(f.name for f in fields(CaseState))


class WireError(ValueError):
    """A payload that is not what it claims to be, or that breaks a lane."""


def state_to_wire(state: CaseState) -> dict:
    return asdict(state)


def state_from_wire(data: dict) -> CaseState:
    unknown = set(data) - _CASE_FIELDS
    if unknown:
        raise WireError(f"unknown CaseState fields: {sorted(unknown)}")
    return CaseState(**data)


def message_to_wire(message: AgentMessage) -> dict:
    return message.to_dict()


def message_from_wire(data: dict) -> AgentMessage:
    return AgentMessage(
        sender=str(data["sender"]),
        recipient=str(data["recipient"]),
        intent=str(data["intent"]),
        payload=dict(data.get("payload") or {}),
        seq=int(data.get("seq", 0)),
    )


def state_patch(before: dict, after: CaseState) -> dict:
    """The fields whose value changed, as {field: new value}."""
    return {key: value for key, value in asdict(after).items() if before.get(key) != value}


def apply_patch(state: CaseState, patch: dict, *, allowed: frozenset[str], agent: str) -> None:
    unknown = set(patch) - _CASE_FIELDS
    if unknown:
        raise WireError(f"{agent} returned unknown CaseState fields: {sorted(unknown)}")
    outside = set(patch) - allowed
    if outside:
        raise WireError(f"{agent} wrote outside its declared lane: {sorted(outside)}")
    for key, value in patch.items():
        setattr(state, key, value)
