# Agent microservices on a single compute — design

Date: 2026-09-19 · Status: draft for review · Branch: `feature/agent-microservices`

## 1. Why

The lecturer's requirement: the **application** must be a microservice design,
one agent per container, while the whole thing is deployed on **one compute**
to keep cost down. The infrastructure itself does not have to be microservices.

Today the backend is one FastAPI process. All seven agents run in-process,
sequenced by `agents/orchestration.py` over an in-process `MessageBus`
(`agents/messaging.py`), whose docstring already anticipates swapping the bus
for a real transport without touching any agent's `COMMS`.

## 2. Decisions

| Decision | Choice | Rejected alternative, and why |
|---|---|---|
| Split depth | One real HTTP service per agent | Broker (Redis/NATS) between agents: harder to keep the safety-gated order deterministic. Same image with different CMD: still a monolith. |
| Orchestration | Intake-gateway calls agents over HTTP in the **existing fixed order** | Choreography via events: loses the deterministic safety gate. |
| Deployment | **All containers in one ECS Fargate task** (≤10 containers), existing ALB/cluster/`ecs_service` | Compose on EC2: throws away the ECS design. ECS-on-EC2: ENI limits force bridge mode, more infra work for no app gain. Per-service Fargate tasks: 10+ computes, violates "one compute". |
| LLM | Hosted OpenAI API only in production (`LLM_PROVIDER_ORDER=openai`) | The local CLI and Ollama providers needed local hosting; they have since been removed from the code. |
| Local dev/tests | `AGENT_TRANSPORT=inprocess` keeps today's single-process behaviour | — |

## 3. Services

Ten containers in the backend task. Ports are loopback-only inside the task
(`awsvpc` shares `localhost`); only `intake-gateway` is an ALB target.

| Container | Port | Owns | Secrets | `essential` |
|---|---|---|---|---|
| `intake-gateway` | 8000 | public API, SSE, rate limit, guardrail, redaction, orchestrator, `MessageBus`, audit, staff API | staff API key | yes |
| `classifier-agent` | 8101 | severity model (baked at build, release-gated as today) | — | no |
| `safety-agent` | 8102 | red-flag rules, semantic layer | — | **yes** |
| `routing-agent` | 8103 | clinic dataset, GoWhere hours, OneMap client, route cache | OneMap pair | no |
| `reflection-agent` | 8104 | critic | — | no |
| `hitl-agent` | 8105 | escalation decision logic | — | no |
| `handoff-agent` | 8106 | clinician summary | — | no |
| `llm-gateway` | 8107 | `llm.py` chain, circuit breakers, router, cost accounting, PII egress check | **OpenAI key (only here)** | no |
| `rag-service` | 8108 | corpus, TF-IDF + ONNX embedder, Onyx adapter | Onyx key | no |
| `redis` | 6379 | escalation queue, case store, audit stream (AOF) | — | **yes** |

Kept **in-process as shared library** (split rule: split along secrets, state,
resource profile and sharing; keep pure deterministic safety code local so it
cannot fail over the network): `guardrail.py`, `redact.py`, `models.py`,
`CaseState`, `messaging.py`, `redflags.py`.

Outside the backend task:
- `frontend` — unchanged, its own task.
- `batch-scorer` (nightly) and `drift-monitor` (daily) — one `ml-jobs` image,
  two commands, run by the existing `scheduled_task` module.
- Prometheus / Alertmanager / Grafana — unchanged tasks; scrape config gains
  one target per container.
- Not deployed: `mcp-server` (stays stdio, local), `safety-nlp` (torch, off),
  ADOT sidecar (off; Prometheus covers metrics). These would exceed the
  10-container limit.

Correction to an earlier discussion point: the escalation queue is used by
`main.py` via `store.py`, not by `hitl-agent`. So `store.py` gains a Redis
backend used by the gateway (and read by `batch-scorer`); `hitl-agent` stays a
pure decision service.

## 4. Agent contract

Every agent service exposes the same surface:

```
POST /v1/invoke
  headers  X-Correlation-Id, X-Internal-Token
  body     { "op": "run" | "areason" | "prescreen" | ..., "case_id": str,
             "state": <CaseState as dict>, "inbox": [<AgentMessage>],
             "iteration": int }
  reply    { "result": <op return value>, "state_patch": {field: value},
             "messages": [<AgentMessage>], "timing_ms": float, "llm_used": bool }
GET  /v1/capability   { slug, capability, comms }  -> aggregated by /api/agents
GET  /health          liveness
GET  /ready           model/data loaded
GET  /metrics         Prometheus
```

`op` exists because agents expose more than `run` today
(`safety.prescreen`, `safety.areason`, `reflection.areason`); the allowed ops
are declared per agent.

`emit` is an op on every agent (an announcement is built from the current
state). The one piece of worker memory an announcement needs, Safety's last
result, makes a round trip through the gateway as `carry`, so the service
stays stateless. `consumed` tells the service whether the orchestrator
delivered an inbox, preserving `ConsumesMessages` semantics.

Agent services are **stateless per request**: they rebuild the worker from the
posted state, call the op, call `emit()`, and return the diff. This removes the
per-case worker-state interleaving that `new_session()` exists to prevent.

The gateway enforces least privilege at the boundary:
- a returned message whose intent is not in that agent's `COMMS.publishes` is
  rejected (`enforce_comms`, unchanged);
- a `state_patch` touching a field outside that agent's **field allow-list** is
  rejected. The allow-lists are declared next to each agent's `COMMS` and
  checked by a test, like `ORCHESTRATOR_PUBLISHES` today.

`llm-gateway` and `rag-service` expose `POST /v1/complete` and
`POST /v1/retrieve` mirroring `llm.complete()` and `rag.retrieve()`. `llm.py`
and `rag.py` gain an HTTP client mode behind the same function signatures, so
agent code does not change.

### Transport abstraction

