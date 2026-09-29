# Agent Microservices Implementation Plan


**Goal:** Split CareRoute's backend so each agent runs as its own HTTP microservice in its own container, with the intake agent as the gateway/orchestrator, while keeping the whole thing deployable on one compute.

**Architecture:** The intake-gateway keeps the fixed, safety-gated step order and the `MessageBus`. Each other worker is reached through a `RemoteAgent` proxy that has the same interface as the in-process worker (`consume`/`run`/`areason`/`prescreen`/`emit`). Each call is a POST to that agent's container, which rebuilds the real worker class from the posted `CaseState` and returns a state patch. `AGENT_TRANSPORT=inprocess` (the default) keeps today's single-process behaviour, so the existing suite is untouched. `llm-gateway` (the only holder of the OpenAI key) and `rag-service` are shared services. Redis makes the escalation queue and audit trail durable.

**Tech Stack:** Python 3.12, FastAPI, httpx (ASGITransport / MockTransport in tests), pytest, redis-py + fakeredis, Docker multi-target builds, Docker Compose, GitLab CI, Prometheus.

**Spec:** `docs/design/specs/2026-09-19-agent-microservices-design.md`

## Global Constraints

- Branch: all work is committed on `SIT` (user instruction). Do not push.
- `AGENT_TRANSPORT` defaults to `inprocess`; with it unset, behaviour must be byte-for-byte today's (the whole existing suite passes unchanged).
- An unavailable agent must never make the outcome less safe: classifier/safety/routing/hitl down ⇒ `state.escalated = True`; acuity is never lowered.
- Agent replies may write only the fields in that agent's `CONTRACT.writes` and announce only intents in its `COMMS.publishes`.
- No patient text, prompts or model output in any log line or exception message added by this plan.
- Only the `llm-gateway` container is given `OPENAI_API_KEY`; production uses `LLM_PROVIDER_ORDER=openai`.
- Ports: gateway 8000, classifier 8101, safety 8102, routing 8103, reflection 8104, hitl 8105, handoff 8106, llm-gateway 8107, rag-service 8108, redis 6379.
- The backend ECS task is limited to 10 containers: intake-gateway, 6 agents, llm-gateway, rag-service, redis. `mcp-server` and `safety-nlp` are not built.
- `ruff check app` (config `backend/ruff.toml`, line length 120) must stay clean; use a targeted `# noqa: <code> - <reason>` in the codebase's style where a rule has to be waived.
- Test commands run from `backend/` with the repo venv. The suite's kill switch keeps every LLM call on the deterministic path.
  `.venv/Scripts/python -m pytest <path> -q`
- New test files live in `backend/tests/` and are prefixed `test_ms_` (the test dirs have no `__init__.py`, so basenames must be unique).

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/config.py` (modify) | transport, agent URLs, token, gateway/RAG/Redis URLs |
| `backend/app/agents/base.py` (modify) | `AgentUnavailableError` |
| `backend/app/microservices/__init__.py` (create) | package marker + one-paragraph map |
| `backend/app/microservices/wire.py` (create) | CaseState/AgentMessage ⇄ JSON, state patch, lane check |
| `backend/app/microservices/workers.py` (create) | slug → class, allowed ops, carry attrs, per-request worker factory |
| `backend/app/microservices/common.py` (create) | token check, readiness flag, `/health` `/ready` `/metrics` |
| `backend/app/microservices/agent_app.py` (create) | `build_agent_app(slug)` — `/v1/invoke`, `/v1/capability` |
| `backend/app/microservices/remote.py` (create) | `RemoteAgent`, breakers, client factory, `remote_workers()` |
| `backend/app/microservices/llm_app.py` (create) | llm-gateway service |
| `backend/app/microservices/rag_app.py` (create) | rag-service |
| `backend/app/microservices/serve.py` (create) | container entrypoint |
| `backend/app/persistence.py` (create) | Redis connect + best-effort write-through |
| `backend/app/agents/orchestration.py` (modify) | await-tolerant calls, degradation, http `new_session` |
| `backend/app/main.py`, `backend/app/models.py` (modify) | http wiring, skip warm-ups, `/api/health.agents` |
| `backend/app/metrics.py` (modify) | `careroute_agent_calls_total` |
| `backend/app/llm.py`, `backend/app/rag.py` (modify) | client mode for the shared services |
| `backend/app/store.py`, `backend/app/audit.py` (modify) | Redis write-through + reload |
| `backend/Dockerfile` (rewrite) | one target per service + `monolith` default |
| `docker-compose.yml` (rewrite) | every container as its own service |
| `monitoring/prometheus.yml`, `monitoring/alert.rules.yml` (modify) | scrape agents, `AgentDown` |
| `scripts/compose_smoke.py` (create) | one real triage through every container |
| `.gitlab-ci.yml` (modify) | image matrix, scans, push, compose smoke |
| `docs/ARCHITECTURE.md`, the spec (modify) | microservice view |

The infrastructure change (`careroute_ai_infra`: ECR repos, 10-container task definition, scheduled tasks, scrape targets) is a **separate plan in that repo**, written after this one lands. This plan produces working, testable software on its own: local Compose and CI.

---

### Task 1: Configuration, `AgentUnavailableError`, wire format

**Files:**
- Modify: `backend/app/config.py` (append a section after the LLM provider settings, ~line 196)
- Modify: `backend/app/agents/base.py` (after `enforce_tool_access`, ~line 96)
- Create: `backend/app/microservices/__init__.py`, `backend/app/microservices/wire.py`
- Test: `backend/tests/test_ms_wire.py`

**Interfaces:**
- Produces: `config.AGENT_TRANSPORT: str`, `config.AGENT_URLS: dict[str, str]`, `config.AGENT_TIMEOUT_SECONDS: float`, `config.INTERNAL_TOKEN: str`, `config.LLM_GATEWAY_URL: str`, `config.RAG_SERVICE_URL: str`, `config.REDIS_URL: str`
- Produces: `app.agents.base.AgentUnavailableError(RuntimeError)`
- Produces: `wire.WireError(ValueError)`, `wire.state_to_wire(state) -> dict`, `wire.state_from_wire(data) -> CaseState`, `wire.message_to_wire(m) -> dict`, `wire.message_from_wire(d) -> AgentMessage`, `wire.state_patch(before: dict, after: CaseState) -> dict`, `wire.apply_patch(state, patch, *, allowed: frozenset[str], agent: str) -> None`

- [x] **Step 1: Write the failing test**

`backend/tests/test_ms_wire.py`:

```python
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
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ms_wire.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.microservices'`

- [x] **Step 3: Implement**

Append to `backend/app/config.py`:

```python
# --------------------------------------------------------------------------
# [Microservices] One agent, one container
# (docs/design/specs/2026-09-19-agent-microservices-design.md)
# --------------------------------------------------------------------------
# "inprocess" (default): every worker runs inside this process, exactly as
# before. "http": the intake-gateway calls each worker's container.
AGENT_TRANSPORT: str = _str("AGENT_TRANSPORT", "inprocess").strip().lower()
# Base URL of each agent container, e.g. CAREROUTE_AGENT_URL_SAFETY. The
# defaults are the loopback ports the containers share inside one ECS task.
AGENT_URLS: dict[str, str] = {
    slug: _str(f"CAREROUTE_AGENT_URL_{slug.upper()}", f"http://localhost:{port}").rstrip("/")
    for slug, port in (("classifier", 8101), ("safety", 8102), ("routing", 8103),
                       ("reflection", 8104), ("hitl", 8105), ("handoff", 8106))
}
# Longer than one LLM call: an agent may make one (via llm-gateway) per request.
AGENT_TIMEOUT_SECONDS: float = _float("CAREROUTE_AGENT_TIMEOUT", "90")
# Shared secret on every gateway -> service call. Empty disables the check
# (local development); set it in every container in production.
INTERNAL_TOKEN: str = _str("CAREROUTE_INTERNAL_TOKEN", "").strip()
# When set, llm.complete() / rag.retrieve_detailed() call the shared services
# instead of running in-process. Never set them ON those services themselves.
LLM_GATEWAY_URL: str = _str("CAREROUTE_LLM_GATEWAY_URL", "").strip().rstrip("/")
RAG_SERVICE_URL: str = _str("CAREROUTE_RAG_SERVICE_URL", "").strip().rstrip("/")
# Durable backing for the escalation queue, case store and audit trail.
REDIS_URL: str = _str("CAREROUTE_REDIS_URL", "").strip()
```

Add to `backend/app/agents/base.py` right after `enforce_tool_access`:

```python
class AgentUnavailableError(RuntimeError):
    """[Microservices] An agent's container could not produce a usable answer —
    down, timed out, circuit open, or a reply that broke its contract. The
    orchestrator degrades that step, never to a less cautious outcome."""
```

`backend/app/microservices/__init__.py`:

```python
"""[Microservices] One agent, one container.

wire.py       CaseState / AgentMessage <-> JSON, and the per-agent write lane
workers.py    which worker class each container runs, and how it is built
agent_app.py  the HTTP face of one agent (`/v1/invoke`)
remote.py     the gateway side: RemoteAgent stands in for a worker object
llm_app.py    llm-gateway — the only container holding provider keys
rag_app.py    rag-service — corpus index + embedder, shared by every agent
serve.py      container entrypoint: `python -m app.microservices.serve <service>`
"""
```

`backend/app/microservices/wire.py`:

```python
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
```

- [x] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_ms_wire.py -q`
Expected: `6 passed`

- [x] **Step 5: Lint and commit**

```bash
.venv/Scripts/python -m ruff check app tests/test_ms_wire.py
git add app/config.py app/agents/base.py app/microservices/__init__.py app/microservices/wire.py tests/test_ms_wire.py
git commit -m "feat(microservices): transport config and the gateway<->agent wire format"
```

---

### Task 2: The agent service (`/v1/invoke`)

**Files:**
- Create: `backend/app/microservices/workers.py`, `backend/app/microservices/common.py`, `backend/app/microservices/agent_app.py`, `backend/app/microservices/serve.py`
- Test: `backend/tests/test_ms_agent_app.py`

**Interfaces:**
- Consumes: `wire.*` (Task 1), `config.INTERNAL_TOKEN`
- Produces: `workers.AGENT_CLASSES: dict[str, type]` (keys `classifier safety routing reflection hitl handoff`), `workers.OPS: dict[str, frozenset[str]]`, `workers.CARRY: dict[str, tuple[str, ...]]`, `workers.worker_factory(slug) -> Callable[[], object]`
- Produces: `common.check_token(token: str | None) -> None` (raises HTTP 401), `common.mark_ready(name: str) -> None`, `common.add_common_routes(app: FastAPI, name: str) -> None`
- Produces: `agent_app.build_agent_app(slug: str) -> FastAPI`
- Produces: `/v1/invoke` request `{op, case_id, state, inbox, consumed, carry}` → reply `{result, state_patch, messages, carry, timing_ms}`
- Produces: `serve.PORTS: dict[str, int]`, `serve.build(service) -> FastAPI`, `serve.warm(service) -> None`, `serve.main(argv) -> int`

- [x] **Step 1: Write the failing test**

`backend/tests/test_ms_agent_app.py`:

```python
"""[Microservices] One agent container answers exactly as the in-process worker would."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import config
from app.agents import HumanInTheLoopAgent, SafetyOverrideAgent, SeverityClassifierAgent
from app.agents.base import CaseState
from app.microservices import wire
from app.microservices.agent_app import build_agent_app

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
    assert client.get("/metrics").status_code == 200


@pytest.mark.parametrize("slug", ["classifier", "safety", "routing", "reflection", "hitl", "handoff"])
def test_every_agent_builds(slug):
    assert build_agent_app(slug).title == f"careroute-{slug}-agent"


def test_classifier_worker_is_the_real_class():
    from app.microservices.workers import worker_factory
    assert isinstance(worker_factory("classifier")(), SeverityClassifierAgent)
    assert asyncio.iscoroutinefunction(SeverityClassifierAgent.run)
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ms_agent_app.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.microservices.agent_app'`

- [x] **Step 3: Implement**

`backend/app/microservices/workers.py`:

```python
"""[Microservices] What each agent container runs, and how a worker is built.

