"""Network-free tests for the OpenAI boundary used by Care-Routing."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import llm
from app.agents.routing import ROUTING_RESPONSE_SCHEMA


class _Response:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self.text = ""
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Client:
    """Minimal async HTTP client substitute; makes no external request."""

    captured: dict = {}
    response: _Response

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict) -> _Response:
        type(self).captured = {"url": url, "body": json, "headers": headers}
        return type(self).response


def _provider_with_response(monkeypatch, message: dict) -> llm.OpenAIProvider:
    monkeypatch.setattr(llm.config, "OPENAI_API_KEY", "test-key")
    _Client.response = _Response({"choices": [{"message": message}]})
    monkeypatch.setattr(llm.httpx, "AsyncClient", _Client)
    return llm.OpenAIProvider()


def test_openai_router_request_uses_strict_structured_output(monkeypatch):
    """OpenAI receives the router's strict JSON schema and returns a valid decision."""
    provider = _provider_with_response(
        monkeypatch,
        {"content": json.dumps({
            "selected_clinic_id": "chas-100001-open-clinic",
            "confidence": 0.88,
            "rationale": "Open and nearby.",
            "tradeoffs": [],
        })},
    )

    raw = asyncio.run(provider.complete("system", "candidate data", True, ROUTING_RESPONSE_SCHEMA))

    assert json.loads(raw)["selected_clinic_id"] == "chas-100001-open-clinic"
    response_format = _Client.captured["body"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == ROUTING_RESPONSE_SCHEMA
    assert _Client.captured["headers"]["Authorization"] == "Bearer test-key"


def test_openai_refusal_is_not_treated_as_a_routing_decision(monkeypatch):
    """An OpenAI safety refusal is rejected instead of becoming a clinic decision."""
    provider = _provider_with_response(monkeypatch, {"refusal": "Cannot help with this request."})

    with pytest.raises(llm.LLMUnavailableError, match="refused"):
        asyncio.run(provider.complete("system", "candidate data", True, ROUTING_RESPONSE_SCHEMA))


# --------------------------------------------------------------------------
# Token/cost accounting. The provider used to parse `choices` and drop `usage`,
# so the token cost of a triage was unknown — see app/llm_cost.py.
# --------------------------------------------------------------------------

def _provider_with_full_response(monkeypatch, payload: dict) -> llm.OpenAIProvider:
    monkeypatch.setattr(llm.config, "OPENAI_API_KEY", "test-key")
    _Client.response = _Response(payload)
    monkeypatch.setattr(llm.httpx, "AsyncClient", _Client)
    return llm.OpenAIProvider()


@pytest.mark.asyncio
async def test_openai_response_usage_is_recorded(monkeypatch):
    recorded: list[tuple] = []
    monkeypatch.setattr(llm.llm_cost, "record", lambda model, usage: recorded.append((model, usage)))
    provider = _provider_with_full_response(monkeypatch, {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    })

    await provider.complete("sys", "prompt", False, None)

    assert recorded, "provider must hand the usage block to the cost accounting"
    _model, usage = recorded[0]
    assert usage["prompt_tokens"] == 120
    assert usage["completion_tokens"] == 30


@pytest.mark.asyncio
async def test_openai_response_without_usage_still_completes(monkeypatch):
    """A provider that omits `usage` must not break a triage."""
    provider = _provider_with_full_response(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})

    assert await provider.complete("sys", "prompt", False, None) == "ok"


# --- GPT-5 compatibility: models that accept only default sampling parameters -------------
class _Scripted:
    """Replays a list of (status, payload) responses and records every request body."""

    bodies: list = []
    script: list = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict):
        type(self).bodies.append(dict(json))   # a copy: the provider edits its body between retries
        status, payload = type(self).script.pop(0)
        r = _Response(payload)
        r.status_code = status
        r.text = __import__("json").dumps(payload)
        return r


_OK = (200, {"choices": [{"message": {"content": '{"ok": true}'}}]})
_NO_TEMP = (400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.2 with this model. "
                                       "Only the default (1) value is supported.", "param": "temperature"}})


def _scripted(monkeypatch, script):
    monkeypatch.setattr(llm.config, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(llm.httpx, "AsyncClient", _Scripted)
    monkeypatch.setattr(llm, "_UNSUPPORTED_PARAMS", {})
    _Scripted.bodies, _Scripted.script = [], list(script)
    return llm.OpenAIProvider()


def test_a_model_that_rejects_temperature_is_retried_without_it_and_remembered(monkeypatch):
    """gpt-5-mini and gpt-5.5 answer 400 to temperature 0.2 (measured 2026-09-27)."""
    p = _scripted(monkeypatch, [_NO_TEMP, _OK, _OK])
    assert asyncio.run(p.complete("s", "u", True, model="gpt-5.5")) == '{"ok": true}'
    assert "temperature" in _Scripted.bodies[0] and "temperature" not in _Scripted.bodies[1]
    asyncio.run(p.complete("s", "u", True, model="gpt-5.5"))
    assert len(_Scripted.bodies) == 3 and "temperature" not in _Scripted.bodies[2]   # no second 400


def test_models_that_accept_temperature_keep_it(monkeypatch):
    p = _scripted(monkeypatch, [_OK])
    asyncio.run(p.complete("s", "u", True, model="gpt-4o-mini"))
    assert _Scripted.bodies[0]["temperature"] == 0.2


def test_reasoning_effort_goes_only_to_reasoning_models(monkeypatch):
    monkeypatch.setattr(llm.config, "OPENAI_REASONING_EFFORT", "low")
    p = _scripted(monkeypatch, [_OK, _OK])
    asyncio.run(p.complete("s", "u", True, model="gpt-5.4-mini"))
    asyncio.run(p.complete("s", "u", True, model="gpt-4.1-mini"))
    assert _Scripted.bodies[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in _Scripted.bodies[1]


def test_other_400s_are_not_retried(monkeypatch):
    p = _scripted(monkeypatch, [(400, {"error": {"message": "Invalid schema", "param": "response_format"}})])
    with pytest.raises(llm.LLMUnavailableError):
        asyncio.run(p.complete("s", "u", True, model="gpt-5.5"))
    assert len(_Scripted.bodies) == 1
