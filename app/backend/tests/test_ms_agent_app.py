"""[Microservices] One agent container answers exactly as the in-process worker would."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import config, metrics
from app.agents import HumanInTheLoopAgent, SafetyOverrideAgent, SeverityClassifierAgent
from app.agents.base import CaseState
from app.microservices import wire
from app.microservices.agent_app import build_agent_app
from app.microservices.common import _READY, mark_ready

CHEST_PAIN = "crushing chest pain radiating to my left arm and sweating"


def _invoke(client, op, state, **extra):
    body = {"op": op, "case_id": "case-t", "state": wire.state_to_wire(state), **extra}
    return client.post("/v1/invoke", json=body)


def test_hitl_run_patch_matches_local_worker():
    state = CaseState(raw_text="mild headache", confidence=0.3)
    local_state = CaseState(raw_text="mild headache", confidence=0.3)
    before = wire.state_to_wire(local_state)
    local_result = HumanInTheLoopAgent().run(local_state)

    reply = _invoke(TestClient(build_agent_app("hitl")), "run", state)

    assert reply.status_code == 200
    body = reply.json()
    assert body["state_patch"] == wire.state_patch(before, local_state)
    assert body["result"]["escalated"] == local_result["escalated"]


def test_classifier_run_is_awaited():
    reply = _invoke(TestClient(build_agent_app("classifier")), "run", CaseState(raw_text=CHEST_PAIN))
    assert reply.status_code == 200
    assert "acuity_code" in reply.json()["result"]


def test_safety_emit_rebuilds_the_announcement_from_carry():
    local = SafetyOverrideAgent()
    local_state = CaseState(raw_text=CHEST_PAIN)
    local.run(local_state)
    expected = local.emit(local_state)

    client = TestClient(build_agent_app("safety"))
    state = CaseState(raw_text=CHEST_PAIN)
    run = _invoke(client, "run", state).json()
    wire.apply_patch(state, run["state_patch"], allowed=SafetyOverrideAgent.CONTRACT.writes, agent="safety")
    emitted = _invoke(client, "emit", state, carry=run["carry"]).json()

    assert emitted["messages"][0]["intent"] == expected.intent
    assert emitted["messages"][0]["payload"] == expected.payload


def test_safety_prescreen_returns_a_bool():
    reply = _invoke(TestClient(build_agent_app("safety")), "prescreen", CaseState(raw_text=CHEST_PAIN))
    assert reply.json()["result"] is SafetyOverrideAgent().prescreen(CHEST_PAIN)


def test_unknown_op_is_404():
    assert _invoke(TestClient(build_agent_app("hitl")), "areason", CaseState(raw_text="x")).status_code == 404


def test_bad_state_is_422():
    client = TestClient(build_agent_app("hitl"))
    reply = client.post("/v1/invoke", json={"op": "run", "state": {"raw_text": "x", "bogus": 1}})
    assert reply.status_code == 422


def test_internal_token_is_enforced_when_configured(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_TOKEN", "s3cret")
    client = TestClient(build_agent_app("hitl"))
    assert _invoke(client, "run", CaseState(raw_text="x")).status_code == 401
    ok = client.post("/v1/invoke", headers={"X-Internal-Token": "s3cret"},
                     json={"op": "run", "state": wire.state_to_wire(CaseState(raw_text="x"))})
    assert ok.status_code == 200


def test_capability_card_and_probes():
    client = TestClient(build_agent_app("classifier"))
    assert client.get("/v1/capability").json()["slug"] == "classifier"
    assert client.get("/health").json()["status"] == "ok"
    metrics_reply = client.get("/metrics")
    assert metrics_reply.status_code == 200
    body = metrics_reply.text
    if metrics.enabled():
        assert "careroute_" in body
    else:
        assert "metrics disabled" in body


def test_ready_before_and_after_mark_ready():
    client = TestClient(build_agent_app("hitl"))
    assert "hitl" not in _READY
    assert client.get("/ready").status_code == 503
    mark_ready("hitl")
    try:
        assert client.get("/ready").status_code == 200
    finally:
        _READY.discard("hitl")


@pytest.mark.parametrize("slug", ["classifier", "safety", "routing", "reflection", "hitl", "handoff"])
def test_every_agent_builds(slug):
    assert build_agent_app(slug).title == f"careroute-{slug}-agent"


def test_classifier_worker_is_the_real_class():
    from app.microservices.workers import worker_factory
    assert isinstance(worker_factory("classifier")(), SeverityClassifierAgent)
    assert asyncio.iscoroutinefunction(SeverityClassifierAgent.run)