Stateless per request: every `/v1/invoke` gets a FRESH worker, so two cases in
flight can never see each other's inbox or Safety result (the bug
PipelineOrchestrator.new_session exists to prevent in-process). The expensive,
case-independent parts — the clinic dataset, the hours snapshot, the OneMap
client, the route cache, Safety's semantic layer — are built ONCE per
container and handed to each worker, exactly as new_session does.
"""
from __future__ import annotations

from collections.abc import Callable

from ..agents.classifier import SeverityClassifierAgent
from ..agents.handoff import ClinicianHandoffAgent
from ..agents.hitl import HumanInTheLoopAgent
from ..agents.reflection import ReflectionAgent
from ..agents.routing import CareRoutingAgent
from ..agents.safety import SafetyOverrideAgent

AGENT_CLASSES: dict[str, type] = {
    "classifier": SeverityClassifierAgent,
    "safety": SafetyOverrideAgent,
    "routing": CareRoutingAgent,
    "reflection": ReflectionAgent,
    "hitl": HumanInTheLoopAgent,
    "handoff": ClinicianHandoffAgent,
}

#: The worker methods the orchestrator calls on each agent. `emit` is on every
#: agent because an announcement is built from the CURRENT state (Reflection's
#: reads `state.reflection`, which the orchestrator sets after the loop).
OPS: dict[str, frozenset[str]] = {
    "classifier": frozenset({"run", "emit"}),
    "safety": frozenset({"prescreen", "areason", "run", "emit"}),
    "routing": frozenset({"run", "emit"}),
    "reflection": frozenset({"areason", "run", "emit"}),
    "hitl": frozenset({"run", "emit"}),
    "handoff": frozenset({"run", "emit"}),
}

#: Instance attributes that a worker's emit() reads and its run() wrote.
#: Everything else emit() needs is on CaseState. Safety alone builds its
#: `safety.override` message from its own last result, so that result makes a
#: round trip through the gateway as `carry` instead of living in a process.
CARRY: dict[str, tuple[str, ...]] = {"safety": ("_last_result", "_last_protocol_issues")}


def worker_factory(slug: str) -> Callable[[], object]:
    if slug == "safety":
        template = SafetyOverrideAgent()
        return lambda: SafetyOverrideAgent(semantic=template.semantic)
    if slug == "routing":
        template = CareRoutingAgent()
        return lambda: CareRoutingAgent(
            lookup=template.lookup, hours=template.hours, maps=template.maps,
            use_onemap=False, route_cache=template._route_cache,
        )
    if slug in AGENT_CLASSES:
        return AGENT_CLASSES[slug]
    raise KeyError(f"unknown agent {slug!r}")
```

`backend/app/microservices/common.py`:

```python
"""[Microservices] What every service container shares: the internal-token
check, the readiness flag and the three probe endpoints."""
from __future__ import annotations

import hmac

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from .. import config, metrics

_READY: set[str] = set()


def mark_ready(name: str) -> None:
    """Called by serve.warm() once the expensive start-up work is done."""
    _READY.add(name)


def check_token(token: str | None) -> None:
    expected = config.INTERNAL_TOKEN
    if expected and not hmac.compare_digest(token or "", expected):
        raise HTTPException(status_code=401, detail="internal token required")


def add_common_routes(app: FastAPI, name: str) -> None:
    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": name}

    @app.get("/ready")
    async def ready() -> dict:
        if name not in _READY:
            raise HTTPException(status_code=503, detail="warming up")
        return {"ready": True, "service": name}

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)
```

`backend/app/microservices/agent_app.py`:

```python
"""[Microservices] The HTTP face of ONE agent.

`python -m app.microservices.serve <slug>` runs this for one worker. The worker
class is the SAME class the in-process pipeline uses; this module only moves
CaseState across the network and back. It never logs the state: it is patient
data.
"""
from __future__ import annotations

import inspect
import time

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from .. import correlation
from ..agents import registry
from . import wire
from .common import add_common_routes, check_token
from .workers import CARRY, OPS, worker_factory


class InvokeRequest(BaseModel):
    op: str
    case_id: str | None = None
    state: dict
    inbox: list[dict] = Field(default_factory=list)
    consumed: bool = False
    carry: dict = Field(default_factory=dict)


def build_agent_app(slug: str) -> FastAPI:
    make_worker = worker_factory(slug)
    ops = OPS[slug]
    carry_attrs = CARRY.get(slug, ())
    app = FastAPI(title=f"careroute-{slug}-agent")
    add_common_routes(app, slug)

    @app.get("/v1/capability")
    async def capability() -> dict:
        return registry.describe(slug)

    @app.post("/v1/invoke")
    async def invoke(
        req: InvokeRequest,
        x_internal_token: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None),
    ) -> dict:
        check_token(x_internal_token)
        if req.op not in ops:
            raise HTTPException(status_code=404, detail=f"{slug} has no op {req.op!r}")
        try:
            state = wire.state_from_wire(req.state)
            inbox = [wire.message_from_wire(m) for m in req.inbox]
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        cid_token = correlation.bind(correlation.sanitise(x_correlation_id) or correlation.new_correlation_id())
        case_token = correlation.bind_case(req.case_id) if req.case_id else None
        try:
            before = wire.state_to_wire(state)
            worker = make_worker()
            for attr in carry_attrs:
                if attr in req.carry:
                    setattr(worker, attr, req.carry[attr])
            if req.consumed:
                worker.consume(inbox)
            start = time.perf_counter()
            result, messages = None, []
            if req.op == "prescreen":
                result = worker.prescreen(state.raw_text)
            elif req.op == "emit":
                messages = [wire.message_to_wire(worker.emit(state))]
            else:
                out = getattr(worker, req.op)(state)
                if inspect.isawaitable(out):
                    out = await out
                # areason's return value is internal to the worker; its effect is the patch.
                result = None if req.op == "areason" else out
            carry = {attr: getattr(worker, attr) for attr in carry_attrs} if req.op == "run" else {}
            return jsonable_encoder({
                "result": result,
                "state_patch": wire.state_patch(before, state),
                "messages": messages,
                "carry": carry,
                "timing_ms": round((time.perf_counter() - start) * 1000, 2),
            })
        finally:
            if case_token is not None:
                correlation.reset_case(case_token)
            correlation.reset(cid_token)

    return app
```

`backend/app/microservices/serve.py`:

```python
"""[Microservices] Container entrypoint: `python -m app.microservices.serve <service>`."""
from __future__ import annotations

import argparse

import uvicorn
from fastapi import FastAPI

from .. import correlation
from .common import mark_ready

PORTS: dict[str, int] = {
    "classifier": 8101, "safety": 8102, "routing": 8103,
    "reflection": 8104, "hitl": 8105, "handoff": 8106,
}


def build(service: str) -> FastAPI:
    from .agent_app import build_agent_app

    return build_agent_app(service)


def warm(service: str) -> None:
    """Pay the first request's start-up cost now, on the main thread."""
    if service == "classifier":
        from ..ml.model import get_model

        get_model()
    mark_ready(service)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one CareRoute service container.")
    parser.add_argument("service", choices=sorted(PORTS))
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - a container listens on its own interface
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    correlation.install()
    app = build(args.service)
    warm(args.service)
    uvicorn.run(app, host=args.host, port=args.port or PORTS[args.service])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_ms_agent_app.py -q`
Expected: all pass. If `test_safety_emit_rebuilds_the_announcement_from_carry` fails on the payload, `SafetyOverrideAgent.emit` reads another attribute set by `run()`. Find it with `grep -n "self\._" app/agents/safety.py` and add it to `CARRY["safety"]`; don't change the test.

- [x] **Step 5: Lint and commit**

```bash
.venv/Scripts/python -m ruff check app tests/test_ms_agent_app.py
git add app/microservices/workers.py app/microservices/common.py app/microservices/agent_app.py app/microservices/serve.py tests/test_ms_agent_app.py
git commit -m "feat(microservices): one agent per service — /v1/invoke over the real worker classes"
```

---

### Task 3: `RemoteAgent` — the gateway side

**Files:**
- Create: `backend/app/microservices/remote.py`
- Modify: `backend/app/metrics.py` (the `AGENT_DURATION` block at ~line 80, the `except ImportError` block at ~line 181, and a helper after `observe_agent` at ~line 227)
- Test: `backend/tests/test_ms_remote.py`

**Interfaces:**
- Consumes: `wire.*`, `workers.AGENT_CLASSES`, `agent_app.build_agent_app`, `AgentUnavailableError`, `llm._Breaker`
- Produces: `remote.RemoteAgent(slug)` with `SLUG`, `COMMS`, `CONTRACT`, `TOOL_ALLOWLIST`, `consume(inbox)`, `async run(state) -> dict`, `async areason(state) -> None`, `async prescreen(text) -> bool`, `async emit(state) -> AgentMessage`
- Produces: `remote.configure(factory: Callable[[str], httpx.AsyncClient] | None)`, `remote.reset()`, `remote.remote_workers() -> dict[str, RemoteAgent]`, `remote.agent_health() -> dict[str, dict]`
- Produces: `metrics.observe_agent_call(agent: str, outcome: str)` (`outcome` ∈ `ok | error | rejected | breaker_open`)

- [x] **Step 1: Write the failing test**

`backend/tests/test_ms_remote.py`:

```python
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


def _stub(reply: dict) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/invoke")
    async def invoke() -> dict:
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


def test_remote_workers_cover_every_orchestrated_agent():
    assert set(remote.remote_workers()) == {"classifier", "safety", "routing", "reflection", "hitl", "handoff"}
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ms_remote.py -q`
Expected: FAIL with `ImportError: cannot import name 'remote'`

- [x] **Step 3: Implement the metric**

In `backend/app/metrics.py`, directly after the `AGENT_DURATION = Histogram(...)` definition inside the `try:` block:

```python
    # [Microservices] One count per gateway -> agent-container call, by outcome
    # (ok / error / rejected / breaker_open). The AgentDown alert reads this.
    AGENT_CALLS = Counter(
        "careroute_agent_calls_total",
        "Gateway calls to agent containers, by agent and outcome",
        ["agent", "outcome"],
    )
```

In the `except` fallback block, change `    AGENT_DURATION = None` to `    AGENT_DURATION = AGENT_CALLS = None`.

After `observe_agent(...)`:

```python
def observe_agent_call(agent: str, outcome: str) -> None:
    """[Microservices] Count one gateway -> agent call. No-op without prometheus_client."""
    if AGENT_CALLS is None:
        return
    AGENT_CALLS.labels(agent=agent, outcome=outcome).inc()
