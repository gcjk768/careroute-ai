"""[Microservices] The gateway side of the agent contract.

`RemoteAgent` stands in for a worker object inside PipelineOrchestrator: it
carries the worker's SLUG / COMMS / CONTRACT, and `run`, `areason`, `prescreen`
and `emit` become POSTs to that agent's container (agent_app.py). Everything
the orchestrator already enforces still applies, and the network boundary adds
checks of its own, all performed BEFORE a call is recorded as successful:

  * the reply must be the JSON shape that op promises (agent_app.py's own
    response shape) — a dict, with `result`/`carry` dicts-or-None on `run`,
    a bool-or-None `result` on `prescreen`;
  * a reply may only write the CaseState fields in the agent's CONTRACT.writes;
  * an announcement must come from that agent and carry an intent in its
    COMMS.publishes (the same enforce_comms the in-process worker calls).

A reply that fails any check is treated exactly like a dead container:
AgentUnavailableError, and the orchestrator degrades the step. A lying agent
is not trusted more than a silent one — and, because the reply came over the
network, its content (a forged sender, an unexpected field, attacker- or
patient-controlled text) never rides inside our own exception message or log
line: `_reject` reports only the failing op and the exception's TYPE.

Retries: one, and only on a connection error (the container may be
restarting). A timeout is not retried — the agent may still be working, and a
second copy of the same LLM call doubles the cost for no gain.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from .. import config, correlation, llm, metrics
from ..agents.base import AgentUnavailableError, CaseState
from ..agents.messaging import AgentMessage, CommsAccessError, enforce_comms
from ..llm import _Breaker  # same semantics and thresholds as the LLM provider chain
from . import wire
from .workers import AGENT_CLASSES

logger = logging.getLogger("careroute.remote")

ClientFactory = Callable[[str], httpx.AsyncClient]
_client_factory: ClientFactory | None = None
_BREAKERS: dict[str, _Breaker] = {}

#: Everything a malformed or lying reply can raise while `_validate` is
#: picking it apart. Caught as one group in `_invoke` so no reply-shape
#: exception ever escapes uncaught, whatever op it came from.
_REPLY_ERRORS = (AttributeError, KeyError, TypeError, ValueError, CommsAccessError)


class _CallFailed(Exception):
    """Internal: the call produced no usable HTTP 200."""


def configure(factory: ClientFactory | None) -> None:
    """Route calls through `factory` (tests: in-process ASGI apps). None = real URLs."""
    global _client_factory
    _client_factory = factory


def reset() -> None:
    configure(None)
    _BREAKERS.clear()


def _client(slug: str) -> httpx.AsyncClient:
    if _client_factory is not None:
        return _client_factory(slug)
    return httpx.AsyncClient(base_url=config.AGENT_URLS[slug], timeout=config.AGENT_TIMEOUT_SECONDS)


def _breaker(slug: str) -> _Breaker:
    return _BREAKERS.setdefault(slug, _Breaker())


def agent_health() -> dict[str, dict]:
    """Breaker state per agent, for /api/health. Empty until an agent was called."""
    return {
        slug: {"breaker": "open" if b.opened_at is not None else "closed", "failures": b.failures}
        for slug, b in sorted(_BREAKERS.items())
    }


def remote_workers() -> dict[str, RemoteAgent]:
    """The keyword arguments PipelineOrchestrator takes, one RemoteAgent each."""
    return {slug: RemoteAgent(slug) for slug in AGENT_CLASSES}


class RemoteAgent:
    def __init__(self, slug: str) -> None:
        cls = AGENT_CLASSES[slug]
        self.SLUG = cls.SLUG
        self.COMMS = cls.COMMS
        self.CONTRACT = cls.CONTRACT
        self.TOOL_ALLOWLIST = getattr(cls, "TOOL_ALLOWLIST", [])
        self.received: list[AgentMessage] = []
        self._consumed = False
        self._carry: dict = {}

    # -- the worker interface the orchestrator already calls -----------------
    def consume(self, inbox: list[AgentMessage]) -> None:
        self.received = list(inbox)
        self._consumed = True

    async def run(self, state: CaseState) -> dict:
        return await self._invoke("run", state)

    async def areason(self, state: CaseState, timeout_s: float | None = None) -> None:
        # `timeout_s` matches the in-process signature and is ignored on
        # purpose: the container bounds its own critic call from its config.
        await self._invoke("areason", state)

    async def shadow_nlp(self, state: CaseState, *, enabled: bool = True) -> None:
        # `enabled` is accepted so this matches the in-process signature, and
        # ignored on purpose: the safety container owns the NLP models, so it
        # reads its own config rather than trusting a flag off the wire.
        await self._invoke("shadow_nlp", state)

    async def prescreen(self, text: str) -> bool:
        return await self._invoke("prescreen", CaseState(raw_text=text))

    async def emit(self, state: CaseState) -> AgentMessage:
        return await self._invoke("emit", state, carry=self._carry)

    # -- reply validation -------------------------------------------------
    def _validate(self, op: str, data: object, state: CaseState):
        """Check the WHOLE reply before anything from it is recorded as
        success or touches `state`. Any deviation from agent_app.py's own
        response shape raises here, caught by `_invoke` and turned into a
        rejection via `_reject` — never a raw AttributeError/TypeError/etc
        escaping to the caller.
        """
        if not isinstance(data, dict):
            raise wire.WireError("reply is not a JSON object")

        if op in ("run", "areason", "shadow_nlp"):
            patch = data.get("state_patch") or {}
            if not isinstance(patch, dict):
                raise wire.WireError("state_patch is not an object")
            result = data.get("result")
            if result is not None and not isinstance(result, dict):
                raise wire.WireError("result is not an object")
            carry = data.get("carry")
            if carry is not None and not isinstance(carry, dict):
                raise wire.WireError("carry is not an object")
            # Everything above is checked before the patch touches `state`,
            # so a reply that is malformed ANYWHERE changes nothing — the
            # same "validate the whole patch before applying it" contract
            # wire.apply_patch itself keeps for CONTRACT.writes.
            wire.apply_patch(state, patch, allowed=self.CONTRACT.writes, agent=self.SLUG)
            if op in ("areason", "shadow_nlp"):
                return None
            self._carry = dict(carry or {})
            return dict(result or {})

        if op == "prescreen":
            result = data.get("result")
            if result is not None and not isinstance(result, bool):
                raise wire.WireError("result is not a boolean")
            return bool(result)

        if op == "emit":
            messages = data.get("messages")
            if not isinstance(messages, list) or len(messages) != 1:
                raise wire.WireError("expected exactly one message")
            message = wire.message_from_wire(messages[0])
            if message.sender != self.SLUG:
                raise wire.WireError("reply sender does not match the agent")
            enforce_comms(self, message.intent)
            return message

        raise wire.WireError(f"unknown op {op!r}")

    # -- transport ------------------------------------------------------------
    async def _invoke(self, op: str, state: CaseState, *, carry: dict | None = None):
        breaker = _breaker(self.SLUG)
        if breaker.is_open(time.monotonic()):
            metrics.observe_agent_call(self.SLUG, "breaker_open")
            raise AgentUnavailableError(f"{self.SLUG} circuit open")
        body = {
            "op": op,
            "case_id": state.case_id,
            "state": wire.state_to_wire(state),
            "inbox": [wire.message_to_wire(m) for m in self.received],
            "consumed": self._consumed,
            "carry": carry or {},
        }
        try:
            response = await self._post(body)
            if response.status_code != 200:
                raise _CallFailed(f"HTTP {response.status_code}")
        except _CallFailed as exc:
            breaker.record_failure(time.monotonic())
            metrics.observe_agent_call(self.SLUG, "error")
            logger.warning("agent=%s op=%s unavailable: %s", self.SLUG, op, exc)
            raise AgentUnavailableError(f"{self.SLUG} unavailable: {exc}") from None
        # Validate BEFORE recording success: a reply that fails shape, lane
        # or COMMS checks (op="emit") must count as exactly one "rejected"
        # outcome and one breaker failure — never both "ok" and "rejected".
        try:
            data = response.json()
            result = self._validate(op, data, state)
        except _REPLY_ERRORS as exc:
            self._reject(op, exc)
        llm.merge_call_log(data.get("llm_calls"))
        breaker.record_success()
        metrics.observe_agent_call(self.SLUG, "ok")
        return result

    async def _post(self, body: dict) -> httpx.Response:
        headers = {"X-Correlation-Id": correlation.get(), "X-Internal-Token": config.INTERNAL_TOKEN}
        last = "no attempt"
        async with _client(self.SLUG) as client:
            for _attempt in range(2):
                try:
                    return await client.post("/v1/invoke", json=body, headers=headers)
                except httpx.ConnectError as exc:
                    last = type(exc).__name__  # container restarting: worth exactly one retry
                except httpx.HTTPError as exc:
                    raise _CallFailed(type(exc).__name__) from None
        raise _CallFailed(last)

    def _reject(self, op: str, exc: Exception) -> None:
        """A reply that failed shape/lane/COMMS validation: treated like a
        dead container. Only `type(exc).__name__` is used — never
        `str(exc)` — because the reply came from the network and its text
        (a forged sender, an unexpected field name, a copied fragment of
        patient text) must never ride inside our own exception message or
        this warning log line."""
        _breaker(self.SLUG).record_failure(time.monotonic())
        metrics.observe_agent_call(self.SLUG, "rejected")
        logger.warning("agent=%s op=%s reply rejected: %s", self.SLUG, op, type(exc).__name__)
        raise AgentUnavailableError(f"{self.SLUG} reply rejected: {type(exc).__name__}") from None