`AgentClient` with two implementations, selected by `AGENT_TRANSPORT`:
- `InProcessAgentClient` — calls the worker object directly (today's path).
- `HttpAgentClient` — calls `/v1/invoke`; per-agent timeout; one retry on
  connection errors only; circuit breaker reusing `llm._Breaker`.

`PipelineOrchestrator` calls `self.<agent>` through the client instead of the
object. The step order, the safety gate, the Reflection loop and every
`publish()` stay in the gateway exactly as today.

## 5. Failure handling

Rule: an unavailable agent must never make the outcome less safe.

| Down | Gateway behaviour |
|---|---|
| classifier | No model acuity; case forced to human review; safety still runs. |
| **safety** | **Fail closed**: `apply_safety_failsafe` — force review, never lower acuity. |
| routing | Return acuity advice with "clinic list unavailable" (995 guidance on emergency); flag review. |
| reflection | Skip critic; audit records `reflection unavailable`. |
| hitl | Gateway writes the escalation straight to Redis; patient still gets advice. |
| handoff | Escalation queued without the summary. |
| llm-gateway | Existing behaviour: every agent falls back to deterministic logic. |
| rag-service | Existing behaviour: citations degrade; no crash. |
| redis | Essential container → ECS restarts the task; snapshot restored from S3. |

New Prometheus metric `careroute_agent_calls_total{agent,outcome}` and alert
`AgentDown` routed through the existing Alertmanager. `/api/health` reports each
agent's readiness and breaker state.

As built, `AgentDown` fires on the **call outcomes**, not on breaker state: every
call to an agent failed in the last 5 min and none succeeded. Breaker-open is one
way to reach that, but a container answering with a contract violation on every
call never opens a breaker and is just as down. A second alert,
`AgentMetricsDown` (`up{job="careroute-agents"} == 0`), covers the container
being unreachable by Prometheus at all.

## 6. Images and build

`backend/Dockerfile` becomes multi-target:

```
base (python, venv, shared lib)
 ├─ intake-gateway   ├─ classifier-agent (+ model, trained+gated in a builder stage)
 ├─ safety-agent     ├─ routing-agent (+ data/gpgowhere_hours.json)
 ├─ reflection-agent ├─ hitl-agent   ├─ handoff-agent
 ├─ llm-gateway      ├─ rag-service (+ corpus, ONNX embedder)
 └─ ml-jobs
```

All run non-root with a `HEALTHCHECK`, as today. Redis and the monitoring
images are pinned to exact versions (no `:latest`).

`docker-compose.yml` runs every container as its own service for local use,
with the same ports and `AGENT_TRANSPORT=http`, plus frontend and monitoring.

CI (`.gitlab-ci.yml`): `build:images` builds every target from a shared cached
base; Trivy and Dockle scan every image; `deploy:push-images` pushes the
scanned tarballs by commit SHA; `deploy:trigger-infra` passes the SHA on. No
job changes shape — the image list grows from 2 to 12: the ten backend targets,
the frontend, and the legacy `backend` monolith, which is still built, scanned
and pushed because the live ECS service deploys it until the infra plan switches
that task definition. All of them are saved as ONE multi-image tarball, so the
layers the backend images share are stored once instead of a dozen times.

The intake-gateway image also carries the model artifact, for `GET /api/fairness`
only; triage never loads it there.

## 7. Infrastructure (`careroute_ai_infra`)

- `ecr`: `repositories` gains the eleven new names.
- `ecs_service` (backend): task definition takes a list of containers with
  `dependsOn` (`HEALTHY`), per-container secrets, and `essential` flags per
  §3. Task size 2 vCPU / 8 GB. Only `intake-gateway:8000` is registered with
  the ALB target group.
- `scheduled_task`: `drift_monitor` switches to the `ml-jobs` image; a second
  instance runs `batch-scorer` nightly.
- Monitoring: Prometheus scrape targets become the backend task's
  `:8000,:8101-8108` via Cloud Map.
- Redis persistence: AOF inside the task plus a nightly snapshot to the
  existing artifacts bucket. EFS is a future flag, not built now.
- Everything else (ALB with 120 s idle timeout, networking, Secrets Manager,
  deploy circuit breaker and SHA rollback) unchanged.

Scale-out path (presentation, not built): split the task definition into one
ECS service per container and switch `localhost` URLs to Service Connect names.
No application change.

## 8. Testing

- Existing suite runs unchanged with `AGENT_TRANSPORT=inprocess`.
- **Transport parity test**: the triage eval cases run through both
  transports (HTTP via FastAPI `TestClient`s) and must produce identical
  acuity, safety flags, routing tier, escalation and bus history.
- Contract tests per agent: capability card matches the class; undeclared
  intent and out-of-allow-list `state_patch` are rejected by the gateway.
- Failure tests: each row of §5, by making that agent's client raise.
- Red-flag recall stays gated at 1.0 (`test_triage_eval.py`), under both
  transports.
- Compose smoke job in CI: `docker compose up`, wait for health, run one
  triage over HTTP, assert every agent's `careroute_agent_call_total` moved.

## 9. Work split

1. App: `AgentClient` + in-process implementation; orchestrator calls through it (no behaviour change).
2. App: agent service wrapper (`/v1/invoke`, capability, health, metrics) and field allow-lists.
3. App: HTTP client, breakers, failure handling, parity + failure tests.
4. App: `llm-gateway` and `rag-service` HTTP modes.
5. App: Redis backend for `store.py` and audit.
6. App: multi-target Dockerfile, compose, CI image matrix, compose smoke job.
7. Infra: ECR repos, multi-container task definition, scheduled tasks, scrape config.
8. Docs: `ARCHITECTURE.md` and diagrams updated to the microservice view.

## 10. Out of scope

Message broker between agents; per-service ECS services; Service Connect;
EFS; MCP over HTTP; enabling `safety-nlp` in production; removing the
local CLI / `ollama` providers from code (done 2026-09-23).