```

- [x] **Step 4: Implement `remote.py`**

`backend/app/microservices/remote.py`:

```python
"""[Microservices] The gateway side of the agent contract.

`RemoteAgent` stands in for a worker object inside PipelineOrchestrator: it
carries the worker's SLUG / COMMS / CONTRACT, and `run`, `areason`, `prescreen`
and `emit` become POSTs to that agent's container (agent_app.py). Everything
the orchestrator already enforces still applies, and the network boundary adds
two checks of its own:

  * a reply may only write the CaseState fields in the agent's CONTRACT.writes;
  * an announcement must come from that agent and carry an intent in its
    COMMS.publishes (the same enforce_comms the in-process worker calls).

A reply that fails either check is treated exactly like a dead container:
AgentUnavailableError, and the orchestrator degrades the step. A lying agent
is not trusted more than a silent one.

Retries: one, and only on a connection error (the container may be
restarting). A timeout is not retried — the agent may still be working, and a
second copy of the same LLM call doubles the cost for no gain.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from .. import config, correlation, metrics
from ..agents.base import AgentUnavailableError, CaseState
from ..agents.messaging import AgentMessage, CommsAccessError, enforce_comms
from ..llm import _Breaker  # same semantics and thresholds as the LLM provider chain
from . import wire
from .workers import AGENT_CLASSES

logger = logging.getLogger("careroute.remote")

ClientFactory = Callable[[str], httpx.AsyncClient]
_client_factory: ClientFactory | None = None
_BREAKERS: dict[str, _Breaker] = {}


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
        data = await self._invoke("run", state)
        self._carry = dict(data.get("carry") or {})
        return dict(data.get("result") or {})

    async def areason(self, state: CaseState) -> None:
        await self._invoke("areason", state)

    async def prescreen(self, text: str) -> bool:
        data = await self._invoke("prescreen", CaseState(raw_text=text))
        return bool(data.get("result"))

    async def emit(self, state: CaseState) -> AgentMessage:
        data = await self._invoke("emit", state, carry=self._carry)
        try:
            messages = data.get("messages") or []
            if len(messages) != 1:
                raise wire.WireError(f"expected one message, got {len(messages)}")
            message = wire.message_from_wire(messages[0])
            if message.sender != self.SLUG:
                raise wire.WireError(f"sender {message.sender!r} is not {self.SLUG!r}")
            enforce_comms(self, message.intent)
        except (CommsAccessError, KeyError, TypeError, ValueError) as exc:
            self._reject("emit", exc)
        return message

    # -- transport ------------------------------------------------------------
    async def _invoke(self, op: str, state: CaseState, *, carry: dict | None = None) -> dict:
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
        try:
            data = response.json()
            if op in ("run", "areason"):
                wire.apply_patch(state, data.get("state_patch") or {},
                                 allowed=self.CONTRACT.writes, agent=self.SLUG)
        except (AttributeError, TypeError, ValueError) as exc:
            self._reject(op, exc)
        breaker.record_success()
        metrics.observe_agent_call(self.SLUG, "ok")
        return data

    async def _post(self, body: dict) -> httpx.Response:
        headers = {"X-Correlation-Id": correlation.get(), "X-Internal-Token": config.INTERNAL_TOKEN}
        last = "no attempt"
        for _attempt in range(2):
            try:
                async with _client(self.SLUG) as client:
                    return await client.post("/v1/invoke", json=body, headers=headers)
            except httpx.ConnectError as exc:
                last = type(exc).__name__  # container restarting: worth exactly one retry
            except httpx.HTTPError as exc:
                raise _CallFailed(type(exc).__name__) from None
        raise _CallFailed(last)

    def _reject(self, op: str, exc: Exception) -> None:
        _breaker(self.SLUG).record_failure(time.monotonic())
        metrics.observe_agent_call(self.SLUG, "rejected")
        logger.warning("agent=%s op=%s reply rejected: %s", self.SLUG, op, type(exc).__name__)
        raise AgentUnavailableError(f"{self.SLUG} reply rejected: {exc}") from None
```

- [x] **Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ms_remote.py tests/test_ms_agent_app.py -q`
Expected: all pass.

- [x] **Step 6: Lint and commit**

```bash
.venv/Scripts/python -m ruff check app tests/test_ms_remote.py
git add app/microservices/remote.py app/metrics.py tests/test_ms_remote.py
git commit -m "feat(microservices): RemoteAgent — lane and COMMS checks at the network boundary, breaker, one retry"
```

---

### Task 4: Orchestrator over either transport, and degradation

**Files:**
- Modify: `backend/app/agents/orchestration.py` (imports ~line 57-74; `new_session` ~line 160; `_safety_pregate` ~line 322; `_reflection_rerun` ~line 351-417; `orchestrate` ~line 426-679)
- Modify: `backend/app/main.py` (`lifespan` ~line 51-81; `orchestrator = ...` ~line 140; `health()` ~line 476)
- Modify: `backend/app/models.py` (`HealthResponse` ~line 131)
- Test: `backend/tests/test_ms_degradation.py`, `backend/tests/test_ms_transport_parity.py`

**Interfaces:**
- Consumes: `remote.remote_workers()`, `remote.configure()`, `remote.agent_health()`, `AgentUnavailableError`, `routing.tier_for_acuity(code: str) -> str`
- Produces: `PipelineOrchestrator._call(agent, method, *args)`, `_run_worker(state, slug, agent) -> (dict, float)`, `_reason(agent, state)`, `_announce(bus, state, agent, result, audit) -> dict | None`, `_degrade(state, slug, exc) -> dict`; module constant `UNAVAILABLE = "unavailable"` (the `source` of a degraded step)
- Produces: `HealthResponse.agents: dict[str, dict]`

- [x] **Step 1: Write the failing degradation test**

`backend/tests/test_ms_degradation.py`:

```python
"""[Microservices] An agent container that does not answer never makes the outcome less safe."""
from __future__ import annotations

import asyncio

import pytest

from app.agents import SymptomIntakeAgent
from app.agents.base import AgentUnavailableError, CaseState
from app.agents.routing import tier_for_acuity
from app.microservices.workers import AGENT_CLASSES

CHEST_PAIN = "crushing chest pain radiating to my left arm and sweating"
MILD = "I have had a mild headache since this morning"


class _Down:
    """A RemoteAgent whose container is gone."""

    def __init__(self, slug: str) -> None:
        cls = AGENT_CLASSES[slug]
        self.SLUG, self.COMMS, self.CONTRACT = cls.SLUG, cls.COMMS, cls.CONTRACT
        self.TOOL_ALLOWLIST = getattr(cls, "TOOL_ALLOWLIST", [])

    def consume(self, inbox) -> None:
        pass

    async def run(self, state):
        raise AgentUnavailableError(f"{self.SLUG} down")

    async def areason(self, state):
        raise AgentUnavailableError(f"{self.SLUG} down")

    async def prescreen(self, text):
        raise AgentUnavailableError(f"{self.SLUG} down")

    async def emit(self, state):
        raise AgentUnavailableError(f"{self.SLUG} down")


def _drive(down: str, text: str) -> tuple[CaseState, list[dict]]:
    orchestrator = SymptomIntakeAgent(**{down: _Down(down)})
    state = CaseState(raw_text=text)

    async def _no_delay():
        return None

    async def go():
        return [e async for e in orchestrator.orchestrate(
            state, audit=lambda **_kw: None, log=lambda *_a: None, delay=_no_delay)]

    return state, asyncio.run(go())


def _results(events, agent):
    return [e for e in events if e.get("event") == "agent_result" and e.get("agent") == agent]


@pytest.mark.parametrize("down", ["classifier", "safety", "routing", "hitl"])
def test_a_decision_agent_down_forces_human_review(down):
    state, events = _drive(down, MILD)
    assert state.escalated is True
    assert "unavailable" in (state.escalation_reason or "")
    assert _results(events, down)[0]["data"]["source"] == "unavailable"
    assert _results(events, "reflection"), "the pipeline must still complete"


def test_safety_down_fails_closed_at_the_route_gate():
    state, events = _drive("safety", CHEST_PAIN)
    assert state.escalated is True
    assert any(e.get("event") == "safety_protocol_issue" for e in events)


def test_routing_down_still_gives_the_care_tier():
    state, _ = _drive("routing", MILD)
    assert state.care_tier == tier_for_acuity(state.acuity_code)
    assert "unavailable" in (state.routing_reason or "")


def test_reflection_down_stops_the_loop_and_completes():
    state, _ = _drive("reflection", MILD)
    assert state.reflection["source"] == "unavailable"
    assert state.reflection["loop"]["stopReason"] == "agent_unavailable"


def test_handoff_down_leaves_no_packet():
    state, events = _drive("handoff", CHEST_PAIN)
    assert state.escalated is True
    assert state.handoff_summary == ""
    assert _results(events, "handoff")[0]["data"]["source"] == "unavailable"
```

- [x] **Step 2: Run it to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ms_degradation.py -q`
Expected: FAIL. `AgentUnavailableError` propagates out of `orchestrate`, and `safety.run(state)` is called without `await` on an async stub (`TypeError` / coroutine never awaited).

- [x] **Step 3: Orchestration imports and helpers**

In `backend/app/agents/orchestration.py`, replace the import block:

```python
import time
from collections.abc import Awaitable, Callable

from .. import metrics, rag, redflags
```

with:

```python
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .. import config, metrics, rag, redflags
```

In the `from .base import (...)` list, add `AgentUnavailableError,`. Replace `from .routing import CareRoutingAgent` with `from .routing import CareRoutingAgent, tier_for_acuity`.

Below `ORCHESTRATOR_PUBLISHES = ...` add:

```python
#: The `source` of a step whose agent container did not answer.
UNAVAILABLE = "unavailable"
```

At the top of `new_session`'s body, before `return type(self)(...)`:

```python
        if config.AGENT_TRANSPORT == "http":
            # [Microservices] Remote workers hold nothing expensive and no state
            # beyond one request: a fresh set per request, nothing handed down.
            from ..microservices.remote import remote_workers

            return type(self)(**remote_workers())
```

Directly after `_timed_sync`, add the helpers:

```python
    # ------------------------------------------------------------------
    # [Microservices] Calling a worker that may live in another container.
    # ------------------------------------------------------------------
    async def _call(self, agent: object, method: str, *args: Any) -> Any:
        """`agent.method(*args)`, awaited when it is async.

        In-process workers mix sync (`safety.run`, `hitl.run`, every `emit`)
        and async (`classifier.run`) methods; a RemoteAgent's are all async
        because each one is a network call. This is the one place that
        difference is absorbed, so the step sequence below reads the same for
        both transports.
        """
        out = getattr(agent, method)(*args)
        if inspect.isawaitable(out):
            out = await out
        return out

    async def _run_worker(self, state: CaseState, slug: str, agent: object) -> tuple[dict, float]:
        """Run one worker; a container that cannot answer degrades the step."""
        start = time.perf_counter()
        try:
            result = await self._call(agent, "run", state)
        except AgentUnavailableError as exc:
            result = self._degrade(state, slug, exc)
        return result, time.perf_counter() - start

    async def _reason(self, agent: object, state: CaseState) -> None:
        """The additive LLM layers (Safety's semantic flags, Reflection's critic).
        Losing one leaves the deterministic step to decide alone — exactly what
        happens when the LLM itself is down — so it is not an error here."""
        try:
            await self._call(agent, "areason", state)
        except AgentUnavailableError:
            pass

    async def _announce(self, bus: MessageBus, state: CaseState, agent: object, result: dict,
                        audit: Callable[..., None]) -> dict | None:
        """Publish `agent`'s message for this step; None when it has none to give."""
        if result.get("source") == UNAVAILABLE:
            return None
        try:
            message = await self._call(agent, "emit", state)
        except AgentUnavailableError as exc:
            audit(actor=getattr(agent, "SLUG", "agent"), action="announcement_unavailable", detail=str(exc))
            return None
        return self.publish(bus, state, message, audit)

    def _degrade(self, state: CaseState, slug: str, exc: Exception) -> dict:
        """[Microservices][Responsible-AI] The step's agent did not answer.

        Never a less cautious outcome: any of the four decision agents missing
        puts the case in front of a clinician. Safety needs no special case
        here — its missing `safety.override` makes the route gate fail closed
        below, exactly as a malformed reply would. Acuity is never touched.
        """
        label = AGENT_LABELS.get(slug, slug)
        result: dict = {"source": UNAVAILABLE, "error": type(exc).__name__}
        if slug == "reflection":
            result.update(passed=False, issues=[f"{label} unavailable"], corrections=[], rerun_suggested=False)
            return result
        if slug == "handoff":
            return result
        if slug == "safety":
            result.update(triggered=False, rule=None, reason=f"{label} unavailable",
                          prior_acuity=state.acuity_code, forced_acuity=None, channel=None)
        if slug == "routing":
            state.care_tier = tier_for_acuity(state.acuity_code)
            state.routing_reason = "Clinic routing unavailable; showing the care tier only."
        reason = f"{label} unavailable — escalated for clinician review."
        state.escalated = True
        state.escalation_reason = (
            f"{state.escalation_reason} {reason}".strip() if state.escalation_reason else reason
        )
        return result
```

- [x] **Step 4: Route every worker call through the helpers**

Replace `_safety_pregate` with:

```python
    async def _safety_pregate(self, state: CaseState) -> None:
        """[Agentic Routing][AI-Security] Safety-first conditional routing: run
        the deterministic red-flag check EARLY (before the classifier) as a
        cheap gate. On an obvious red flag we mark a fast-path so the classifier
        can skip redundant LLM work -- the authoritative Safety-Override worker
        still runs in-order and is the only thing that can force acuity, and it
        can only ever RAISE it (escalation-only invariant preserved). An
        unreachable Safety container leaves the fast path off: it only ever
        SKIPS work, so off is the cautious default."""
        try:
            state.safety_fast_path = bool(await self._call(self.safety, "prescreen", state.raw_text))
        except AgentUnavailableError:
            state.safety_fast_path = False
```

In `_reflection_rerun`, replace from `self.deliver_inbox(bus, self.classifier)` through `self.safety.run(state)`:

```python
        self.deliver_inbox(bus, self.classifier)
        rerun_result, _ = await self._run_worker(state, "classifier", self.classifier)
        # Re-apply the deterministic safety gate (re-adds its signed explanation
        # contribution and can only raise acuity).
        self.deliver_inbox(bus, self.safety)
        await self._run_worker(state, "safety", self.safety)
