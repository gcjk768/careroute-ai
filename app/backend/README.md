# CareRoute AI — Backend

A FastAPI backend for a multi-agent healthcare triage assistant. A
**Supervisor** agent orchestrates five worker agents (Symptom-Intake,
Severity-Classifier, Safety-Override, Care-Routing, Human-in-the-Loop),
each of which reasons via an **LLM provider** (the hosted OpenAI API) and
falls back to deterministic rule-based logic when no provider is available —
so a demo never fails, with or without a model.

## Running

```bash
cd app/backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

The API is served at `http://localhost:8000/api/...`. CORS is enabled for
`http://localhost:5173` and `http://localhost:3000` (typical Vite/CRA dev
ports).

### LLM provider chain

Agent reasoning is served by a configurable **provider chain** (`app/llm.py`);
the first provider that returns a usable answer wins, and if all fail the agents
use deterministic rule-based logic. Configure via `.env` (copy `.env.example`):

| Order | Provider | How it's used |
|-------|----------|---------------|
| 1 | **OpenAI / ChatGPT API** | Used only when `OPENAI_API_KEY` is set. Model via `OPENAI_MODEL` (default `gpt-4o-mini`). |

`LLM_PROVIDER_ORDER` (default `openai`) names the providers to try. **The
provider is optional** — with none available, every worker falls back to
deterministic keyword/rule logic. `GET /api/health` reports the active provider
(or `rules` when the deterministic fallback is in effect).

The local CLI provider (a subprocess on a developer laptop) and the
Ollama provider (a localhost server) were removed on 2026-09-23: neither exists
in the deployed service, and a chain that can resolve to a developer's local
tool is not a production chain. `tests/test_llm_providers.py` pins this.

> **Privacy:** the provider sends patient text to an external LLM. For real
> PHI use a BAA-covered endpoint (`OPENAI_BASE_URL`) or leave the provider
> unset so the deterministic path handles every case — see `SECURITY.md`
> (LLM06). Secrets are read from the environment only, never logged.

## Architecture

```
Guardrail (deterministic prompt-injection screen)
        │
        ▼
   Supervisor
        │
        ├─▶ Symptom-Intake        (LLM → structured symptoms, fallback: keyword cleanup)
        ├─▶ Severity-Classifier   (ML model → acuity + confidence + real SHAP; fallback: LLM, then keyword rules)
        ├─▶ Safety-Override       (always deterministic; can only escalate acuity, never relax it)
        ├─▶ Care-Routing          (deterministic acuity → care tier → mock clinic/wait time)
        └─▶ Human-in-the-Loop     (deterministic: red flag OR confidence < 0.6 → escalate)
        │
        ▼
  RAG citation lookup (in-memory corpus, keyword overlap)
        │
        ▼
  Final aggregated, cited response + persisted case/escalation
```

Each case is streamed to the client over Server-Sent Events
(`POST /api/triage/stream`) with one event per pipeline stage, and a small
`asyncio.sleep` between steps so a frontend can animate the pipeline as it
runs.

### Where LangGraph could replace the hand-rolled supervisor

`app/agents.py`'s `Supervisor` class currently calls workers in a fixed
sequence and mutates a shared `CaseState` dataclass — effectively a
linear graph with one conditional edge (the safety-override can force a
re-route). This was intentionally kept simple and dependency-free for a
demo. A production version could model the same workers as **LangGraph**
nodes with the `CaseState` as the graph state, using conditional edges for:

- looping back to the classifier if intake confidence is very low,
- branching directly from Safety-Override to Human-in-the-Loop without
  waiting for Care-Routing when a top-severity red flag fires,
- adding retry/backoff edges around the LLM call nodes instead of the
  in-function try/except used here.

The worker logic itself (`SymptomIntakeAgent`, `SeverityClassifierAgent`,
etc.) would not need to change — only the orchestration wiring in
`Supervisor` / `main.py`'s event stream would move into a `StateGraph`.

## Files

