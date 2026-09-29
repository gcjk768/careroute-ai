"""[Microservices] Same case, same answer, whichever side of the network the agents are on.

Runs the gold triage vignettes through the real SSE pipeline twice — agents
in-process, then every agent behind its own HTTP app — and requires the
decision and the whole agent-to-agent conversation to be identical. Because
the gold set includes every must-escalate case, this is also the red-flag
recall gate (tests/test_triage_eval.py) re-run over HTTP.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import config
from app.main import _triage_event_stream
from app.microservices import remote
from app.microservices.agent_app import build_agent_app
from app.microservices.workers import AGENT_CLASSES
from app.models import TriageRequest

_FIXTURE = Path(__file__).parent / "fixtures" / "triage_vignettes.json"
_COMPARED = ("acuity", "careTier", "confidence", "escalated", "escalationReason", "clinic",
             "rationale", "citations")
_VOLATILE = {"durationMs", "elapsedMs", "timing_ms", "ts", "createdAt"}


def _vignettes() -> list[dict]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["vignettes"]


def _stable(value):
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    import app.main as main_mod

    async def _no_delay(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "_step_delay", _no_delay)


@pytest.fixture(scope="module")
def agent_apps():
    return {slug: build_agent_app(slug) for slug in AGENT_CLASSES}


def _run(text: str):
    async def collect():
        events = []
        async for chunk in _triage_event_stream(TriageRequest(text=text, language="en", isVoice=False)):
            line = chunk.strip()
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):].strip()))
        return events

    events = asyncio.run(collect())
    final = next(e for e in events if e.get("event") == "final")
    conversation = [(e["sender"], e["recipient"], e["intent"], e["payload"])
                    for e in events if e.get("event") == "agent_message"]
    return _stable({k: final.get(k) for k in _COMPARED}), _stable(conversation)


@pytest.mark.parametrize("vignette", _vignettes(), ids=lambda v: v.get("id", v["text"][:24]))
def test_http_transport_matches_in_process(monkeypatch, agent_apps, vignette):
    in_process = _run(vignette["text"])

    monkeypatch.setattr(config, "AGENT_TRANSPORT", "http")
    remote.configure(lambda slug: httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_apps[slug]),
                                                    base_url=f"http://{slug}"))
    try:
        over_http = _run(vignette["text"])
    finally:
        remote.reset()

    assert over_http == in_process