```

and replace the last four worker lines (`self.deliver_inbox(bus, self.routing)` … `self.publish(bus, state, self.hitl.emit(state), audit)`):

```python
        self.deliver_inbox(bus, self.routing)
        routing_rerun, _ = await self._run_worker(state, "routing", self.routing)
        await self._announce(bus, state, self.routing, routing_rerun, audit)
        self.deliver_inbox(bus, self.hitl)
        hitl_rerun, _ = await self._run_worker(state, "hitl", self.hitl)
        await self._announce(bus, state, self.hitl, hitl_rerun, audit)
```

In `orchestrate`, make these exact replacements. Leave every other line (events, audit, log, metrics, comments) as it is.

1. `self._safety_pregate(state)` → `await self._safety_pregate(state)`.
2. Classifier. Replace `classifier_result, dur = await self._timed_async(self.classifier.run(state))` with `classifier_result, dur = await self._run_worker(state, "classifier", self.classifier)`. Then replace:
   ```python
        classified_event = self.publish(bus, state, self.classifier.emit(state), audit)
        yield classified_event
   ```
   with:
   ```python
        classified_event = await self._announce(bus, state, self.classifier, classifier_result, audit)
        if classified_event is not None:
            yield classified_event
   ```
   and `safety_request = self.safety_request(state, int(classified_event["seq"]))` with:
   ```python
        # -1 when the classifier made no announcement: Safety then reports the
        # missing `acuity.classified` as a protocol issue and the gate fails closed.
        safety_request = self.safety_request(
            state, int(classified_event["seq"]) if classified_event is not None else -1)
   ```
3. Safety. Replace
   ```python
        await self.safety.areason(state)
        safety_result, dur = self._timed_sync(lambda: self.safety.run(state))
   ```
   with
   ```python
        await self._reason(self.safety, state)
        safety_result, dur = await self._run_worker(state, "safety", self.safety)
   ```
   and `yield self.publish(bus, state, self.safety.emit(state), audit)` with
   ```python
        safety_event = await self._announce(bus, state, self.safety, safety_result, audit)
        if safety_event is not None:
            yield safety_event
   ```
4. Routing. `routing_result, dur = await self._timed_async(self.routing.run(state))` → `routing_result, dur = await self._run_worker(state, "routing", self.routing)`. Then `yield self.publish(bus, state, self.routing.emit(state), audit)` →
   ```python
        routing_event = await self._announce(bus, state, self.routing, routing_result, audit)
        if routing_event is not None:
            yield routing_event
   ```
5. HITL. `hitl_result, dur = self._timed_sync(lambda: self.hitl.run(state))` → `hitl_result, dur = await self._run_worker(state, "hitl", self.hitl)`. Then `yield self.publish(bus, state, self.hitl.emit(state), audit)` →
   ```python
        hitl_event = await self._announce(bus, state, self.hitl, hitl_result, audit)
        if hitl_event is not None:
            yield hitl_event
   ```
6. Reflection. Replace both `await self.reflection.areason(state)` calls (first pass, and the one inside the `while` loop) with `await self._reason(self.reflection, state)`. Replace both `reflection_result = self.reflection.run(state)` with `reflection_result, _ = await self._run_worker(state, "reflection", self.reflection)`. Directly after the `while` loop ends (before `reflection_result["corrections"] = all_corrections`) insert:
   ```python
        if reflection_result.get("source") == UNAVAILABLE:
            stop_reason = "agent_unavailable"
   ```
   Replace `yield self.publish(bus, state, self.reflection.emit(state), audit)` with
   ```python
        reflection_event = await self._announce(bus, state, self.reflection, reflection_result, audit)
        if reflection_event is not None:
            yield reflection_event
   ```
7. Handoff. `handoff_result, dur = await self._timed_async(self.handoff.run(state))` → `handoff_result, dur = await self._run_worker(state, "handoff", self.handoff)`. `yield self.publish(bus, state, self.handoff.emit(state), audit)` →
   ```python
            handoff_event = await self._announce(bus, state, self.handoff, handoff_result, audit)
            if handoff_event is not None:
                yield handoff_event
   ```

The intake step (`self.intake.run` / `self.intake.emit`) stays as it is. Intake is this object and always runs in the gateway.

- [x] **Step 5: Run degradation tests and the existing agent suite**

Run: `.venv/Scripts/python -m pytest tests/test_ms_degradation.py tests/agents tests/test_agents.py tests/test_triage_eval.py -q`
Expected: all pass. The existing suites must pass unchanged. If one fails, the edit changed behaviour for in-process workers. Fix the edit, not the test.

- [x] **Step 6: Write the failing transport-parity test**

`backend/tests/test_ms_transport_parity.py`:

```python
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
```

- [x] **Step 7: Run it**

Run: `.venv/Scripts/python -m pytest tests/test_ms_transport_parity.py -q -x`
Expected: all pass. If a case differs, don't loosen the comparison. The diff shows which field crossed the wire wrongly:
- a field that's missing over HTTP means the agent wrote outside its `CONTRACT.writes`. The lane check rejected the reply, so the step degraded (look for `"source": "unavailable"`). Add the field to that agent's `CONTRACT.writes` in `app/agents/<agent>.py` and run `tests/agents/test_contracts.py` again.
- a payload that differs only in type (tuple vs list) means `_stable` needs to normalise it. Add the normalisation there, not in production code.

- [x] **Step 8: Wire the gateway (`main.py`, `models.py`)**

`backend/app/models.py`, add to `HealthResponse` after `breakers`:

```python
    # [Microservices] Per-agent-container breaker state when AGENT_TRANSPORT=http.
    agents: dict[str, dict] = Field(default_factory=dict)
```

`backend/app/main.py`, replace `orchestrator = SymptomIntakeAgent()` with:

```python
if config.AGENT_TRANSPORT == "http":
    # [Microservices] Every other worker is its own container; this process is
    # the intake agent + orchestrator + public API. See app/microservices/.
    from .microservices.remote import remote_workers

    orchestrator = SymptomIntakeAgent(**remote_workers())
else:
    orchestrator = SymptomIntakeAgent()
```

In `lifespan`, replace from the line `    from . import rag_embed` through `        warmup_task.cancel()` with:

```python
    # [Microservices] With AGENT_TRANSPORT=http the model and the embedder live
    # in the classifier and rag-service containers; this process loads neither.
    in_process = config.AGENT_TRANSPORT != "http"
    if in_process:
        from . import rag_embed

        rag_embed.warm()

    # The reference must be held for the task's lifetime: the event loop keeps
    # only a weak reference, so a bare `create_task(...)` can be garbage
    # collected mid-flight and the warmup silently cancelled.
    warmup_task = asyncio.create_task(_warm()) if in_process else None
    try:
        yield
    finally:
        if warmup_task is not None:
            warmup_task.cancel()
