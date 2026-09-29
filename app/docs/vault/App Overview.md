---
tags: [architecture, careroute]
updated: 2026-09-29
---
# App Overview

Back to [[Home]]. Related: [[MLOps Pipeline]] · [[Loop Engineering]] · [[RAG and Onyx]]

A single **FastAPI** backend ([`backend/app/main.py`](../../backend/app/main.py)) hosts an agent pipeline
and streams each step to a **Next.js** frontend over Server-Sent Events. Every worker follows the same
contract: **try an LLM step, fall back to deterministic rules** — so the system always produces a safe
result, even with no LLM (which is how the test-suite runs).

The published `/openapi.json` is the API contract: every route declares the error codes and content types
it really returns (400/401/404/429, Prometheus text on `/metrics`, SSE on `/api/triage/stream`), and the
blocking CI job `test:api-fuzz-schemathesis` ([`.gitlab-ci.yml`](../../.gitlab-ci.yml)) fails on any drift.

## Agents (7 workers, one of which orchestrates)
**One file per agent** in the [`backend/app/agents/`](../../backend/app/agents/) package (split from the
former single `agents.py` so 5 members own 5 agents). Each has a least-privilege tool allow-list, an
autonomy level, and an **`AgentContract`** (declared `writes`/`returns` lane over `CaseState`, enforced by
`tests/agents/test_contracts.py`). Owners: Intake=Sham Goh, Classifier=Koh Guan Chin James, Safety=Aaron
Liew, Routing=Marcus Teh, HITL=Heriz Yusoff, Clinician-Handoff=Heriz Yusoff; Reflection/base =
platform (James).

> [!important] Orchestration moved into Symptom-Intake
> There is no separate Supervisor agent any more. `SymptomIntakeAgent` is **both** the first
> worker **and** the orchestrator (L3, `classification=orchestrator`): it mixes in
> [`orchestration.py`](../../backend/app/agents/orchestration.py) (`PipelineOrchestrator`,
> owner Sham Goh) and drives the other five. `supervisor.py` survives only as a deprecated
> alias — `Supervisor is SymptomIntakeAgent`. It drives the other six, the last of which
> (Clinician-Handoff) runs only on escalated cases. `capability.ORCHESTRATOR_SLUG` is the single
> source of truth for who holds the role, and `test_exactly_one_agent_is_the_orchestrator`
> pins that exactly one agent claims it.
>
> **The trade-off, stated:** since 2026-09-24 the steps after Safety follow a per-case plan
> ([`backend/app/agents/planner.py`](../../backend/app/agents/planner.py): full / emergency /
> red_flag / clarify / low_confidence), but every plan is validated against an explicit
> transition graph (HITL, Reflection and Handoff always present; 995 guidance only for a P1)
> and any rejected plan or planner error falls back to the fixed sequence — so an order nothing
> safety-gates can still never run.
> The cost is that the agent producing the pipeline's input is now also the one deciding who
> consumes it, so there is no independent party between those two jobs. `AgentContract`
> enforcement and the safety gate are unchanged and still mechanical.
- **Symptom-Intake** (L2, `intake.py`) — normalises free text → clean clinical sentence + keywords.
  Deterministic path *cleans* (never summarises or translates), so it cannot drop or invent a symptom;
  the LLM path fires only when that pass finds **zero keywords** — which catches another language, a
  misspelling ("cant breath") and odd phrasing alike — and its English output is what lets the
  English-only [`redflags.py`](../../backend/app/redflags.py) table match a non-English emergency.
- **Severity-Classifier** (L2, `classifier.py`) — ML model + SHAP + [[RAG and Onyx|RAG]] citations. Three
  paths (model → LLM → keyword rules) funnelled through one validation choke point; proposes ONE
  clarifying question when under-confident, chosen by information gain — over the model's feature space
  when the model is present, over the rules table when it is not, so elicitation also works offline.
  Keyword matching is word-boundary-anchored and negation-aware ("no chest pain" does not fire chest
  pain). See [[Clarifying Questions Design]].