- `requirements.txt` — minimal deps (fastapi, uvicorn, httpx, pydantic)
- `requirements-dev.txt` — test-only deps (pytest, pytest-asyncio, httpx)
- `app/models.py` — Pydantic request/response models, acuity enum/table
- `app/llm.py` — LLM provider chain (`complete`, `health`); the OpenAI provider, breaker, router and cache
- `app/config.py` — typed, env-driven configuration (provider order, models, timeouts, secrets)
- `app/ml/` — real severity model: `features.py`, `data.py` (synthetic + injected fairness bias), `model.py` (train + SHAP + audit), `fairness.py` (subgroup metrics + PSI drift)
- `app/guardrail.py` — deterministic prompt-injection/jailbreak screen
- `app/redflags.py` — hard-coded red-flag rule table + evaluator
- `app/rag.py` — in-memory clinical-guidance corpus + `retrieve()`
- `app/agents.py` — the five worker agents + Supervisor orchestration, tool allow-lists, `explain()`
- `app/audit.py` — per-case audit trail (FR-13 traceability)
- `app/store.py` — in-memory cases/escalations/fairness store + seed data
- `app/main.py` — FastAPI app, CORS, routes, SSE generator, audit endpoint
- `.env.example` — `LLM_PROVIDER_ORDER`, `OPENAI_*`
- `SECURITY.md` — AI security risk register (OWASP LLM Top 10 mapping)
- `tests/` — unit + e2e + AI-security test suite (see Testing below)

## API summary

- `GET /api/health`
- `POST /api/triage/stream` (SSE)
- `GET /api/escalations`
- `GET /api/escalations/{id}`
- `POST /api/escalations/{id}/decision`
- `GET /api/fairness`
- `GET /api/cases/{case_id}/audit` — per-case audit trail (FR-13; 404 if unknown)

## Testing

```bash
cd app/backend
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

The suite forces the deterministic (rule-based) path in **every** test (via a
global `conftest.py` fixture), so tests never call any network provider — they are fast, reproducible, and assert on structure
and the deterministic safety invariants, never on model wording. This also
directly exercises the "runs without a model" guarantee.

- `tests/test_guardrail.py` — AI-security: clean input passes; a range of
  prompt-injection/jailbreak payloads are blocked; empty/oversized input
  is blocked.
- `tests/test_redflags.py` — agent-behaviour: chest pain / stroke signs →
  P1; suicidal ideation escalates; `apply_override` can only raise
  severity, never lower it; no red flags → not triggered.
- `tests/test_agents.py` — with the LLM disabled, Symptom-Intake
  and Severity-Classifier fall back deterministically; ambiguous input
  yields classifier confidence below the 0.6 threshold; explanation is
  always non-empty; `enforce_tool_access` raises for a disallowed tool.
- `tests/test_api_e2e.py` — full API contract via `TestClient`: health,
  fairness shape, non-empty escalations, an SSE triage run ending in a
  `final` event with `acuity.code == "P1_RESUSCITATION"` and
  `escalated == True` for chest pain, a prompt-injection request that is
  guardrail-blocked with **no** `agent_result`/`agent_active`/`final`
  events, escalation decision flip + 404 on unknown id, and the audit
  endpoint returning entries after a run.
- `tests/test_ml.py` — Responsible-AI/MLOps: features extract expected flags;
  the model predicts severe cases with a non-empty **real SHAP** explanation;
  vague input is low-confidence (drives escalation); the **fairness audit is
  real** and the mitigation does not worsen the gap (`after <= before`); PSI is
  zero for identical distributions.

## How aspects map to code

| Course aspect | Where in this codebase |
|---|---|
| **Explainable & Responsible AI** | `app/ml/model.py` — a real scikit-learn severity model with **real SHAP** explanations (`shap.TreeExplainer`), a **real stratified fairness audit** (per-subgroup accuracy, before→after gap from an age-blind vs age-aware + rebalanced mitigation, red-flag recall) and **real PSI drift**, served at `GET /api/fairness`; surfaced via `CaseState.explanation` → `final` SSE event and the governance dashboard. Keyword surrogate + seeded snapshot remain as fallbacks. |
| **AI & Cybersecurity** | `app/guardrail.py` (prompt-injection screen), `app/redflags.py` (un-overridable deterministic safety net), `enforce_tool_access`/`TOOL_ALLOWLIST` in `app/agents.py` (least privilege), `SECURITY.md` (OWASP LLM Top 10 risk register) |
| **Architecting Agentic AI** | `app/agents.py` — five workers + `Supervisor`, each with a declared `AUTONOMY_LEVEL` (Spectrum of Agency L1–L3) and `TOOL_ALLOWLIST`; safety-gated fixed orchestration order in `Supervisor`/`_triage_event_stream` |
| **Integrating & Deploying (MLSecOps/LLMSecOps)** | `app/audit.py` + `audit_log.record(...)` calls in `app/main.py` (per-case, per-step audit trail), `logging.getLogger("careroute")` INFO lines per pipeline step, `GET /api/cases/{case_id}/audit`, `tests/` + `requirements-dev.txt` (CI-able test gate) |

Throughout the touched files, inline comments are tagged
`[Responsible-AI]`, `[AI-Security]`, `[Agentic]`, `[MLOps]` to make this
mapping traceable directly in the source.