```

In `health()`, before `return HealthResponse(`:

```python
    agents: dict[str, dict] = {}
    if config.AGENT_TRANSPORT == "http":
        from .microservices.remote import agent_health

        agents = agent_health()
```

and add `agents=agents,` to the `HealthResponse(...)` arguments.

- [x] **Step 9: Full backend suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: the same pass/skip counts as before this plan plus the new `test_ms_*` tests, and no failures. (Record the baseline first with `git stash; pytest -q; git stash pop` if you don't have it.)

- [x] **Step 10: Lint and commit**

```bash
.venv/Scripts/python -m ruff check app tests/test_ms_degradation.py tests/test_ms_transport_parity.py
git add app/agents/orchestration.py app/main.py app/models.py tests/test_ms_degradation.py tests/test_ms_transport_parity.py
git commit -m "feat(microservices): orchestrate over HTTP with degradation that never lowers caution"
```

---

### Task 5: Shared services — `llm-gateway` and `rag-service`

**Files:**
- Modify: `backend/app/llm.py` (imports ~line 29-31; start of `complete()` ~line 583; a new helper after `complete`)
- Modify: `backend/app/rag.py` (imports ~line 21-30; start of `retrieve_detailed` ~line 364; a new helper before it)
- Create: `backend/app/microservices/llm_app.py`, `backend/app/microservices/rag_app.py`
- Modify: `backend/app/microservices/serve.py`
- Test: `backend/tests/test_ms_shared_services.py`

**Interfaces:**
- Consumes: `common.*`, `config.LLM_GATEWAY_URL`, `config.RAG_SERVICE_URL`, `config.INTERNAL_TOKEN`
- Produces: `llm._GATEWAY_TRANSPORT: httpx.AsyncBaseTransport | None`, `llm._complete_via_gateway(system, prompt, json_mode, json_schema, task) -> str`
- Produces: `rag._SERVICE_TRANSPORT: httpx.BaseTransport | None`, `rag._retrieve_via_service(query_text, top_k) -> dict | None`
- Produces: `llm_app.build_llm_app() -> FastAPI` (`POST /v1/complete` → `{"text"}`; 503 when no provider), `rag_app.build_rag_app() -> FastAPI` (`POST /v1/retrieve` → `{"hits","mode","embedder"}`)
- Produces: `serve.PORTS` gains `"llm": 8107, "rag": 8108`

- [x] **Step 1: Write the failing test**

`backend/tests/test_ms_shared_services.py`:

```python
"""[Microservices] llm-gateway and rag-service, from both ends."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, llm, rag
from app.llm import complete as REAL_COMPLETE  # captured before conftest disables the LLM
from app.microservices.llm_app import build_llm_app
from app.microservices.rag_app import build_rag_app


@pytest.fixture(autouse=True)
def _no_transports(monkeypatch):
    monkeypatch.setattr(llm, "_GATEWAY_TRANSPORT", None)
    monkeypatch.setattr(rag, "_SERVICE_TRANSPORT", None)


def test_complete_goes_through_the_gateway_when_configured(monkeypatch):
    seen = {}

    def gateway(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"text": "ok from gateway"})

    monkeypatch.setattr(config, "LLM_GATEWAY_URL", "http://llm")
    monkeypatch.setattr(llm, "_GATEWAY_TRANSPORT", httpx.MockTransport(gateway))
    assert asyncio.run(REAL_COMPLETE("sys", "prompt", task=None)) == "ok from gateway"
    assert b'"prompt"' in seen["body"]


@pytest.mark.parametrize("reply", [httpx.Response(503), httpx.Response(500)])
def test_gateway_failure_is_llm_unavailable(monkeypatch, reply):
    monkeypatch.setattr(config, "LLM_GATEWAY_URL", "http://llm")
    monkeypatch.setattr(llm, "_GATEWAY_TRANSPORT", httpx.MockTransport(lambda _r: reply))
    with pytest.raises(llm.LLMUnavailableError):
        asyncio.run(REAL_COMPLETE("sys", "prompt"))


def test_llm_app_serves_complete(monkeypatch):
    async def fake(*_a, **_k):
        return "hello"

    monkeypatch.setattr(llm, "complete", fake)
    reply = TestClient(build_llm_app()).post("/v1/complete", json={"system": "s", "prompt": "p"})
    assert reply.json() == {"text": "hello"}


def test_llm_app_is_503_with_no_provider():
    # conftest's autouse fixture makes llm.complete raise LLMUnavailableError.
    reply = TestClient(build_llm_app()).post("/v1/complete", json={"system": "s", "prompt": "p"})
    assert reply.status_code == 503


def test_llm_app_refuses_to_point_at_itself(monkeypatch):
    monkeypatch.setattr(config, "LLM_GATEWAY_URL", "http://llm")
    with pytest.raises(RuntimeError):
        build_llm_app()


def test_retrieve_uses_the_service_when_configured(monkeypatch):
    hit = {"title": "T", "snippet": "S", "source": "src"}
    monkeypatch.setattr(config, "RAG_SERVICE_URL", "http://rag")
    monkeypatch.setattr(rag, "_SERVICE_TRANSPORT", httpx.MockTransport(
        lambda _r: httpx.Response(200, json={"hits": [hit], "mode": "hybrid", "embedder": "e"})))
    assert rag.retrieve_detailed("chest pain") == {"hits": [hit], "mode": "hybrid", "embedder": "e"}


def test_a_dead_rag_service_falls_back_to_local_retrieval(monkeypatch):
    monkeypatch.setattr(config, "RAG_SERVICE_URL", "http://rag")
    monkeypatch.setattr(rag, "_SERVICE_TRANSPORT", httpx.MockTransport(lambda _r: httpx.Response(500)))
    result = rag.retrieve_detailed("chest pain")
    assert result["hits"], "a dead rag-service must never cost the case its citations"


def test_rag_app_serves_retrieve():
    reply = TestClient(build_rag_app()).post("/v1/retrieve", json={"query": "chest pain", "top_k": 2})
    assert reply.status_code == 200
    assert reply.json()["hits"]
```

- [x] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ms_shared_services.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.microservices.llm_app'`

- [x] **Step 3: Client mode in `llm.py`**

Change `from . import config, llm_cost, metrics` to `from . import config, correlation, llm_cost, metrics`. Below `class LLMUnavailableError`, add:

```python
# [Microservices] Tests route the gateway call through this (httpx.MockTransport).
_GATEWAY_TRANSPORT: httpx.AsyncBaseTransport | None = None
```

In `complete()`, directly after the kill-switch `if config.KILL_SWITCH: raise ...` block:

```python
    # [Microservices] Every container except llm-gateway reaches a model only
    # through it: one place holds the provider keys, the breakers, the cache and
    # the cost accounting.
    if config.LLM_GATEWAY_URL:
        return await _complete_via_gateway(system, prompt, json_mode, json_schema, task)
```

After `complete()`:

```python
async def _complete_via_gateway(system: str, prompt: str, json_mode: bool, json_schema: dict | None,
                                task: str | None) -> str:
    headers = {"X-Correlation-Id": correlation.get(), "X-Internal-Token": config.INTERNAL_TOKEN}
    body = {"system": system, "prompt": prompt, "json_mode": json_mode, "json_schema": json_schema, "task": task}
    try:
        async with httpx.AsyncClient(base_url=config.LLM_GATEWAY_URL, timeout=config.AGENT_TIMEOUT_SECONDS,
                                     transport=_GATEWAY_TRANSPORT) as client:
            response = await client.post("/v1/complete", json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise LLMUnavailableError(f"llm-gateway unreachable: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise LLMUnavailableError(f"llm-gateway HTTP {response.status_code}")
    try:
        return str(response.json()["text"])
    except (KeyError, TypeError, ValueError):
        raise LLMUnavailableError("llm-gateway returned an unusable body") from None
```

- [x] **Step 4: Client mode in `rag.py`**

Add imports in the existing blocks: `import httpx` among the third-party imports, and `from . import config` among the local imports (ruff `I` sorts them). Before `def retrieve_detailed`, add:

```python
# [Microservices] Tests route the service call through this (httpx.MockTransport).
_SERVICE_TRANSPORT: httpx.BaseTransport | None = None


def _retrieve_via_service(query_text: str, top_k: int) -> dict | None:
    """rag-service holds the corpus index and the embedder. On ANY failure this
    returns None and the caller retrieves locally (lexical): a dead rag-service
    costs retrieval quality, never a citation. Sync on purpose — every call
    site already runs retrieval on a worker thread (see rag_embed.warm)."""
    headers = {"X-Internal-Token": config.INTERNAL_TOKEN}
    try:
        with httpx.Client(base_url=config.RAG_SERVICE_URL, timeout=10.0, transport=_SERVICE_TRANSPORT) as client:
            response = client.post("/v1/retrieve", json={"query": query_text or "", "top_k": top_k},
                                   headers=headers)
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        data = response.json()
        return {"hits": list(data["hits"]), "mode": str(data["mode"]), "embedder": data.get("embedder")}
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.warning("rag-service unavailable (%s); retrieving locally", type(exc).__name__)
        return None
```

At the top of `retrieve_detailed`'s body (after the docstring, before the local imports):

```python
    if config.RAG_SERVICE_URL:
        remote = _retrieve_via_service(query_text, top_k)
        if remote is not None:
            return remote
```

- [x] **Step 5: The two services**

`backend/app/microservices/llm_app.py`:

```python
"""[Microservices] llm-gateway — the ONLY container given provider keys.

Every other container calls this instead of a provider (llm.complete sees
CAREROUTE_LLM_GATEWAY_URL and forwards). Here llm.complete runs the real
provider chain, with its breakers, router, cache and cost accounting, so the
cost and latency of every LLM call in the system land on one /metrics.
"""
from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .. import config, correlation, llm
from .common import add_common_routes, check_token


class CompleteRequest(BaseModel):
    system: str
    prompt: str
    json_mode: bool = False
    json_schema: dict | None = None
    task: str | None = None


def build_llm_app() -> FastAPI:
    if config.LLM_GATEWAY_URL:
        raise RuntimeError("llm-gateway must call providers itself; unset CAREROUTE_LLM_GATEWAY_URL here")
    app = FastAPI(title="careroute-llm-gateway")
    add_common_routes(app, "llm")

    @app.post("/v1/complete")
    async def complete(
        req: CompleteRequest,
        x_internal_token: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None),
    ) -> dict:
        check_token(x_internal_token)
        token = correlation.bind(correlation.sanitise(x_correlation_id) or correlation.new_correlation_id())
        try:
            text = await llm.complete(req.system, req.prompt, req.json_mode, req.json_schema, task=req.task)
        except llm.LLMUnavailableError:
            raise HTTPException(status_code=503, detail="no LLM provider available") from None
        finally:
            correlation.reset(token)
        return {"text": text}

    return app
```

`backend/app/microservices/rag_app.py`:

```python
"""[Microservices] rag-service — the corpus index and the embedder, loaded once
and shared by every agent that retrieves (classifier, handoff, the gateway)."""
from __future__ import annotations

from fastapi import FastAPI, Header
from pydantic import BaseModel, Field

from .. import config, rag
from .common import add_common_routes, check_token


class RetrieveRequest(BaseModel):
    query: str
    top_k: int = Field(default=2, ge=1, le=10)


def build_rag_app() -> FastAPI:
    if config.RAG_SERVICE_URL:
        raise RuntimeError("rag-service must retrieve itself; unset CAREROUTE_RAG_SERVICE_URL here")
    app = FastAPI(title="careroute-rag-service")
    add_common_routes(app, "rag")

    # A plain `def`: FastAPI runs it on its thread pool, which is where every
    # retrieval already runs (the embedder is warmed on the main thread first).
    @app.post("/v1/retrieve")
    def retrieve(req: RetrieveRequest, x_internal_token: str | None = Header(default=None)) -> dict:
        check_token(x_internal_token)
        return rag.retrieve_detailed(req.query, req.top_k)

    return app
```

In `backend/app/microservices/serve.py`, extend `PORTS` with `"llm": 8107, "rag": 8108,` and replace `build` and `warm`:

```python
def build(service: str) -> FastAPI:
    if service == "llm":
        from .llm_app import build_llm_app

        return build_llm_app()
    if service == "rag":
        from .rag_app import build_rag_app

        return build_rag_app()
    from .agent_app import build_agent_app

    return build_agent_app(service)


def warm(service: str) -> None:
    """Pay the first request's start-up cost now, on the main thread."""
    if service == "classifier":
        from ..ml.model import get_model

        get_model()
    elif service == "rag":
        from .. import rag_embed

        rag_embed.warm()  # MUST be the main thread: see rag_embed.warm
    mark_ready(service)
```

- [x] **Step 6: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ms_shared_services.py tests/test_ms_transport_parity.py -q`
Expected: all pass.

- [x] **Step 7: Lint and commit**

```bash
.venv/Scripts/python -m ruff check app tests/test_ms_shared_services.py
git add app/llm.py app/rag.py app/microservices/llm_app.py app/microservices/rag_app.py app/microservices/serve.py tests/test_ms_shared_services.py
git commit -m "feat(microservices): llm-gateway holds the only provider key; rag-service shares the index"
```

---

### Task 6: Redis — the escalation queue and audit trail survive a restart

**Files:**
- Create: `backend/app/persistence.py`
- Modify: `backend/app/store.py` (`Store.__init__` ~line 146, `save_case` ~line 164, `create_escalation_from_case` ~line 248, `decide_escalation` ~line 284, new `_load`, module singleton ~line 428)
- Modify: `backend/app/audit.py` (`AuditLog.__init__` ~line 73, `record` ~line 78, new `_load`, singleton ~line 127)
- Modify: `backend/requirements.txt` (add `redis==5.2.1`), `backend/requirements-dev.txt` (add `fakeredis==2.26.2`)
- Test: `backend/tests/test_ms_persistence.py`

**Interfaces:**
- Consumes: `config.REDIS_URL`
- Produces: `persistence.connect(url: str | None = None) -> redis.Redis | None`, `persistence.write(client, command: str, *args) -> None`, keys `KEY_CASES KEY_ESCALATIONS KEY_SESSIONS KEY_DIGESTS AUDIT_PREFIX`
- Produces: `Store(redis_client=None)`, `AuditLog(redis_client=None)`. The module singletons connect when `CAREROUTE_REDIS_URL` is set.

- [x] **Step 1: Install the dependencies**

Add `redis==5.2.1` to `backend/requirements.txt`, directly under `prometheus-client==0.21.1`, with the comment `# [Microservices] durable escalation queue + audit trail (app/persistence.py)`. Add `fakeredis==2.26.2` to `backend/requirements-dev.txt`.

Run: `.venv/Scripts/python -m pip install redis==5.2.1 fakeredis==2.26.2`
Expected: `Successfully installed ...`

- [x] **Step 2: Write the failing test**

`backend/tests/test_ms_persistence.py`:

```python
"""[Microservices] A restarted gateway still has every escalation a clinician has not seen."""
from __future__ import annotations

import fakeredis
import pytest
import redis

from app.audit import AuditLog
from app.models import Acuity, CaseRecord
from app.store import Store, now_iso


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture(autouse=True)
def _ground_truth_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("CAREROUTE_GROUND_TRUTH_LOG", str(tmp_path / "gt.jsonl"))


def _case(case_id: str = "case-1") -> CaseRecord:
    return CaseRecord(
        caseId=case_id, sessionId="sess-1", rawText="chest pain", language="en", isVoice=False,
        normalisedSymptoms="chest pain", acuity=Acuity.from_code("P2_EMERGENT"), confidence=0.4,
        careTier="ED", escalated=True, rationale="r", citations=[], evidence=["chest pain"],
        safetyTriggered=False, createdAt=now_iso(),
    )


def test_cases_and_escalations_survive_a_restart(r):
    first = Store(redis_client=r)
    case = _case()
    first.save_case(case)
    escalation = first.create_escalation_from_case(case, "low confidence")

    second = Store(redis_client=r)
    assert second.get_case(case.caseId) == case
    assert second.get_escalation(escalation.id) == escalation
    # The episodic-memory digest must still verify after the round trip.
    assert [c.caseId for c in second.recall_session("sess-1")] == [case.caseId]


def test_seed_escalations_are_not_duplicated_on_restart(r):
    first_ids = {e.id for e in Store(redis_client=r).list_escalations()}
    assert {e.id for e in Store(redis_client=r).list_escalations()} == first_ids


def test_a_decision_survives_a_restart(r):
    first = Store(redis_client=r)
    escalation = first.create_escalation_from_case(_case(), "why")
    first.decide_escalation(escalation.id, "agree", "seen", "dr-a", None)
    assert Store(redis_client=r).get_escalation(escalation.id).status == "decided"


def test_redis_down_never_fails_a_case():
    dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2,
                       decode_responses=True)
    store = Store(redis_client=dead)
    store.save_case(_case())
    assert store.get_case("case-1") is not None


def test_audit_chain_survives_a_restart(r):
    first = AuditLog(redis_client=r)
    first.record("case-1", actor="intake", action="llm", detail="a")
    first.record("case-1", actor="classifier", action="model", detail="b")

    second = AuditLog(redis_client=r)
    assert second.for_case("case-1") == first.for_case("case-1")
    assert second.verify_chain("case-1") is True
    assert second.record("case-1", actor="safety", action="ok", detail="c").seq == 3
    assert second.verify_chain("case-1") is True
```

- [x] **Step 3: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ms_persistence.py -q`
Expected: FAIL with `TypeError: Store.__init__() got an unexpected keyword argument 'redis_client'`

- [x] **Step 4: `persistence.py`**

```python
"""[Microservices] Durable backing for the case store, escalation queue and
audit trail.

The gateway is a single replica, so memory stays the truth while it runs and
every mutation is ALSO written to Redis; a restarted gateway reloads from
Redis. Writes are best effort: a Redis outage is logged and never fails a
triage, because the record is still in memory and the patient still needs
their answer. With CAREROUTE_REDIS_URL unset, nothing here runs.
"""
from __future__ import annotations

import logging

from . import config

logger = logging.getLogger("careroute.persistence")

KEY_CASES = "careroute:cases"
KEY_ESCALATIONS = "careroute:escalations"
KEY_SESSIONS = "careroute:sessions"
KEY_DIGESTS = "careroute:digests"
AUDIT_PREFIX = "careroute:audit:"


def connect(url: str | None = None):
    url = config.REDIS_URL if url is None else url
    if not url:
        return None
    import redis

    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)