- **Safety-Override** (L1, `safety.py`) — un-overridable red-flag rules ([`redflags.py`](../../backend/app/redflags.py)), 14 categories incl. `altered_consciousness`, `head_injury` and `meningitis_sepsis`). When a rule fires it also rewrites the model's counterfactual, which no longer applies. See [[Changelog]].
  Two additive layers sit beside the rules and can only ever escalate: a clinical-NLP **shadow pass**
  ([`safety_nlp/`](../../backend/app/safety_nlp/), `CAREROUTE_SAFETY_NLP`, off by default) that records
  telemetry without touching acuity, and a bounded **LLM adjudicator** for uncertain semantic findings
  (`CAREROUTE_SAFETY_LLM`, off by default — read from the environment on every call, not cached in
  `config`). The shadow pass is an agent op like `run`/`areason`, so it executes on both transports.
  The released `careroute-backend` image ships its models (CI `build:images` passes `WITH_SAFETY_NLP=1`
  and `WITH_SAFETY_NLP_DIRECT_NLI=1`; [`prefetch.py`](../../backend/app/safety_nlp/prefetch.py) pins
  and hash-checks NER, assertion, BioLORD and the mDeBERTa direct-NLI comparator from
  [`manifest.json`](../../backend/models/safety/manifest.json)); ECS turns them on per environment.
- **Care-Routing** (L2, `routing.py`) — acuity → care tier + clinic + wait.
- **Human-in-the-Loop** (L2, `hitl.py`) — decides if a clinician must review.
- **Reflection / Critic** (L2, `reflection.py`) — Evaluator-Optimizer; one *more-cautious* corrective pass. See [[Loop Engineering]].
- **Clinician-Handoff** (L2, [`handoff.py`](../../backend/app/agents/handoff.py)) — turns an escalation
  into the packet a reviewing clinician actually reads: a bounded plain-language summary, grounding
  citations, up to two follow-up questions. Runs **last and only when `escalated`** — after the
  Reflection loop, because Reflection can force an escalation nothing upstream flagged, and
  summarising before that settles would describe a case as routine moments before it escalates.
  Faithfulness, not acuity, is its safety property: the prompt is bounded to the case facts and the
  fallback template can invent nothing. Its summary is LLM05-screened in `main.py` before reaching a
  clinician. See [[Clinician Handoff Pipeline Integration]] · [[Clinician Handoff UI]].

**Isolated dev:** each member edits only their agent file + satellite modules + `tests/agents/test_<agent>.py`,
and runs `pytest -m <agent>`. `pytest -m contract` is the boundary guard. Branch per agent, e.g.
`feature/symptom-intake-agent`.

**Agent-to-agent (A2A) communication.** On top of shared `CaseState`, agents talk *explicitly* via a typed
message bus ([`agents/messaging.py`](../../backend/app/agents/messaging.py)): each worker publishes an
`AgentMessage` (sender, recipient, intent, payload, seq) onto an in-process ordered `MessageBus`. Each agent
declares a `COMMS` interface (publish/subscribe intents) — least-privilege for messaging, enforced by
`enforce_comms()`. Flow: `case.opened → symptoms.normalised → acuity.classified → safety.override →
care.routed → review.decision → decision.reviewed`, plus `handoff.ready` on escalated cases only. The bus history is audited, streamed as `agent_message`
SSE events, and returned on the final payload (`messages`). Synchronous/in-process → the safety-gated order
stays deterministic; swap `MessageBus` for a broker in production. Test with `pytest -m comms`.

**Agent vs policy node — declared, not asserted.** CareRoute is a **hybrid**, not an all-LLM multi-agent
system, and each worker says so itself: every one declares an `AgentCapability`
([`agents/capability.py`](../../backend/app/agents/capability.py)) naming its reasoning, action space,
memory and tool use, plus a classification of `agent` / `policy_node` / `orchestrator`.
`enforce_capability()` rejects a declaration the implementation cannot support, and a policy node must
carry an `upgrade_path`. `agents/reasoning.py` is the additive upgrade template — the deterministic
result stays as the floor and a reasoning layer may only escalate, so recall cannot regress.
Exactly one worker sets `uses_trained_model` (the Severity-Classifier). Test with `pytest -m capability`;
detail in [[Agent Capability Audit]].

**Evaluations.** Six evaluations are specified as code in
[`app/evals/plan.py`](../../backend/app/evals/plan.py) — each with a dataset, expected outputs, metrics
and acceptance criteria, validated on every CI run. Test with `pytest -m eval`; detail in
[[Evaluation Plan]]. Both of the above answer [[Proposal Review Feedback]].

