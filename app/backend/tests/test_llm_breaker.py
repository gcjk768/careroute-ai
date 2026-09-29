"""[AI-Security][ASI08] Circuit breaker over the LLM provider chain.

OWASP Agentic ASI08 (Cascading Failures) is about **amplification**: five workers
call `llm.complete()` per triage, so one dead provider is waited on five times per
case, and every concurrent case pays the same tax. These tests pin the property
that matters — a provider already known to be down is skipped rather than called
again — and the two ways that could go wrong: never opening, or never closing.
"""
from __future__ import annotations

import pytest

from app import config, llm

# The root conftest autouse-patches `llm.complete` to raise, so every agent test
# takes the deterministic path. These tests are ABOUT `complete` itself, so the
# real coroutine is captured at import time (before any fixture runs) and put
# back per-test by the `chain` fixture below.
_REAL_COMPLETE = llm.complete


class _CountingProvider:
    """Stands in for a real provider and records how often it was actually called."""

    def __init__(self, name: str, *, fails: bool) -> None:
        self.name = name
        self.calls = 0
        self._fails = fails

    def label(self) -> str:
        return self.name

    async def available(self) -> bool:
        return True

    async def complete(self, system, prompt, json_mode=False, json_schema=None) -> str:
        self.calls += 1
        if self._fails:
            raise llm.LLMUnavailableError(f"{self.name} is down")
        return "ok"


@pytest.fixture
def chain(monkeypatch):
    """Install a two-provider chain and guarantee breaker state never leaks."""
    def _install(*providers):
        monkeypatch.setattr(llm, "complete", _REAL_COMPLETE)
        monkeypatch.setattr(llm, "_PROVIDERS", {p.name: p for p in providers})
        monkeypatch.setattr(config, "LLM_PROVIDER_ORDER", [p.name for p in providers])
        monkeypatch.setattr(config, "KILL_SWITCH", False)
        llm.reset_breakers()
        return providers

    yield _install
    llm.reset_breakers()


async def _drive(n: int) -> None:
    for _ in range(n):
        with pytest.raises(llm.LLMUnavailableError):
            await llm.complete("sys", "prompt")


@pytest.mark.asyncio
async def test_breaker_opens_and_stops_calling_a_dead_provider(chain, monkeypatch):
    monkeypatch.setattr(config, "LLM_BREAKER_THRESHOLD", 3)
    monkeypatch.setattr(config, "LLM_BREAKER_COOLDOWN_SECONDS", 30)
    (dead,) = chain(_CountingProvider("dead", fails=True))

    await _drive(6)

    # Called 3 times to trip the breaker, then skipped without being called.
    assert dead.calls == 3
    assert llm.breaker_state()["dead"]["open"] is True


@pytest.mark.asyncio
async def test_an_open_breaker_falls_through_to_the_next_provider(chain, monkeypatch):
    monkeypatch.setattr(config, "LLM_BREAKER_THRESHOLD", 2)
    monkeypatch.setattr(config, "LLM_BREAKER_COOLDOWN_SECONDS", 30)
    dead, healthy = chain(
        _CountingProvider("dead", fails=True), _CountingProvider("healthy", fails=False)
    )

    for _ in range(5):
        assert await llm.complete("sys", "prompt") == "ok"

    # The point of the control: the chain keeps serving while the dead provider
    # stops being paid for. Failing over is not the same as not calling.
    assert dead.calls == 2
    assert healthy.calls == 5


@pytest.mark.asyncio
async def test_a_success_resets_the_failure_count(chain, monkeypatch):
    """A flaky-but-usable provider must never be locked out.

    The breaker counts CONSECUTIVE failures, so two failures, a success, then two
    more failures must not trip a threshold of 3.
    """
    monkeypatch.setattr(config, "LLM_BREAKER_THRESHOLD", 3)
    flaky = _CountingProvider("flaky", fails=True)
    chain(flaky)

    await _drive(2)
    flaky._fails = False
    assert await llm.complete("sys", "prompt") == "ok"
    flaky._fails = True
    await _drive(2)

    assert llm.breaker_state()["flaky"]["open"] is False


@pytest.mark.asyncio
async def test_breaker_half_opens_after_the_cooldown_and_recovers(chain, monkeypatch):
    """A breaker that never closes is an outage the breaker itself caused."""
    monkeypatch.setattr(config, "LLM_BREAKER_THRESHOLD", 2)
    monkeypatch.setattr(config, "LLM_BREAKER_COOLDOWN_SECONDS", 0)  # cooldown elapsed
    recovering = _CountingProvider("recovering", fails=True)
    chain(recovering)

    await _drive(2)
    assert recovering.calls == 2

    recovering._fails = False
    assert await llm.complete("sys", "prompt") == "ok"  # the half-open trial call
    assert llm.breaker_state()["recovering"] == {"open": False, "consecutiveFailures": 0}


@pytest.mark.asyncio
async def test_breaker_state_reaches_the_health_endpoint(chain, monkeypatch):
    """The control has to be OBSERVABLE, not just correct.

    `/api/health` builds its own payload rather than returning `llm.health()`
    verbatim, so a new field there is easy to add to the docs and forget to wire.
    This is the test that caught exactly that.
    """
    from app.main import health

    monkeypatch.setattr(config, "LLM_BREAKER_THRESHOLD", 2)
    monkeypatch.setattr(config, "LLM_BREAKER_COOLDOWN_SECONDS", 30)
    chain(_CountingProvider("dead", fails=True))

    await _drive(3)
    payload = (await health()).model_dump()

    assert payload["breakers"]["dead"] == {"open": True, "consecutiveFailures": 2}


@pytest.mark.asyncio
async def test_kill_switch_still_wins_over_the_breaker(chain, monkeypatch):
    """ASI10 must not be weakened by ASI08 plumbing: no provider is called at all."""
    (provider,) = chain(_CountingProvider("any", fails=False))
    monkeypatch.setattr(config, "KILL_SWITCH", True)

    with pytest.raises(llm.LLMUnavailableError, match="kill switch"):
        await llm.complete("sys", "prompt")

    assert provider.calls == 0
