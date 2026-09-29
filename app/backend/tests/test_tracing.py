"""app/tracing.py: silent when unconfigured; with a sink, one generation per
served LLM call (redacted prompt) and one agent span per worker, all under the
case's own trace id."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import correlation, llm, tracing
from app.main import _triage_event_stream
from app.models import TriageRequest

# Captured at import, before conftest's session-wide stub replaces it.
_REAL_COMPLETE = llm.complete


class _FakeObs:
    def __init__(self, kw, log):
        self.kw, self.log, self.update_kw = kw, log, {}
        log.append(("start", kw["name"]))

    def update(self, **kw):
        self.update_kw = kw

    def end(self):
        self.log.append(("end", self.kw["name"]))


class _FakeLangfuse:
    def __init__(self):
        self.observations: list[dict] = []
        self.log: list[tuple[str, str]] = []
        self.objs: list[_FakeObs] = []

    @staticmethod
    def create_trace_id(seed=None):
        return f"trace-{seed}"

    def start_observation(self, **kw):
        self.observations.append(kw)
        self.objs.append(_FakeObs(kw, self.log))
        return self.objs[-1]


@pytest.fixture
def sink(monkeypatch):
    fake = _FakeLangfuse()
    monkeypatch.setattr(tracing, "_langfuse", lambda: fake)
    monkeypatch.setattr(tracing, "_langsmith", lambda: None)
    return fake


def test_unconfigured_tracing_is_off():
    tracing._langfuse.cache_clear()
    tracing._langsmith.cache_clear()
    assert tracing.enabled() is False
    tracing.end(tracing.generation_start("t", "m", "sys", "prompt"), {"content": "out"})  # no sink, no error


def test_a_served_llm_call_is_one_generation_with_the_masked_prompt(monkeypatch, sink):
    class _Provider:
        name = "fake"

        def label(self):
            return "fake:model"
        meters_usage = True

        async def available(self):
            return True

        async def complete(self, system, prompt, json_mode, json_schema, **_):
            sink.log.append(("provider", "call"))   # the span must already be open here
            return "ok"

    monkeypatch.setattr(llm, "_ordered", lambda _order=None: [_Provider()])
    token = correlation.bind_case("case_abc")
    try:
        assert asyncio.run(_REAL_COMPLETE("sys", "my NRIC is S1234567D and I have a cough")) == "ok"
    finally:
        correlation.reset_case(token)

    [gen] = sink.observations
    assert sink.log == [("start", "llm:untasked"), ("provider", "call"), ("end", "llm:untasked")]
    assert gen["as_type"] == "generation" and sink.objs[0].update_kw["output"] == {"content": "ok"}
    assert gen["trace_context"] == {"trace_id": "trace-case_abc"}
    sent = json.dumps(gen["input"])
    assert "S1234567D" not in sent and "cough" in sent


def test_an_orchestrated_case_is_one_trace_of_agent_spans(monkeypatch, sink):
    import app.main as main_mod

    async def _dead(*_a, **_k):
        raise llm.LLMUnavailableError("off")

    async def _no_delay(*_a, **_k):
        return None

    monkeypatch.setattr(llm, "complete", _dead)
    monkeypatch.setattr(main_mod, "_step_delay", _no_delay)

    async def _run():
        async for _ in _triage_event_stream(TriageRequest(text="sore throat and runny nose for two days")):
            pass

    asyncio.run(_run())
    spans = [o for o in sink.observations if o["as_type"] == "agent"]
    assert {"agent:intake", "agent:classifier", "agent:safety", "agent:routing", "agent:hitl"} <= {s["name"] for s in spans}
    assert len({s["trace_context"]["trace_id"] for s in spans}) == 1
    assert all(s["input"] is None for s in spans)  # agent spans carry no patient text
    # every span opened was closed (a span created after the work records 0 s)
    assert sorted(n for e, n in sink.log if e == "start") == sorted(n for e, n in sink.log if e == "end")