## ML severity model
[`backend/app/ml/`](../../backend/app/ml/) — a real `RandomForestClassifier`:
- **Training data** — synthetic profiles in [`backend/app/ml/data.py`](../../backend/app/ml/data.py). A forest
  extrapolates badly on symptom pairs it never saw (the milder feature can decide: "high fever with body aches"
  once served P5), so a live miss is fixed by adding the missing profile and re-running the release gates — model
  `33e265607d69` (2026-09-25).
- **Explainability** — SHAP (local + global), a single-edit counterfactual, and a SHAP-vs-local-surrogate agreement audit (the `lime` package was never installed; the in-repo surrogate replaces it).
- **Fairness** — accuracy gap, demographic parity, equal opportunity, **equalized odds** (TPR *and*
  FPR, so over-triaging cannot pass as fairness), **disparate impact** (four-fifths ratio — reported,
  not gated, because age-adjusted acuity is correct medicine), counterfactual (sex-flip).
  Served scoring is sex-blind (`_SexBlind` in [`backend/app/ml/model.py`](../../backend/app/ml/model.py)): sex acts only via the per-group thresholds. See [[Changelog]].
- **Data documentation** — [`backend/DATASHEET.md`](../../backend/DATASHEET.md) (Gebru et al.),
  pinned to `triage_dataset.meta.json` by `tests/test_datasheet.py` so it cannot go stale.
- **Drift** — PSI (data/target/concept). **Calibration** — isotonic, ECE/Brier.
- **Integrity** — content-addressed version + SHA-256 (`dataSha256`/`modelSha256`).
- Training/serving split: [`ml/train.py`](../../backend/app/ml/train.py) builds; `get_model()` loads. See [[MLOps Pipeline]].
- **Release gate** — train-time floors (acc ≥ 0.75, red-flag recall ≥ 0.95) are IDENTICAL to CI's
  `test:model-gate`, so a failing model is never persisted or registered.
- **Serving telemetry** — every prediction feeds model-level Prometheus metrics (class mix, confidence,
  `careroute_model_info{version}`) + a de-identified JSONL **inference log**
  ([`ml/inference_log.py`](../../backend/app/ml/inference_log.py)) that live monitoring reads. See [[MLOps Pipeline]].
- **HITL ground truth** — a clinician's `finalAcuity` on an escalation decision is recorded vs the
  model's prediction (`backend/monitoring/ground_truth.jsonl` + agreement metric): real labels for
  live accuracy and future retraining.
- **Batch serving** (2026-09-17) — [`ml/batch_score.py`](../../backend/app/ml/batch_score.py)
  re-scores the PENDING escalation queue against the currently served model and reports the
  cases it now ranks more urgent than the model that triaged them. Reports only: it mutates no
  escalation, because re-prioritising a clinical queue overnight is a decision for a person.
  Not scheduled — the in-memory store gives an out-of-process cron nothing to read.
- **Train/serve skew gate** (2026-09-17) —
  [`ml/feature_contract.py`](../../backend/app/ml/feature_contract.py) fingerprints the feature
  extractor's configuration and behaviour into the artifact at training time and verifies it at
  load, so a keyword edit that changes what a feature MEANS rejects the artifact instead of
  being served silently.
- **Availability — yield and harvest** (2026-09-17). Yield (served / received) and uptime are
  recording rules; **harvest** ([`app/availability.py`](../../backend/app/availability.py)) scores
  how COMPLETE each served answer was, over five independently-degradable components. It exists
  because this app degrades rather than fails: every fallback returns a successful response, so a
  dependency outage is invisible to yield by construction.

## Platform components (2026-09-16)
Four cross-cutting pieces that are not agents but are named components in the Day 3 AM
deck, each served over the API so a reviewer can see them rather than take them on trust:

* **Agent Registry** ([`agents/registry.py`](../../backend/app/agents/registry.py), `GET /api/agents`)
  — the canonical worker set, which the test harness re-exports so app and tests cannot
  diverge, plus discovery built from each worker's own declared `AgentCapability`. Records
  deliberately carry **no endpoint or transport** — but the *reason* changed on 2026-09-19:
  agent-as-service is now implemented (see Microservice deployment below), and the address
  table is deployment configuration (`config.AGENT_URLS`), not something an agent announces
  about itself. See [[Agent Capability Audit]].
* **Tool Registry** ([`tools/registry.py`](../../backend/app/tools/registry.py), `GET /api/tools`)
  — the catalog behind Care-Routing's bounded ReAct loop; the schema shown to the model is
  the schema enforced at use.
