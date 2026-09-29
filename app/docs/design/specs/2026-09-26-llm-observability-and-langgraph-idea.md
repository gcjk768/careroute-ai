---
tags: [architecture, careroute, active]
updated: 2026-09-26
---
# Idea — LLM tracing (Langfuse) and a LangGraph adapter

Status: **IMPLEMENTED 2026-09-26** on branch `feat/llm-tracing`, all three at James's request — including
LangSmith, which the table below advised against; it is built as an opt-in sink on the same hooks (prompts
reaching it are already PII-masked). Where the build differs from the plan below: spans are flat per case
(explicit trace id, no ambient context — see `backend/app/tracing.py`), there is no session grouping yet, and
the LangGraph adapter needed the A2A bus to reproduce the interview. Originally parked for a later branch. When picked up, brainstorm briefly against this note, then build in the
order below, one branch per tool.

Related: [[RAG and Onyx]] · [[Lecture Alignment]] · [[Changelog]] · `docs/ARCHITECTURE.md` (LangGraph topology table) ·
`backend/README.md` §"Where LangGraph could replace the hand-rolled supervisor".

## What exists today (verified 2026-09-26)

- No Langfuse, LangSmith, LangGraph or LangChain anywhere in the code or requirements.
- Observability is Prometheus + Grafana (`backend/app/metrics.py`, `monitoring/`): request, guardrail,
  escalation and interview counters; model-inference latency; LLM token, cost and latency metrics with
  four alert rules. This is what closes the Lecture 05 "token and cost tracking" point.
- Per-case tracing is the audit log (`backend/app/audit.py`, queryable by case id) and the recorded
  agent-to-agent conversation on every final event. Correlation ids bind every log line to request + case.
- The gap: **prompts and raw LLM responses are not stored per case.** That is the one thing a tracing
  tool would add.
- Course requirement check: the lecture material lists LangSmith as one *example* of cost tracking;
  `ASPECTS.md` maps observability to Prometheus/Grafana. None of the three tools is a stated requirement.

## Decision

| Tool | Verdict | Why |
|---|---|---|
| **Langfuse** | **Add** (when asked) | Self-hostable and MIT, so patient text stays in the Docker stack (same reasoning as choosing Onyx over a hosted search). Framework-agnostic SDK fits the hand-rolled supervisor and the single LLM gateway. Gives the per-trace prompt/response/cost view Grafana cannot. |
| **LangSmith** | **Do not add** | Free tier is hosted only (self-hosting is an enterprise licence); prompts carry health text. Built around LangChain, which this app does not use, so no integration saving. Covers no rubric point Grafana + Langfuse do not. Mention in the report as the hosted alternative considered. |
| **LangGraph rewrite** | **Do not do** | The supervisor (`agents/orchestration.py`, ~1,350 lines) also runs the planner, the A2A bus, the safety fail-safe protocol, the bounded Reflection loop, the interview stop and ordered SSE streaming; ~60 tests pin that behaviour and the microservices mode runs agents over HTTP. A rewrite is 1–2 weeks of risk for no behaviour change. |
| **LangGraph adapter** | Optional, if LangGraph must appear | A thin `StateGraph` over the EXISTING agents (nodes = agents, `CaseState` = graph state, conditional edges = the planner's shapes) that runs beside the supervisor for demos/diagrams, never on the production path. |

## Langfuse — how it would be built

1. **`backend/app/tracing.py`** (new). Two helpers behind the Langfuse client: `trace_case(case_id)` and
   `record_generation(...)` / `span(agent)`. Every helper is a **no-op** when `LANGFUSE_PUBLIC_KEY` /
   `LANGFUSE_SECRET_KEY` are unset or the `langfuse` package is absent, so tests and CI never touch it.
2. **LLM gateway hook** in `backend/app/llm.py::complete`. The one choke point every agent's LLM call goes
   through; it already knows task, tier, provider, token usage and latency. Record one *generation* per
   call with the redacted prompt (redaction already happens before the prompt is built), the response,
   model, tokens, cost and latency.
3. **Agent span hook** in `PipelineOrchestrator._run_worker` (`agents/orchestration.py`), which already
   times every worker. One span per agent, so a case shows intake → classifier → safety → routing →
   hitl → reflection → handoff as nested steps with their generations inside.
4. **Trace identity.** Trace id = case id from `app/correlation.py`, so a Langfuse trace, an audit entry
   and a Grafana log line share one key. Interview turns of one conversation share a `session_id` =
   the request's `sessionId` when present.
5. **Config.** `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` in `config.py`. Nothing else.
6. **Docker Compose.** A `langfuse` service + Postgres, ClickHouse, Redis, MinIO under a separate compose
   **profile** (`--profile tracing`) so the default stack stays as light as today.
7. **Tests and docs.** Hooks are silent no-ops when unconfigured; a fake client records the expected
   generation for one `complete()` call and the expected spans for one orchestrated case; Changelog and
   vault entries; a line in `ASPECTS.md`.

Privacy: only masked text reaches Langfuse because `llm.complete` receives the redacted prompt.

### How to see the result
- `docker compose --profile tracing up`, open `http://localhost:3000`, create a project once, copy the
  two keys into `.env`, restart the backend.
- Run one triage in the patient UI. It appears under **Traces** named by case id within seconds.
- The trace is a waterfall: agents as nested spans with durations; inside each, the LLM generations with
  the prompt sent, the JSON returned, model, tokens and cost.
- Filters find traces where an agent errored or cost exceeded a threshold; tag a bad output and add it to
  a dataset for evaluation runs. The Dashboard tab gives per-model token/cost totals (overlaps Grafana).

## LangGraph adapter — how it would be built
- `backend/app/agents/graph.py` (new, optional import): `StateGraph(CaseState)` with one node per agent
  calling the existing `run()`; conditional edges reproducing `planner.plan_case` shapes (urgent /
  full / interview-stop after HITL); no bus, no SSE — it is a demonstration surface.
- `scripts/show_graph.py` renders the compiled graph (Mermaid) for the report; a test asserts the graph's
  node/edge set equals the planner's declared shapes so the picture cannot drift from the code.
- Never wired into `main.py`.

## Effort (measured against this codebase)

| Work | Working time | What slows it |
|---|---|---|
| Langfuse hooks, config, no-op tests, docs | 1–2 h | one full backend test run (~50 min locally) |
| Langfuse compose profile, verified end to end | 30–60 min | Docker Desktop must be running to pull images |
| LangGraph adapter + graph-equals-planner test + diagram | 2–3 h | reproducing the plan shapes faithfully |
| LangSmith export on the same hooks | 30 min | only if hosted prompts are acceptable — not recommended |

Order when picked up: Langfuse → (optional) LangGraph adapter → LangSmith only if still wanted.