def write(client, command: str, *args) -> None:
    if client is None:
        return
    try:
        getattr(client, command)(*args)
    except Exception:  # noqa: BLE001 - a Redis outage must not fail a triage; memory still holds the record
        logger.warning("redis %s failed; record kept in memory only", command, exc_info=False)
```

- [x] **Step 5: `store.py` write-through**

Add `from . import persistence` to the local imports. Replace `def __init__(self) -> None:` with `def __init__(self, redis_client=None) -> None:`. Add `self._redis = redis_client` as its first line, and after `self._seed_escalations()` add:

```python
        # [Microservices] Reload what a previous gateway process decided.
        if self._redis is not None:
            self._load()
```

Replace `save_case` with:

```python
    def save_case(self, case: CaseRecord) -> None:
        # The case itself is always persisted (keyed by its trusted, server-
        # generated caseId). It is only added to EPISODIC memory (the per-session
        # index) when the client-supplied sessionId is well-formed -- a spoofed /
        # malformed sessionId therefore can't write into episodic memory at all.
        self.cases[case.caseId] = case
        persistence.write(self._redis, "hset", persistence.KEY_CASES, case.caseId, case.model_dump_json())
        if valid_session_id(case.sessionId):
            ids = self.session_index.setdefault(case.sessionId, [])
            ids.append(case.caseId)
            # [AI-Security][ASI06] Fingerprint the record as it enters episodic
            # memory, so `recall_session` can prove it is replaying what was
            # actually decided rather than what someone later wrote.
            self._memory_digests[case.caseId] = memory_digest(case)
            persistence.write(self._redis, "hset", persistence.KEY_DIGESTS, case.caseId,
                              self._memory_digests[case.caseId])
            # [AI-Security] Bound per-session retention so a spoofed id can't be
            # used to flood/poison the store; drop the oldest ids past the cap.
            if len(ids) > _MAX_SESSION_CASES:
                dropped = ids[:-_MAX_SESSION_CASES]
                for old in dropped:
                    self._memory_digests.pop(old, None)
                del ids[:-_MAX_SESSION_CASES]
                persistence.write(self._redis, "hdel", persistence.KEY_DIGESTS, *dropped)
            persistence.write(self._redis, "hset", persistence.KEY_SESSIONS, case.sessionId, json.dumps(ids))
```

In `create_escalation_from_case`, after `self.escalations[escalation.id] = escalation`:

```python
        persistence.write(self._redis, "hset", persistence.KEY_ESCALATIONS, escalation.id,
                          escalation.model_dump_json())
```

In `decide_escalation`, after `self.escalations[escalation_id] = escalation`:

```python
        persistence.write(self._redis, "hset", persistence.KEY_ESCALATIONS, escalation_id,
                          escalation.model_dump_json())
```

Add after `_seed_escalations` (at the end of the class):

```python
    def _load(self) -> None:
        """Replace the in-memory state with what Redis holds. On a first start
        Redis has no escalations, so the demo seeds are written INTO it once;
        after that the seeds come back from Redis instead of being re-minted
        with new ids on every restart."""
        r = self._redis
        try:
            escalations = r.hgetall(persistence.KEY_ESCALATIONS)
            cases = r.hgetall(persistence.KEY_CASES)
            sessions = r.hgetall(persistence.KEY_SESSIONS)
            digests = r.hgetall(persistence.KEY_DIGESTS)
        except Exception:  # noqa: BLE001 - start serving from memory; writes will retry per call
            logger.warning("store: redis unavailable at start-up; serving from memory", exc_info=False)
            return
        if escalations:
            self.escalations = {k: EscalationDetail.model_validate_json(v) for k, v in escalations.items()}
        else:
            for escalation in self.escalations.values():
                persistence.write(r, "hset", persistence.KEY_ESCALATIONS, escalation.id,
                                  escalation.model_dump_json())
        self.cases = {k: CaseRecord.model_validate_json(v) for k, v in cases.items()}
        self.session_index = {k: json.loads(v) for k, v in sessions.items()}
        self._memory_digests = dict(digests)
```

Replace the singleton `store = Store()` with `store = Store(redis_client=persistence.connect())`.

- [x] **Step 6: `audit.py` write-through**

Add imports: `import json`, `import logging`, change `from dataclasses import dataclass` to `from dataclasses import asdict, dataclass`, and add `from . import persistence`. Below the imports add `logger = logging.getLogger("careroute.audit")`.

Replace `AuditLog.__init__` with:

```python
    def __init__(self, redis_client=None) -> None:
        self._entries: dict[str, list[AuditEntry]] = {}
        self._counters: dict[str, count[int]] = {}
        self._lock = Lock()
        # [Microservices] Append-only copy in Redis; reloaded by a new process.
        self._redis = redis_client
        if redis_client is not None:
            self._load()
```

In `record`, directly before `return entry`:

```python
            persistence.write(self._redis, "rpush", persistence.AUDIT_PREFIX + case_id, json.dumps(asdict(entry)))
```

Add a method:

```python
    def _load(self) -> None:
        prefix = persistence.AUDIT_PREFIX
        try:
            for key in self._redis.scan_iter(match=prefix + "*"):
                entries = [AuditEntry(**json.loads(row)) for row in self._redis.lrange(key, 0, -1)]
                case_id = key[len(prefix):]
                self._entries[case_id] = entries
                self._counters[case_id] = count(len(entries) + 1)
        except Exception:  # noqa: BLE001 - an unreadable audit store must not stop the API starting
            logger.warning("audit: redis unavailable at start-up; trail starts empty", exc_info=False)
```

Replace the singleton `audit_log = AuditLog()` with `audit_log = AuditLog(redis_client=persistence.connect())`.

- [x] **Step 7: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_ms_persistence.py tests/test_api_e2e.py -q`
Expected: all pass.

- [x] **Step 8: Lint and commit**

```bash
.venv/Scripts/python -m ruff check app tests/test_ms_persistence.py
git add app/persistence.py app/store.py app/audit.py requirements.txt requirements-dev.txt tests/test_ms_persistence.py
git commit -m "feat(microservices): Redis write-through so a restart loses no escalation or audit entry"
```

---

### Task 7: Containers — multi-target Dockerfile, Compose, monitoring, smoke test

**Files:**
- Rewrite: `backend/Dockerfile`
- Rewrite: `docker-compose.yml`
- Modify: `monitoring/prometheus.yml`, `monitoring/alert.rules.yml`
- Create: `scripts/compose_smoke.py`

**Interfaces:**
- Consumes: `python -m app.microservices.serve <service>` (Tasks 2 and 5), `AGENT_TRANSPORT`, `CAREROUTE_*` env (Task 1), `careroute_agent_calls_total` (Task 3)
- Produces: Docker targets `intake-gateway classifier-agent safety-agent routing-agent reflection-agent hitl-agent handoff-agent llm-gateway rag-service ml-jobs monolith` (default = `monolith`). Images are named `careroute-<target>:${IMAGE_TAG:-local}`.
- Produces: `scripts/compose_smoke.py --base-url URL` (exit 0 = a real triage went through every agent container)

- [x] **Step 1: Rewrite `backend/Dockerfile`**

```dockerfile
# CareRoute backend — ONE Dockerfile, ONE target per microservice.
#
#   docker build --target classifier-agent -t careroute-classifier-agent backend
#
# Every service image is `runtime-base` (slim, non-root, shared code) plus only
# what that service needs: the severity model, the clinic-hours snapshot or the
# embedder. The last stage, `monolith`, is the default target, so a plain
# `docker build backend` still produces the single-process backend (all agents
# in-process) for local use. See docs/design/specs/2026-09-19-agent-microservices-design.md.

# ---- builder: deps + a VALIDATED model -------------------------------------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
# [MLOps] Train + enforce the release gate at build time. A gate breach
# (accuracy / red-flag recall / fairness) exits non-zero and FAILS the build,
# so a regressed model can never be packaged.
ENV CAREROUTE_MODEL_DIR=/app/models
RUN python -m app.ml.train

# ---- rag-builder: + the embedding extra, for rag-service only ---------------
FROM builder AS rag-builder
COPY requirements-agentic.txt .
RUN pip install -r requirements-agentic.txt

# ---- runtime-base: slim, non-root, shared code -------------------------------
FROM python:3.12-slim AS runtime-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    CAREROUTE_MODEL_DIR=/app/models \
    LLM_PROVIDER_ORDER=openai \
    AGENT_TRANSPORT=http \
    HEALTH_PATH=/ready
WORKDIR /app
RUN groupadd -r careroute && useradd -r -g careroute careroute
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/app ./app
# Shared volume for the inference / ground-truth logs the ml-jobs read.
RUN mkdir -p /app/telemetry && chown -R careroute:careroute /app
USER careroute
# One liveness probe for every service; each stage sets SERVICE_PORT (and the
# gateway its own HEALTH_PATH). Slim image has no curl, hence python.
HEALTHCHECK --interval=30s --timeout=3s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; u='http://127.0.0.1:'+os.environ['SERVICE_PORT']+os.environ['HEALTH_PATH']; sys.exit(0 if urllib.request.urlopen(u, timeout=2).status==200 else 1)"]

# ---- the gateway: intake agent + orchestrator + public API ------------------
FROM runtime-base AS intake-gateway
ENV SERVICE_PORT=8000 HEALTH_PATH=/api/health
# Models only for GET /api/fairness; triage never loads them here.
COPY --from=builder --chown=careroute:careroute /app/models ./models
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime-base AS classifier-agent
ENV SERVICE_PORT=8101
COPY --from=builder --chown=careroute:careroute /app/models ./models
EXPOSE 8101
CMD ["python", "-m", "app.microservices.serve", "classifier"]

FROM runtime-base AS safety-agent
ENV SERVICE_PORT=8102
EXPOSE 8102
CMD ["python", "-m", "app.microservices.serve", "safety"]

FROM runtime-base AS routing-agent
ENV SERVICE_PORT=8103
# RUNTIME data read by app/tools/gpgowhere.py (see the old single-stage file's
# note: without it the closed-clinic filter silently turns off).
COPY --chown=careroute:careroute data/gpgowhere_hours.json ./data/gpgowhere_hours.json
EXPOSE 8103
CMD ["python", "-m", "app.microservices.serve", "routing"]

FROM runtime-base AS reflection-agent
ENV SERVICE_PORT=8104
EXPOSE 8104
CMD ["python", "-m", "app.microservices.serve", "reflection"]

FROM runtime-base AS hitl-agent
ENV SERVICE_PORT=8105
EXPOSE 8105
CMD ["python", "-m", "app.microservices.serve", "hitl"]

FROM runtime-base AS handoff-agent
ENV SERVICE_PORT=8106
EXPOSE 8106
CMD ["python", "-m", "app.microservices.serve", "handoff"]

FROM runtime-base AS llm-gateway
ENV SERVICE_PORT=8107
EXPOSE 8107
CMD ["python", "-m", "app.microservices.serve", "llm"]

FROM runtime-base AS rag-service
ENV SERVICE_PORT=8108
COPY --from=rag-builder /opt/venv /opt/venv
EXPOSE 8108
# The embedder downloads on first warm-up; give it time before probing.
HEALTHCHECK --interval=30s --timeout=3s --start-period=180s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; u='http://127.0.0.1:'+os.environ['SERVICE_PORT']+os.environ['HEALTH_PATH']; sys.exit(0 if urllib.request.urlopen(u, timeout=2).status==200 else 1)"]
CMD ["python", "-m", "app.microservices.serve", "rag"]

# ---- batch-scorer (default) and drift-monitor: run to completion ------------
FROM runtime-base AS ml-jobs
COPY --from=builder --chown=careroute:careroute /app/models ./models
HEALTHCHECK NONE
CMD ["python", "-m", "app.ml.batch_score", "--from-store"]

# ---- monolith: the DEFAULT target — every agent in one process --------------
FROM runtime-base AS monolith
ENV AGENT_TRANSPORT=inprocess SERVICE_PORT=8000 HEALTH_PATH=/api/health
COPY --from=builder --chown=careroute:careroute /app/models ./models
COPY --chown=careroute:careroute data/gpgowhere_hours.json ./data/gpgowhere_hours.json
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [x] **Step 2: Rewrite `docker-compose.yml`**

```yaml
# CareRoute as microservices: one agent, one container, all on one machine.
# Run:  docker compose up --build        then open http://localhost:8080
#
# The intake-gateway (network alias `backend`, so the frontend and Prometheus
# configs are unchanged) is the only backend port published; every agent is
# reachable only on this Compose network. OPENAI_API_KEY reaches ONE container,
# llm-gateway. Without a key every agent runs its deterministic fallback.
# Scheduled jobs:  docker compose --profile jobs run --rm batch-scorer
#                  docker compose --profile jobs run --rm drift-monitor
# Spec: docs/design/specs/2026-09-19-agent-microservices-design.md