* **Correlation** ([`correlation.py`](../../backend/app/correlation.py)) — a per-request
  ContextVar and a logging **filter on the handlers**, so every log line carries
  `correlation_id` + `case_id` including from modules that never opted in. Inbound
  `X-Correlation-ID` is honoured but sanitised: it is untrusted data heading for a log file,
  and a newline would let a caller forge whole log lines.
* **Registry lifecycle** ([`ml/lifecycle.py`](../../backend/app/ml/lifecycle.py),
  [`ml/promote.py`](../../backend/app/ml/promote.py)) — `@challenger` on every gated build,
  `@champion` only by explicit promotion. Aliases rather than MLflow stages, which 3.x
  removed. See [[Lecture Alignment]] Gap 1.

**Retrieval is hybrid** as of 2026-09-16: chunked dense embeddings fused with lexical TF-IDF
by Reciprocal Rank Fusion when an embedder is present, lexical-only otherwise (which is what
CI runs). Measured on the 32-query E10 set: hybrid Recall@2 **0.969** / MRR 0.917 against
lexical 0.500 / 0.500 — lexical scores 1.0 on queries that share the document's words and
0.0 on paraphrases. See [[RAG and Onyx]].

**How much context is handed over is adaptive** as of 2026-09-17: `top_k` is a ceiling, not a
quota. [`rag.assemble_context`](../../backend/app/rag.py) keeps the top document and each
further one only while its dense cosine is within `CONTEXT_MARGIN` of the best, so a query
with one clear answer is answered with one document. E12
([`evals/context.py`](../../backend/app/evals/context.py)) scores the result: context
precision **0.484 → 0.750** at unchanged recall 0.969. Installing the optional embedder extra
also requires the main-thread warm-up in
[`rag_embed.warm`](../../backend/app/rag_embed.py) — the first onnxruntime import on a worker
thread is an access violation, not an exception.
* **LLM tracing** (2026-09-26, [`tracing.py`](../../backend/app/tracing.py)) — optional Langfuse and/or
  LangSmith sink: one span per agent run (from `_run_worker` in [`agents/orchestration.py`](../../backend/app/agents/orchestration.py)),
  one generation per served LLM call (from `complete` in [`llm.py`](../../backend/app/llm.py), masked prompt),
  all keyed by case id — the gateway container gets it via `X-Case-Id`. Local Langfuse: compose profile `tracing`.
* **LangGraph adapter** (2026-09-26, [`agents/graph.py`](../../backend/app/agents/graph.py)) — the same pipeline
  as a `StateGraph` (nodes = agents, edges from `planner.ALLOWED_TRANSITIONS`, A2A bus carried in graph state).
  Not on the production path; [`tests/agents/test_graph.py`](../../backend/tests/agents/test_graph.py) pins it to the supervisor.

## Patient language (2026-09-29)
The patient reads the clarifying question, the rationale and the routing note **in the language they
wrote in, with the English underneath**. Intake's `detected_language` (zh / ms / ta / es / fr) drives
one `patient.translate` LLM call (fast tier, cacheable) in
[`patient_language.py`](../../backend/app/patient_language.py), made once at the edge in
[`main.py`](../../backend/app/main.py) before the `final` event. The English fields are unchanged
(the interview dedupes on them; the audit stays English); the translations ride in
`final.translations` and render via `Bilingual` in
[`PatientTriage.jsx`](../../frontend/components/PatientTriage.jsx). Any failure → English only.
Static UI labels (quick replies, action guidance) are still English.

## Security & safety controls
Mapped to the OWASP LLM Top 10 **(2025 edition)** and the OWASP Agentic Top 10
([`backend/SECURITY.md`](../../backend/SECURITY.md)): input+output guardrails, PII/PHI redaction,
least-privilege tool allow-lists, deterministic red-flag override, rate limiting, a kill switch, and a
hash-chained tamper-evident audit log. These deterministic checks are the **verifiers** in
[[Loop Engineering]] terms.

Two agentic controls sit alongside them:

* **Episodic-memory integrity (ASI06).** `store.memory_digest()` fingerprints each case as it enters
  episodic memory; `recall_session()` re-verifies before replaying a prior visit into a new triage and
  **excludes** any record that fails or has no digest. `verify_episodic_memory()` reports what was
  dropped.
* **Provider circuit breaker (ASI08).** Three consecutive failures skip an LLM provider for a 30 s
  cooldown, then one half-open trial decides whether it reopens — so a dead provider is not waited on
  once per worker per case. Breaker state is on `GET /api/health`.
