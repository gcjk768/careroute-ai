"""[Microservices] RemoteAgent: same behaviour as the worker, plus the network checks."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from app import config
from app.agents import HumanInTheLoopAgent
from app.agents.base import AgentUnavailableError, CaseState
from app.microservices import remote
from app.microservices.agent_app import build_agent_app


@pytest.fixture(autouse=True)
def _clean_remote():
    remote.reset()
    yield
    remote.reset()


def _asgi(apps: dict[str, FastAPI]):
    remote.configure(lambda slug: httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[slug]),
                                                    base_url=f"http://{slug}"))


def _stub(reply: dict | list) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/invoke")
    async def invoke():
        return reply

    return app


def test_run_applies_the_patch_like_the_local_worker():
    _asgi({"hitl": build_agent_app("hitl")})
    state, local_state = CaseState(raw_text="mild headache", confidence=0.3), CaseState(raw_text="mild headache",
                                                                                        confidence=0.3)
    result = asyncio.run(remote.RemoteAgent("hitl").run(state))
    local = HumanInTheLoopAgent().run(local_state)
    assert state == local_state
    assert result["escalated"] == local["escalated"]


def test_emit_returns_a_declared_message():
    _asgi({"hitl": build_agent_app("hitl")})
    agent = remote.RemoteAgent("hitl")
    state = CaseState(raw_text="mild headache", confidence=0.3)
    asyncio.run(agent.run(state))
    message = asyncio.run(agent.emit(state))
    assert message.sender == "hitl"
    assert message.intent in HumanInTheLoopAgent.COMMS.publishes


def test_patch_outside_the_lane_is_rejected():
    _asgi({"hitl": _stub({"result": {}, "state_patch": {"acuity_code": "P5_SELF_CARE"},
                          "messages": [], "carry": {}})})
    state = CaseState(raw_text="x")
    with pytest.raises(AgentUnavailableError, match="rejected"):
        asyncio.run(remote.RemoteAgent("hitl").run(state))
    assert state.acuity_code == "P3_URGENT"


def test_undeclared_intent_is_rejected():
    forged = {"sender": "hitl", "recipient": "broadcast", "intent": "acuity.classified", "payload": {}, "seq": 0}
    _asgi({"hitl": _stub({"result": None, "state_patch": {}, "messages": [forged], "carry": {}})})
    with pytest.raises(AgentUnavailableError, match="rejected"):
        asyncio.run(remote.RemoteAgent("hitl").emit(CaseState(raw_text="x")))


def test_connection_error_is_retried_once_then_unavailable():
    calls = {"n": 0}

    def refuse(request):
        calls["n"] += 1
        raise httpx.ConnectError("refused", request=request)

    remote.configure(lambda slug: httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://x"))
    with pytest.raises(AgentUnavailableError, match="unavailable"):
        asyncio.run(remote.RemoteAgent("hitl").run(CaseState(raw_text="x")))
    assert calls["n"] == 2


def test_timeout_is_not_retried():
    calls = {"n": 0}

    def slow(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    remote.configure(lambda slug: httpx.AsyncClient(transport=httpx.MockTransport(slow), base_url="http://x"))
    with pytest.raises(AgentUnavailableError):
        asyncio.run(remote.RemoteAgent("hitl").run(CaseState(raw_text="x")))
    assert calls["n"] == 1


def test_breaker_opens_and_skips_the_network():
    calls = {"n": 0}

    def down(request):
        calls["n"] += 1
        return httpx.Response(500)

    remote.configure(lambda slug: httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://x"))
    agent = remote.RemoteAgent("hitl")
    for _ in range(config.LLM_BREAKER_THRESHOLD):
        with pytest.raises(AgentUnavailableError):
            asyncio.run(agent.run(CaseState(raw_text="x")))
    before = calls["n"]
    with pytest.raises(AgentUnavailableError, match="circuit open"):
        asyncio.run(agent.run(CaseState(raw_text="x")))
    assert calls["n"] == before
    assert remote.agent_health()["hitl"]["breaker"] == "open"


def test_non_dict_reply_is_rejected_on_run():
    _asgi({"hitl": _stub([])})
    with pytest.raises(AgentUnavailableError, match="rejected"):
        asyncio.run(remote.RemoteAgent("hitl").run(CaseState(raw_text="x")))


def test_non_dict_reply_is_rejected_on_prescreen():
    _asgi({"hitl": _stub([])})
    with pytest.raises(AgentUnavailableError, match="rejected"):
        asyncio.run(remote.RemoteAgent("hitl").prescreen("x"))


def test_non_dict_reply_is_rejected_on_emit():
    _asgi({"hitl": _stub([])})
    with pytest.raises(AgentUnavailableError, match="rejected"):
        asyncio.run(remote.RemoteAgent("hitl").emit(CaseState(raw_text="x")))


def test_run_with_non_dict_carry_is_rejected():
    _asgi({"hitl": _stub({"result": {}, "state_patch": {}, "messages": [], "carry": "x"})})
    with pytest.raises(AgentUnavailableError, match="rejected"):
        asyncio.run(remote.RemoteAgent("hitl").run(CaseState(raw_text="x")))


def test_undeclared_intent_leaks_nothing_and_trips_the_breaker():
    forged = {"sender": "hitl", "recipient": "broadcast", "intent": "leak-me-if-you-can", "payload": {}, "seq": 0}
    _asgi({"hitl": _stub({"result": None, "state_patch": {}, "messages": [forged], "carry": {}})})
    agent = remote.RemoteAgent("hitl")
    for _ in range(config.LLM_BREAKER_THRESHOLD):
        with pytest.raises(AgentUnavailableError) as exc_info:
            asyncio.run(agent.emit(CaseState(raw_text="x")))
        message = str(exc_info.value)
        assert "leak-me-if-you-can" not in message
        assert "broadcast" not in message
        assert message == "hitl reply rejected: CommsAccessError"
    assert remote.agent_health()["hitl"]["breaker"] == "open"


def test_remote_workers_cover_every_orchestrated_agent():
    assert set(remote.remote_workers()) == {"classifier", "safety", "routing", "reflection", "hitl", "handoff"}


def test_agent_llm_calls_reach_the_gateway_log():
    from app import llm

    call = {"task": "reflection.critic", "tier": "deep", "reason": "route", "model": "gpt-5.4",
            "cached": False, "ms": 900, "prompt": "must not be copied"}
    _asgi({"hitl": _stub({"result": {}, "state_patch": {}, "messages": [], "carry": {},
                          "llm_calls": [call, "junk", {"model": {"nested": 1}}]})})

    async def go():
        log = llm.begin_call_log()
        await remote.RemoteAgent("hitl").run(CaseState(raw_text="x"))
        return log

    log = asyncio.run(go())
    assert log[0] == {k: call[k] for k in llm._CALL_KEYS}
    assert log[1:] == [{"task": None, "tier": None, "reason": None, "cached": None, "ms": None}]
