"""SSE heartbeat — the triage stream must never go silent for long.

WHY THIS EXISTS
---------------
The frontend reaches ``/api/triage/stream`` through the Next.js ``/api`` rewrite,
which is an ``http-proxy`` instance with a 30 s idle ``proxyTimeout``. When the
upstream emits nothing for 30 s the proxy aborts the upstream request but never
ends the browser's response, so the UI hangs on "Triaging..." forever with no
error. Measured in Chrome through a Next rewrite (2026-09-02): a silent gap of
26 s is delivered, 32 s stalls indefinitely, and a 40 s gap bridged by an SSE
comment every 10 s is delivered in full.

Care Routing's OneMap shortlist + bounded replan takes 24-30 s with no events,
which is exactly on that edge — hence the intermittent hang on the P4 clinic
path, and the reliable hang on the red-flag path whose LLM calls gap ~30 s.

The fix is transport-level: while the orchestrator is quiet, emit an SSE comment
frame (``: keepalive``) every few seconds. Comment frames carry no ``data:``
line, so ``lib/api.js`` ignores them, and they reset every idle timer between
the backend and the browser.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import config
from app import main as main_module
from app.main import _with_heartbeat, app

KEEPALIVE = ": keepalive\n\n"


async def _collect(agen) -> list[str]:
    return [item async for item in agen]


@pytest.mark.asyncio
async def test_heartbeat_fills_silent_gaps_and_preserves_order():
    async def slow_source():
        yield "data: a\n\n"
        await asyncio.sleep(0.35)
        yield "data: b\n\n"

    out = await _collect(_with_heartbeat(slow_source(), interval=0.1))

    assert out[0] == "data: a\n\n"
    assert out[-1] == "data: b\n\n"
    middle = out[1:-1]
    # A 0.35 s gap at a 0.1 s cadence must produce at least two keepalives.
    assert len(middle) >= 2, out
    assert all(frame == KEEPALIVE for frame in middle), out


@pytest.mark.asyncio
async def test_heartbeat_is_silent_when_source_is_fast():
    async def fast_source():
        for i in range(5):
            yield f"data: {i}\n\n"

    out = await _collect(_with_heartbeat(fast_source(), interval=1.0))

    assert out == [f"data: {i}\n\n" for i in range(5)]


@pytest.mark.asyncio
async def test_heartbeat_stops_when_source_finishes():
    async def one_shot():
        yield "data: only\n\n"

    out = await _collect(_with_heartbeat(one_shot(), interval=0.05))
    # No trailing keepalives after the source is exhausted.
    assert out == ["data: only\n\n"]


@pytest.mark.asyncio
async def test_heartbeat_propagates_source_errors():
    async def failing_source():
        yield "data: first\n\n"
        raise RuntimeError("upstream broke")

    with pytest.raises(RuntimeError, match="upstream broke"):
        await _collect(_with_heartbeat(failing_source(), interval=0.05))


@pytest.mark.asyncio
async def test_early_consumer_exit_tears_the_source_down_deterministically():
    """A client that disconnects mid-stream must not leave the orchestration
    running.

    The wrapper consumes the source through an explicit task, so simply
    abandoning the wrapper cancels that task but leaves the SOURCE generator
    suspended: its ``finally:`` (which is where the orchestrator releases its
    OneMap session, closes the audit span and lets the CaseState go) then runs
    only when the garbage collector eventually finalises it — on a different
    task, at an unpredictable time, and possibly after the event loop is gone.

    Closing the wrapper must therefore close the source, synchronously, before
    ``aclose()`` returns.
    """
    torn_down = asyncio.Event()

    async def source():
        try:
            yield "data: a\n\n"
            await asyncio.sleep(3600)  # a quiet agent, still working
            yield "data: b\n\n"
        finally:
            torn_down.set()

    wrapper = _with_heartbeat(source(), interval=0.05)
    assert await wrapper.__anext__() == "data: a\n\n"
    await wrapper.aclose()

    assert torn_down.is_set(), "source generator was left for the GC to finalise"


@pytest.mark.asyncio
async def test_early_exit_leaves_no_pending_task_behind():
    """The in-flight ``__anext__`` task must be awaited to completion, not just
    signalled — an un-awaited cancelled task surfaces later as a spurious
    'Task exception was never retrieved' on an unrelated request."""
    async def source():
        yield "data: a\n\n"
        await asyncio.sleep(3600)

    before = {t for t in asyncio.all_tasks()}
    wrapper = _with_heartbeat(source(), interval=0.05)
    await wrapper.__anext__()
    await wrapper.aclose()

    leaked = [t for t in asyncio.all_tasks() - before if not t.done()]
    assert not leaked, leaked


def test_triage_stream_emits_keepalive_during_a_quiet_agent(monkeypatch):
    """Through the real endpoint: a quiet stretch inside the orchestrator must
    be bridged by comment frames, and every real event must still arrive."""
    # Each request runs on a fresh worker set from `orchestrator.new_session()`
    # (see main.py), so the patch must land on the CLASS, not the module-level
    # template instance — otherwise the session never sees the quiet stretch
    # and this test passes only because the real step delays happen to exceed
    # the 0.1 s heartbeat interval.
    orchestrator_cls = type(main_module.orchestrator)
    real_orchestrate = orchestrator_cls.orchestrate

    async def orchestrate_with_a_quiet_stretch(self, state, **kwargs):
        first = True
        async for event in real_orchestrate(self, state, **kwargs):
            yield event
            if first:
                first = False
                await asyncio.sleep(0.4)  # simulate Care Routing waiting on OneMap

    monkeypatch.setattr(orchestrator_cls, "orchestrate", orchestrate_with_a_quiet_stretch)
    monkeypatch.setattr(config, "SSE_HEARTBEAT_SECONDS", 0.1)

    client = TestClient(app)
    resp = client.post("/api/triage/stream", json={"text": "I have a mild sore throat."})
    assert resp.status_code == 200

    body = resp.text
    assert body.count(KEEPALIVE) >= 2, body[:500]
    data_lines = [line for line in body.splitlines() if line.startswith("data:")]
    assert any('"event": "final"' in line for line in data_lines), "final event missing"
    # Comment frames must never be interleaved INSIDE a data frame.
    for frame in body.split("\n\n"):
        if frame.startswith("data:"):
            assert "\n:" not in frame, frame