* **Model tiers.** Every call is routed by task weight in [`llm.py`](../../backend/app/llm.py):
  fast `gpt-5.4-mini`, deep `gpt-5.4`, max `gpt-5.5` (`_TIER_DEFAULTS`; `OPENAI_MODEL_FAST/DEEP/MAX`
  override). GPT-5 params the model rejects (`temperature`, `reasoning_effort`) are dropped and
  remembered per model. Chosen by the 100-scenario head-to-head in
  [`model_soak.py`](../../backend/scripts/model_soak.py) — see [[Changelog]] 2026-09-27 (fourth).
  Over HTTP each agent container returns its calls as `llm_calls`, merged by `RemoteAgent`
  ([`remote.py`](../../backend/app/microservices/remote.py)), so the panel lists every tier.
  `GET /api/health` lists the resolved model per tier under `tiers` (`llm.tier_models`); its `model`
  field is only the base `OPENAI_MODEL`, which no routed call uses.
* **OneMap readiness.** `GET /api/health` also carries `oneMap: {configured, circuit, routing}`
  from [`onemap_status()`](../../backend/app/agents/routing.py). `routing: "estimate_only"` means
  every case is on the labelled distance-and-speed fallback — usually
  `ONEMAP_EMAIL`/`ONEMAP_PASSWORD` missing from the deployed task. Reported because map *tiles*
  are unauthenticated, so the degradation is invisible from the browser.

The classical model is treated as an attack surface in its own right: evasion
([`test_robustness.py`](../../backend/tests/test_robustness.py)), plus poisoning and membership
inference ([`test_ml_attacks.py`](../../backend/tests/test_ml_attacks.py), job
`ai-security:ml-attacks`). Model extraction is measured and recorded as an accepted risk.

The **input guardrail is 6-layer defence-in-depth** (not a single regex; no LLM dependency), per OWASP
LLM01 — [`guardrail.py`](../../backend/app/guardrail.py): hygiene → **normalization** (NFKC homoglyph
fold, strip zero-width/RTL, whitespace, leetspeak) → **decode-and-rescan** (base64/hex/URL) → injection
denylist → **structural detection** (`<|system|>`, `[INST]`, code fences, role JSON) → **topical scoping**
(positive clinical-relevance signal rejects off-scope inputs; conservative so real symptoms pass). This
repairs the classic regex bypasses (encoding, homoglyphs, obfuscation) and is a clean loop-engineering
verifier. See [[Changelog]] (2026-07-18 guardrail defence-in-depth).

**Guardrail de-obfuscation.** [`backend/app/guardrail.py`](../../backend/app/guardrail.py) layer 3b decodes ciphers, encodings, look-alikes ([`backend/app/confusables_ascii.json`](../../backend/app/confusables_ascii.json)) and spacing before the injection patterns run; [`backend/app/evals/pyrit_probe.py`](../../backend/app/evals/pyrit_probe.py) measures it with PyRIT in CI. See [[Changelog]].

## Frontend
Next.js App Router + CSS Modules (`frontend/`): patient triage surface + staff portal (pipeline view,
clinician HITL queue, fairness/governance dashboard). Offline-resilient in-browser simulation fallback.

**Staff auth is server-side.** `frontend/app/api/staff/session/route.js` checks `CAREROUTE_STAFF_PASSWORD` and sets
an httpOnly signed cookie (`frontend/lib/staffSession.js`); `frontend/app/api/escalations/[[...path]]/route.js` refuses
(401) without it whenever `CAREROUTE_STAFF_API_KEY` is set. localStorage only holds the display name. See [[Changelog]].

**Gate rendering on the governance surface.** `frontend/components/GovernanceDashboard.jsx` holds a
`GATE` constant mirroring the release thresholds in `backend/app/ml/model.py` (`MIN_ACCURACY` 0.75,
`MIN_RED_FLAG_RECALL` 0.95, `MAX_FAIRNESS_GAP` 0.35). `/api/fairness` serves measurements but not the
thresholds (`FairnessResponse`), so the frontend restates them; **keep the two in step**, or the
sign-off screen starts grading against a gate CI does not enforce — which is exactly the defect fixed
on 2026-09-18, when the page advertised a ≥ 99% recall target that existed nowhere in the pipeline and
painted every gated KPI green regardless of whether it passed. The per-subgroup bar chart's y-axis
is pinned to `[0, 1]`: the previous `[0.8, 0.95]` domain clipped the worst subgroup (0.791) off the
chart entirely, so the coral "lowest-performing" highlight never rendered. See [[Changelog]]
(2026-09-18 fairness chart).