x-agent: &agent
  restart: unless-stopped
  depends_on:
    llm-gateway: {condition: service_healthy}
    rag-service: {condition: service_healthy}
  deploy:
    resources:
      limits: {cpus: "0.5", memory: 384M}

x-agent-env: &agent-env
  CAREROUTE_LLM_GATEWAY_URL: http://llm-gateway:8107
  CAREROUTE_RAG_SERVICE_URL: http://rag-service:8108
  CAREROUTE_INTERNAL_TOKEN: ${CAREROUTE_INTERNAL_TOKEN:-local-dev-token}

services:
  redis:
    image: redis:7.4.1-alpine
    command: ["redis-server", "--appendonly", "yes"]
    volumes: [redis-data:/data]
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
    deploy:
      resources:
        limits: {cpus: "0.25", memory: 256M}

  llm-gateway:
    build: {context: ./backend, target: llm-gateway}
    image: careroute-llm-gateway:${IMAGE_TAG:-local}
    environment:
      LLM_PROVIDER_ORDER: ${LLM_PROVIDER_ORDER:-openai}
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}
      OPENAI_MODEL: ${OPENAI_MODEL:-gpt-4o-mini}
      CAREROUTE_INTERNAL_TOKEN: ${CAREROUTE_INTERNAL_TOKEN:-local-dev-token}
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "0.5", memory: 256M}

  rag-service:
    build: {context: ./backend, target: rag-service}
    image: careroute-rag-service:${IMAGE_TAG:-local}
    environment:
      CAREROUTE_INTERNAL_TOKEN: ${CAREROUTE_INTERNAL_TOKEN:-local-dev-token}
      ONYX_BASE_URL: ${ONYX_BASE_URL:-}
      ONYX_API_KEY: ${ONYX_API_KEY:-}
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "1.0", memory: 1024M}

  classifier-agent:
    <<: *agent
    build: {context: ./backend, target: classifier-agent}
    image: careroute-classifier-agent:${IMAGE_TAG:-local}
    environment:
      <<: *agent-env
      CAREROUTE_INFERENCE_LOG: /app/telemetry/inference_log.jsonl
    volumes: [telemetry:/app/telemetry]
    deploy:
      resources:
        limits: {cpus: "1.0", memory: 768M}

  safety-agent:
    <<: *agent
    build: {context: ./backend, target: safety-agent}
    image: careroute-safety-agent:${IMAGE_TAG:-local}
    environment: *agent-env

  routing-agent:
    <<: *agent
    build: {context: ./backend, target: routing-agent}
    image: careroute-routing-agent:${IMAGE_TAG:-local}
    environment:
      <<: *agent-env
      ONEMAP_EMAIL: ${ONEMAP_EMAIL:-}
      ONEMAP_PASSWORD: ${ONEMAP_PASSWORD:-}

  reflection-agent:
    <<: *agent
    build: {context: ./backend, target: reflection-agent}
    image: careroute-reflection-agent:${IMAGE_TAG:-local}
    environment: *agent-env

  hitl-agent:
    <<: *agent
    build: {context: ./backend, target: hitl-agent}
    image: careroute-hitl-agent:${IMAGE_TAG:-local}
    environment: *agent-env

  handoff-agent:
    <<: *agent
    build: {context: ./backend, target: handoff-agent}
    image: careroute-handoff-agent:${IMAGE_TAG:-local}
    environment: *agent-env

  intake-gateway:
    build: {context: ./backend, target: intake-gateway}
    image: careroute-intake-gateway:${IMAGE_TAG:-local}
    networks:
      default:
        aliases: [backend]
    ports: ["8000:8000"]
    environment:
      <<: *agent-env
      AGENT_TRANSPORT: http
      CAREROUTE_AGENT_URL_CLASSIFIER: http://classifier-agent:8101
      CAREROUTE_AGENT_URL_SAFETY: http://safety-agent:8102
      CAREROUTE_AGENT_URL_ROUTING: http://routing-agent:8103
      CAREROUTE_AGENT_URL_REFLECTION: http://reflection-agent:8104
      CAREROUTE_AGENT_URL_HITL: http://hitl-agent:8105
      CAREROUTE_AGENT_URL_HANDOFF: http://handoff-agent:8106
      CAREROUTE_REDIS_URL: redis://redis:6379/0
      CAREROUTE_GROUND_TRUTH_LOG: /app/telemetry/ground_truth.jsonl
      # [AI-Security] Shared key for the staff endpoints; set the same on `frontend`.
      CAREROUTE_STAFF_API_KEY: ${CAREROUTE_STAFF_API_KEY:-}
    volumes: [telemetry:/app/telemetry]
    depends_on:
      redis: {condition: service_healthy}
      classifier-agent: {condition: service_healthy}
      safety-agent: {condition: service_healthy}
      routing-agent: {condition: service_healthy}
      reflection-agent: {condition: service_healthy}
      hitl-agent: {condition: service_healthy}
      handoff-agent: {condition: service_healthy}
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "1.0", memory: 768M}

  frontend:
    # BACKEND_URL is a BUILD ARG (Next bakes rewrites() at `next build`) AND a
    # runtime env var (the staff proxy Route Handler reads it per request).
    build:
      context: ./frontend
      args:
        BACKEND_URL: http://backend:8000
    image: careroute-frontend:${IMAGE_TAG:-local}
    ports: ["8080:3000"]
    environment:
      BACKEND_URL: http://backend:8000
      CAREROUTE_STAFF_API_KEY: ${CAREROUTE_STAFF_API_KEY:-}
    depends_on:
      intake-gateway: {condition: service_healthy}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "node", "-e", "require('http').get('http://127.0.0.1:3000/',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"]
      interval: 15s
      timeout: 3s
      retries: 5
      start_period: 15s
    deploy:
      resources:
        limits: {cpus: "1.0", memory: 512M}

  batch-scorer:
    profiles: [jobs]
    build: {context: ./backend, target: ml-jobs}
    image: careroute-ml-jobs:${IMAGE_TAG:-local}
    environment:
      CAREROUTE_REDIS_URL: redis://redis:6379/0
      CAREROUTE_INFERENCE_LOG: /app/telemetry/inference_log.jsonl
      CAREROUTE_GROUND_TRUTH_LOG: /app/telemetry/ground_truth.jsonl
    volumes: [telemetry:/app/telemetry]
    depends_on:
      redis: {condition: service_healthy}

  drift-monitor:
    profiles: [jobs]
    image: careroute-ml-jobs:${IMAGE_TAG:-local}
    command: ["python", "-m", "app.ml.monitor"]
    environment:
      CAREROUTE_MONITOR_SOURCE: live
      CAREROUTE_MONITOR_DIR: /app/telemetry/monitoring
      CAREROUTE_INFERENCE_LOG: /app/telemetry/inference_log.jsonl
      CAREROUTE_GROUND_TRUTH_LOG: /app/telemetry/ground_truth.jsonl
    volumes: [telemetry:/app/telemetry]

  prometheus:
    image: prom/prometheus:v2.55.1
    ports: ["9090:9090"]
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./monitoring/alert.rules.yml:/etc/prometheus/alert.rules.yml:ro
      - prometheus-data:/prometheus
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.path=/prometheus"
    depends_on:
      intake-gateway: {condition: service_healthy}
      alertmanager: {condition: service_started}
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "0.5", memory: 512M}

  alertmanager:
    image: prom/alertmanager:v0.27.0
    ports: ["9093:9093"]
    volumes:
      - ./monitoring/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro
      - alertmanager-data:/alertmanager
    command:
      - "--config.file=/etc/alertmanager/alertmanager.yml"
      - "--storage.path=/alertmanager"
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "0.25", memory: 256M}

  grafana:
    image: grafana/grafana:11.3.1
    ports: ["3000:3000"]
    environment:
      GF_SECURITY_ADMIN_USER: admin
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:-careroute-admin}
      GF_USERS_ALLOW_SIGN_UP: "false"
    volumes:
      - ./monitoring/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./monitoring/grafana/dashboards:/var/lib/grafana/dashboards:ro
      - grafana-data:/var/lib/grafana
    depends_on:
      prometheus: {condition: service_started}
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "0.5", memory: 512M}

volumes:
  redis-data:
  telemetry:
  prometheus-data:
  grafana-data:
  alertmanager-data:
```

- [x] **Step 3: Prometheus scrape targets and alerts**

In `monitoring/prometheus.yml`, add after the `careroute-backend` job:

```yaml
  # [Microservices] One target per agent / shared-service container. The
  # gateway job above already counts every call it makes to them
  # (careroute_agent_calls_total); these are each container's own view.
  - job_name: careroute-agents
    metrics_path: /metrics
    static_configs:
      - targets: ["classifier-agent:8101"]
        labels: {service: careroute-classifier-agent}
      - targets: ["safety-agent:8102"]
        labels: {service: careroute-safety-agent}
      - targets: ["routing-agent:8103"]
        labels: {service: careroute-routing-agent}
      - targets: ["reflection-agent:8104"]
        labels: {service: careroute-reflection-agent}
      - targets: ["hitl-agent:8105"]
        labels: {service: careroute-hitl-agent}
      - targets: ["handoff-agent:8106"]
        labels: {service: careroute-handoff-agent}
      - targets: ["llm-gateway:8107"]
        labels: {service: careroute-llm-gateway}
      - targets: ["rag-service:8108"]
        labels: {service: careroute-rag-service}
```

In `monitoring/alert.rules.yml`, append to the `careroute-triage` group's `rules:`:

```yaml
      # [Microservices] An agent container is failing the gateway: in the last
      # 5 min calls to it errored, were rejected or hit an open circuit, and
      # none succeeded. Cases still COMPLETE (degraded, escalated to a human),
      # which is exactly why this needs an alert — nothing else looks broken.
      - alert: AgentDown
        expr: >
          sum(increase(careroute_agent_calls_total{outcome!="ok"}[5m])) by (agent, service) > 0
          unless
          sum(increase(careroute_agent_calls_total{outcome="ok"}[5m])) by (agent, service) > 0
        for: 1m
        labels:
          severity: critical
          aspect: mlops
        annotations:
          summary: "Agent {{ $labels.agent }} is unavailable to the gateway"
          description: "Every call to the {{ $labels.agent }} container failed in the last 5m; its cases are being escalated to clinicians by default."

      - alert: AgentMetricsDown
        expr: up{job="careroute-agents"} == 0
        for: 2m
        labels:
          severity: critical
          aspect: mlops
        annotations:
          summary: "{{ $labels.service }} is not being scraped"
          description: "Prometheus cannot reach {{ $labels.instance }} — the container is down or unreachable."