## Microservice deployment — one agent, one container (2026-09-19)
`AGENT_TRANSPORT` is a **switch, not a fork**. Unset (`inprocess`, the default) every worker runs in
one process, which is what the test suite and the `monolith` image do. Set to `http`, the
intake-gateway reaches each worker through a `RemoteAgent` proxy
([`backend/app/microservices/remote.py`](../../backend/app/microservices/remote.py)) that POSTs to that
agent's own container. [`tests/test_ms_transport_parity.py`](../../backend/tests/test_ms_transport_parity.py)
asserts all 20 gold vignettes reach the identical decision and agent conversation either way, which is
what makes the split a deployment choice rather than a second behaviour to maintain.

Every step the orchestrator takes on an agent must therefore be an **op** in
[`microservices/workers.py`](../../backend/app/microservices/workers.py) (`OPS`) with a matching method
on the proxy — safety's are `prescreen`, `shadow_nlp`, `areason`, `run`, `emit`. A step called
directly on the in-process worker instead silently disappears under `AGENT_TRANSPORT=http`, and the
parity suite is what catches it.

Ten backend containers on one compute: intake-gateway `:8000`, classifier `:8101`, safety `:8102`,
routing `:8103`, reflection `:8104`, hitl `:8105`, handoff `:8106`, llm-gateway `:8107`,
rag-service `:8108`, redis `:6379`. Only the gateway publishes a port, and it carries the Compose
network alias `backend` so the frontend build arg and `monitoring/prometheus.yml` did not change.

**Why the split lands where it does.** Along secrets — `llm-gateway`
([`microservices/llm_app.py`](../../backend/app/microservices/llm_app.py)) is the only container given
`OPENAI_API_KEY`, and `llm.complete()` forwards to it whenever `CAREROUTE_LLM_GATEWAY_URL` is set;
along state — redis; along resource profile — `rag-service` holds the embedder, `classifier-agent` the
only image with the trained model. Pure deterministic safety code (guardrail, redaction, red-flag
rules) stays **in-process** so it can never fail over a network.

**Degradation never gets less cautious.** An unavailable classifier / safety / routing / hitl answer
escalates the case to a clinician and never lowers the care tier; a down Reflection still escalates.
So a broken agent produces a *completed, escalated* case and nothing else looks wrong — which is why
`AgentDown` exists in [`monitoring/alert.rules.yml`](../../monitoring/alert.rules.yml). It fires on
call outcomes (all calls failed, none succeeded in 5 min), not on breaker state: a container that
answers every call with a contract violation never opens a breaker and is just as down.

**Durability.** [`backend/app/persistence.py`](../../backend/app/persistence.py) write-throughs the case
store, episodic-memory digests, escalation queue and audit trail to Redis, and reloads them on start,
so a gateway restart loses no escalation a clinician has not seen and the audit hash chain still
verifies. Writes are **best effort** — a Redis outage is logged and never fails a triage. With
`CAREROUTE_REDIS_URL` unset none of it runs.

**One Dockerfile, one target per service**, default target `monolith` — so `docker build backend` still
produces today's single-process image, which CI still pushes because the live ECS service deploys it.
`scripts/compose_smoke.py` is the check that the wiring actually works: it fails unless a real red-flag
triage completed, escalated, AND the gateway recorded a successful call to every one of the six agent
containers, so a fully degraded run cannot pass as healthy.

**Shipping to ECS** is this repo's `deploy:ecs` job (automatic on `release/deploy`, a manual button on `main`) ([`scripts/deploy_ecs.sh`](../../scripts/deploy_ecs.sh)):
it registers a new task-definition revision with the pushed tag, rolls `backend` + `frontend`, and goes
green only when ECS is serving that tag. `rollback:production` uses the same script. careroute_ai_infra
(Terraform) owns everything else. See [[Changelog]] 2026-09-23 (night).

**Not deployed.** The ECS side (ECR repos, a ten-container task definition, per-container secrets,
scrape targets) is a separate plan in `careroute_ai_infra`. See [[Changelog]] (2026-09-19 agent
microservices) and [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) §5a.