```

- [x] **Step 4: `scripts/compose_smoke.py`**

```python
"""Compose smoke test: one real triage through every agent container.

    python scripts/compose_smoke.py --base-url http://localhost:8000

Exits 0 only if the gateway is healthy, a red-flag triage completes and is
escalated, and the gateway's own metrics show at least one SUCCESSFUL call to
every agent container — so a degraded run (an agent down, the case escalated
by default) cannot pass as a healthy one. Stdlib only: runs in any CI image.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request

AGENTS = ("classifier", "safety", "routing", "hitl", "reflection", "handoff")
TEXT = "Crushing chest pain spreading to my left arm and sweating, started 20 minutes ago"


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - fixed http URL from the CLI
        return resp.status, resp.read().decode()


def wait_healthy(base: str, seconds: int) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if _get(f"{base}/api/health")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(3)
    sys.exit(f"gateway not healthy at {base} after {seconds}s")


def triage(base: str) -> dict:
    body = json.dumps({"text": TEXT, "language": "en", "isVoice": False}).encode()
    req = urllib.request.Request(f"{base}/api/triage/stream", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    final = None
    with urllib.request.urlopen(req, timeout=240) as resp:  # noqa: S310 - fixed http URL from the CLI
        for raw in resp:
            line = raw.decode().strip()
            if line.startswith("data:"):
                event = json.loads(line[len("data:"):].strip())
                if event.get("event") == "final":
                    final = event
    if final is None:
        sys.exit("triage stream ended without a final event")
    return final


def ok_calls(base: str) -> dict[str, float]:
    _, text = _get(f"{base}/metrics")
    pattern = r'careroute_agent_calls_total\{agent="(\w+)",outcome="ok"\} ([0-9.e+]+)'
    return {m.group(1): float(m.group(2)) for m in re.finditer(pattern, text)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--wait", type=int, default=300)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    wait_healthy(base, args.wait)
    final = triage(base)
    if not final.get("escalated"):
        sys.exit(f"red-flag case was not escalated: {final.get('acuity')}")
    calls = ok_calls(base)
    missing = [a for a in AGENTS if calls.get(a, 0) < 1]
    if missing:
        sys.exit(f"no successful call reached: {missing} (calls seen: {calls})")
    print(f"OK: escalated={final['escalated']} acuity={final['acuity']['code']} calls={calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 5: Validate, build, and run it**

Run (repo root):
```bash
docker compose config -q && echo compose-ok
docker compose build
docker compose up -d --wait --wait-timeout 420 intake-gateway
python scripts/compose_smoke.py --base-url http://localhost:8000
docker compose ps
```
Expected: `compose-ok`. Every image builds, and the model gate passes in `builder`. `up --wait` returns once all ten backend containers are healthy. The smoke script prints `OK: escalated=True ...` with all six agents in `calls`. `docker compose ps` lists 10 running backend containers, and only `8000` is published among them. If `rag-service` is unhealthy because the embedder couldn't download, `docker compose logs rag-service` shows it. Retrieval then serves lexical results, the smoke still passes, and you should report it.

If Docker isn't available on this machine, say so and skip to Step 6. Task 8's `test:compose-smoke` CI job runs the same check.

```bash
docker compose down
```

- [x] **Step 6: Commit**

```bash
git add backend/Dockerfile docker-compose.yml monitoring/prometheus.yml monitoring/alert.rules.yml scripts/compose_smoke.py
git commit -m "build(microservices): one image per agent, Compose on one host, AgentDown alert, smoke test"
```

---

### Task 8: CI — build, scan and publish every image; compose smoke

**Files:**
- Modify: `.gitlab-ci.yml` (global `variables:` ~line 61; `build:images` ~line 1543; `scan:container-image` ~line 1646; `scan:container-dockle` ~line 1668; `deploy:push-images` script ~line 1755-1772)

**Interfaces:**
- Consumes: Docker targets (Task 7), `scripts/compose_smoke.py`
- Produces: artifact `images.tar` (every image, shared layers stored once), CI variable `CAREROUTE_IMAGES`, ECR repos `${ECR_NAMESPACE}/<service>` (the infra plan creates them)

- [x] **Step 1: Global variable**

Under the top-level `variables:` add:

```yaml
  # [Microservices] Every backend image, one per Dockerfile target. The frontend
  # is added explicitly where it applies. Keep in sync with backend/Dockerfile.
  CAREROUTE_IMAGES: "intake-gateway classifier-agent safety-agent routing-agent reflection-agent hitl-agent handoff-agent llm-gateway rag-service ml-jobs"
```

- [x] **Step 2: `build:images`**

Replace its `script:` and `artifacts:` with:

```yaml
  script:
    - |
      for svc in $CAREROUTE_IMAGES; do
        docker build --target "$svc" -t "careroute-${svc}:${CI_COMMIT_SHORT_SHA}" backend
      done
    - docker build -t careroute-frontend:$CI_COMMIT_SHORT_SHA frontend
    # ONE tarball for every image. The ten backend images share their base
    # layers and `docker save` writes a shared layer once; ten separate tars
    # would store the Python venv ten times and exceed the artifact limit.
    - docker save $(for svc in $CAREROUTE_IMAGES frontend; do printf 'careroute-%s:%s ' "$svc" "$CI_COMMIT_SHORT_SHA"; done) -o images.tar
  artifacts:
    paths: [images.tar]
    expire_in: 1 day
```

- [x] **Step 3: `scan:container-image` and `scan:container-dockle`**

Replace the whole `scan:container-image` job with:

```yaml
scan:container-image:
  stage: build
  image: docker:27
  services: [docker:27-dind]
  needs: [build:images]
  variables:
    DOCKER_HOST: tcp://docker:2376
    DOCKER_TLS_CERTDIR: "/certs"
    DOCKER_TLS_VERIFY: "1"
    DOCKER_CERT_PATH: "/certs/client"
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
    - when: manual
      allow_failure: true
  script:
    # Pinned scanner binary: the images live in one multi-image tarball, so each
    # is re-exported singly (to local disk, not an artifact) for Trivy's --input.
    - wget -qO- https://github.com/aquasecurity/trivy/releases/download/v0.56.2/trivy_0.56.2_Linux-64bit.tar.gz | tar -xz -C /usr/local/bin trivy
    - docker load -i images.tar
    - |
      for svc in $CAREROUTE_IMAGES frontend; do
        docker save "careroute-${svc}:${CI_COMMIT_SHORT_SHA}" -o "/tmp/${svc}.tar"
        trivy image --input "/tmp/${svc}.tar" --scanners vuln,secret,misconfig --severity HIGH,CRITICAL --format table
        trivy image --input "/tmp/${svc}.tar" --format json -o "trivy-${svc}-image.json" || true
        rm -f "/tmp/${svc}.tar"
      done
  artifacts:
    paths: ["trivy-*-image.json"]
    when: always
  allow_failure: true
```

Replace the whole `scan:container-dockle` job with:

```yaml
scan:container-dockle:
  stage: build
  image: docker:27
  services: [docker:27-dind]
  needs: [build:images]
  variables:
    DOCKER_HOST: tcp://docker:2376
    DOCKER_TLS_CERTDIR: "/certs"
    DOCKER_TLS_VERIFY: "1"
    DOCKER_CERT_PATH: "/certs/client"
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
    - when: manual
      allow_failure: true
  script:
    - wget -qO- https://github.com/goodwithtech/dockle/releases/download/v0.4.14/dockle_0.4.14_Linux-64bit.tar.gz | tar -xz -C /usr/local/bin dockle
    - docker load -i images.tar
    - |
      for svc in $CAREROUTE_IMAGES frontend; do
        docker save "careroute-${svc}:${CI_COMMIT_SHORT_SHA}" -o "/tmp/${svc}.tar"
        dockle --input "/tmp/${svc}.tar" --exit-code 0 --format list
        rm -f "/tmp/${svc}.tar"
      done
  allow_failure: true
```

- [x] **Step 4: `deploy:push-images`**

Replace
```yaml
    - docker load -i backend-image.tar
    - docker load -i frontend-image.tar
```
with
```yaml
    - docker load -i images.tar
```
and in the push loop replace `for svc in backend frontend; do` with `for svc in $CAREROUTE_IMAGES frontend; do`.

- [x] **Step 5: Compose smoke job**

Add after `scan:container-dockle`:

```yaml
# [Microservices] The SCANNED images, wired together exactly as docker-compose.yml
# wires them, answering one real red-flag triage. Fails if any agent container
# never took a successful call (a degraded run must not pass as healthy).
test:compose-smoke:
  stage: build
  image: docker:27
  services: [docker:27-dind]
  needs: [build:images]
  variables:
    DOCKER_HOST: tcp://docker:2376
    DOCKER_TLS_CERTDIR: "/certs"
    DOCKER_TLS_VERIFY: "1"
    DOCKER_CERT_PATH: "/certs/client"
    IMAGE_TAG: $CI_COMMIT_SHORT_SHA
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
    - when: manual
      allow_failure: true
  script:
    - apk add --no-cache python3 >/dev/null
    - docker load -i images.tar
    - docker compose up -d --no-build --wait --wait-timeout 420 intake-gateway
    - python3 scripts/compose_smoke.py --base-url http://docker:8000
  after_script:
    - docker compose logs --no-color > compose-logs.txt 2>&1 || true
  artifacts:
    paths: [compose-logs.txt]
    when: always
```

- [x] **Step 6: Validate the YAML**

Run (repo root): `backend/.venv/Scripts/python -c "import yaml,sys; yaml.safe_load(open('.gitlab-ci.yml', encoding='utf-8')); print('ci-yaml-ok')"`
Expected: `ci-yaml-ok`. Also run `grep -n "backend-image.tar\|frontend-image.tar" .gitlab-ci.yml`. Expected: no output.

- [x] **Step 7: Commit**

```bash
git add .gitlab-ci.yml
git commit -m "ci(microservices): build, scan and push one image per service; compose smoke on the scanned images"
```

---

### Task 9: Documentation

**Files:**
- Modify: `docs/ARCHITECTURE.md` (new section before `## 6. Decision record`)
- Modify: `docs/design/specs/2026-09-19-agent-microservices-design.md` (§4, §6)

- [x] **Step 1: ARCHITECTURE.md**

Insert before `## 6. Decision record — no agent framework`:

```markdown
## 5a. Microservice deployment — one agent, one container

The application is split so that each agent is its own service; the deployment
puts them all on one compute to keep cost down. The two are independent: the
same images run as one ECS task today and as one ECS service each tomorrow.

| Container | Port | Role |
|---|---|---|
| intake-gateway | 8000 | public API, SSE, guardrails, **intake agent + orchestrator**, message bus, audit |
| classifier-agent | 8101 | Severity Classifier (the only agent image with the trained model) |
| safety-agent | 8102 | Safety Override (red-flag rules + semantic layer) |
| routing-agent | 8103 | Care Routing (clinic data, GoWhere hours, OneMap) |
| reflection-agent | 8104 | Reflection / Critic |
| hitl-agent | 8105 | Human-in-the-Loop decision |
| handoff-agent | 8106 | Clinician Handoff |
| llm-gateway | 8107 | the only container with an LLM provider key |
| rag-service | 8108 | guideline index + embedder, shared by every agent |
| redis | 6379 | escalation queue, case store, audit trail (durable) |

**How a triage flows.** The gateway runs the same fixed, safety-gated order as
before and calls each agent with `POST /v1/invoke` (the case state plus the
agent's inbox). The agent runs its real worker class and replies with the
fields it changed and the message it announces. The gateway accepts a reply
only inside that agent's declared lane (`CONTRACT.writes`) and message intents
(`COMMS.publishes`) — the same least-privilege rules the in-process tests
enforce, now enforced at the network boundary.

**When an agent is down**, the case still completes and is never less cautious:
a missing classifier, safety, routing or HITL answer escalates the case to a
clinician; a missing Safety answer also fails the route gate closed. The
`AgentDown` alert fires, because nothing else would look broken.

**Why split this way.** Split along secrets (llm-gateway), state (redis),
resource profile (rag-service, classifier) and sharing (rag, llm); keep pure,
deterministic safety code (guardrail, redaction, red-flag rules) in-process so
it can never fail over the network.

Run it: `docker compose up --build`. Spec:
`docs/design/specs/2026-09-19-agent-microservices-design.md`.
```

- [x] **Step 2: Spec corrections**

In the spec §4, after the `op` paragraph, add:

```markdown
`emit` is an op on every agent (an announcement is built from the current
state). The one piece of worker memory an announcement needs, Safety's last
result, makes a round trip through the gateway as `carry`, so the service
stays stateless. `consumed` tells the service whether the orchestrator
delivered an inbox, preserving `ConsumesMessages` semantics.
```

In §6, replace `No job changes shape — the image list grows from 2 to 13.` with `No job changes shape — the image list grows from 2 to 11 (ten backend targets plus the frontend), saved as one multi-image tarball so shared layers are stored once.`, and add `The intake-gateway image also carries the model artifact, for GET /api/fairness only; triage never loads it there.`

- [x] **Step 3: Commit**

```bash
git add docs/ARCHITECTURE.md docs/design/specs/2026-09-19-agent-microservices-design.md
git commit -m "docs(architecture): the microservice view — one agent, one container, one compute"
```

---

## After this plan

1. **Infra plan (`careroute_ai_infra`)**, written next, in that repo: ECR repos for the 11 images; the backend `ecs_service` task definition takes 10 containers with `dependsOn` HEALTHY, per-container secrets (OpenAI only on `llm-gateway`, OneMap only on `routing-agent`), and `essential` only on intake-gateway, safety-agent and redis; 2 vCPU / 8 GB; `scheduled_task` runs `ml-jobs` for batch-scorer and drift-monitor; Prometheus targets `:8000,:8101-8108` through Cloud Map; nightly Redis snapshot to the artifacts bucket.
2. Full verification before claiming completion: the whole backend suite, `ruff check app`, and the compose smoke.
