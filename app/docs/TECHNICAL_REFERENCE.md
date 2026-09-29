# CareRoute AI — Technical Reference

> **This is the deep technical reference.** For an overview of what CareRoute is, how it works, who
> built what, and where each course requirement is demonstrated, start at the
> **[README](../README.md)** — it is the shorter, plain-language document. This file is the full
> engineering detail behind it: every agent contract, every security control, the complete API and
> configuration surface, the full test inventory, and every CI job and scanner.

**Contents at a glance:** [Architecture](#architecture) · [The agents](#the-agents) ·
[LLM provider chain](#the-llm-provider-chain) · [ML model & fairness](#the-ml-severity-model-explainability--fairness) ·
[Security controls](#security--safety-controls) · [Frontend](#frontend-surfaces) · [API](#api-reference) ·
[Configuration](#configuration) · [Running it](#running-it) · [Testing](#testing) ·
[CI/CD](#cicd--the-mlsecops-pipeline) · [Project structure](#project-structure)

---

A working, full-stack prototype for the **NUS-ISS "Architecting AI Systems" Practice Module**
(Team 3, Proposal 3). The **Symptom-Intake** agent orchestrates a team of specialised worker agents to
turn a patient's free-text symptoms into a **cited, explainable triage recommendation** — an acuity
level (P1–P5), a care tier, and a wait estimate — while a deterministic safety layer and a
human-in-the-loop escalation path keep a clinician in control.

> **AI assists, the clinician decides.** This is a Practice-Module prototype, **not** a certified
> medical device. Do not use it for real clinical decisions.

The project is deliberately built to demonstrate all four graded course modules at once:

| Module | Demonstrated by |
|--------|-----------------|
| **Explainable & Responsible AI** | Real SHAP explanations, a full fairness audit (accuracy gap, demographic parity, equal opportunity, counterfactual), PSI drift, a Model Card, and PDPC-aligned governance |
| **AI & Cybersecurity** | Input **and** output guardrails, deterministic red-flag override, least-privilege tool allow-lists, PII redaction, rate limiting, a kill switch, a tamper-evident audit log, and OWASP-LLM-mapped red-team scanning |
| **Architecting Agentic AI Solutions** | Seven agents (incl. a Reflection/Critic and a Clinician-Handoff that runs only on escalation), one of which also orchestrates on a private worker set per request, a safety-gated pipeline, per-agent autonomy levels, shared state + a **typed agent-to-agent message bus**, episodic memory, and vector-based RAG grounding |
| **Integrating & Deploying (MLSecOps)** | A 10-stage GitLab CI/CD pipeline with blocking model-quality / data-validation / data-lineage / fairness gates, JUnit tests behind an 80% coverage floor, MLflow tracking + DVC data versioning, Evidently drift monitoring with a **closed drift→retrain loop**, an inference log + model-level Prometheus metrics, a champion–challenger promotion gate, and an executive PDF/Markdown report |

Full requirement-to-code traceability is in **[`ASPECTS.md`](../ASPECTS.md)**.

---

## Table of contents
1. [Architecture](#architecture)
2. [The agents](#the-agents)
3. [The LLM provider chain](#the-llm-provider-chain)
4. [The ML severity model, explainability & fairness](#the-ml-severity-model-explainability--fairness)
5. [Security & safety controls](#security--safety-controls)
6. [Frontend surfaces](#frontend-surfaces)
7. [API reference](#api-reference)
8. [Configuration](#configuration)
9. [Running it](#running-it)
10. [Testing](#testing)
11. [CI/CD — the MLSecOps pipeline](#cicd--the-mlsecops-pipeline)
12. [Project structure](#project-structure)
13. [Further documentation](#further-documentation)

---

## Architecture

A single FastAPI backend hosts the agent pipeline and streams every step to the browser over
**Server-Sent Events (SSE)**, so the React frontend can animate the pipeline working in real time.

![CareRoute AI — system architecture](diagrams/generated/system-architecture.png)

> The named architecture style, the physical architecture and deployment model, the UML deployment
> diagram and the target cloud architecture live in [`ARCHITECTURE.md`](ARCHITECTURE.md).

> [!note] The diagram above is **generated** from [`diagrams/src/system-architecture.mmd`](diagrams/src/system-architecture.mmd)
> by `node scripts/render-diagrams.mjs`, so it cannot drift. The older hand-drawn
> [`careroute_logical_architecture.drawio`](diagrams/careroute_logical_architecture.drawio) is kept for
> its finer detail, but its exported `.png`/`.svg` are stale — draw.io's CLI will not run headless here,
> so they need a manual export from draw.io Desktop.

> **Editable source:** [`docs/diagrams/careroute_logical_architecture.drawio`](diagrams/careroute_logical_architecture.drawio)
> — open in [draw.io / diagrams.net](https://app.diagrams.net) for the clearest, zoomable view; a crisp
> [SVG version](diagrams/careroute_logical_architecture.svg) is also provided. Ingress runs
> `rate-limit → 6-layer input guardrail → PII redaction → orchestrator`; Symptom-Intake holds the
> orchestrator role and drives the seven-agent pipeline in a fixed, safety-gated order on a **private
> worker set per request** (Clinician-Handoff runs only when a case is escalated); Care-Routing selects
> only from verified clinics (CHAS directory, GoWhere hours, OneMap); the output guardrail screens the
> rationale before the final SSE event; the stream carries a keepalive every 10 s; and the LLM provider
> chain backs every worker with a deterministic fallback.

Every worker follows the same resilience contract: **try an LLM-backed reasoning step, and on any
failure fall back to deterministic rule-based logic.** The pipeline therefore always produces a
safe, sensible result — even with no LLM available at all (which is exactly how the test-suite runs).

---

## The agents

Seven agents live in the [`backend/app/agents/`](../backend/app/agents/) package — **one file per
agent** (`intake.py`, `classifier.py`, `safety.py`, `routing.py`, `hitl.py`, `handoff.py`, plus
platform-owned `reflection.py` and the shared `base.py`; `orchestration.py` holds the pipeline
machinery and `supervisor.py` is a **deprecated alias** kept only so old imports resolve — it is not
an eighth agent). Each declares a **least-privilege tool
allow-list**, an **autonomy level** (Spectrum of Agency), its **prompt pattern**, and an
**`AgentContract`** (the CaseState fields it may write + the result keys it must return) — all
enforced/annotated in code.

| Agent | Autonomy | Tools (allow-list) | Role |
|-------|----------|--------------------|------|
| **Symptom-Intake** *(also the orchestrator)* | L2 worker · L3 orchestrating | `llm.complete` + orchestrator tools | Normalises raw/multilingual input into a clean clinical sentence + keywords, then runs the workers in a fixed, safety-gated order and aggregates the cited response |
| **Severity-Classifier** | L2 | `ml.predict`, `llm.complete`, `rag.retrieve` | Estimates acuity + confidence + a **SHAP** explanation (ML model primary, LLM then keyword fallback) |
| **Safety-Override** | L2 over an L1 floor | `redflags.evaluate`, `llm.complete`, `safety_nlp.classify` | Un-overridable red-flag rules that can only **raise** acuity (e.g. chest pain → P1), plus a **semantic NLP layer** ([`app/safety_nlp/`](../backend/app/safety_nlp/)) that can only add signals on top of the deterministic floor |
| **Care-Routing** | L2 | `clinic.lookup`, `facility.hours.lookup`, `travel.estimate`, `llm.complete` | Maps final acuity → care tier + a clinic and wait estimate, over verified clinics only |
| **Human-in-the-Loop** | L2 | `escalation.create` | Decides if a human must review (low confidence or safety trigger) |
| **Clinician-Handoff** | L2 | `llm.complete`, `rag.retrieve` | Builds the clinician handoff packet — **runs only when a case is escalated** |
| **Reflection / Critic** | L2 | `critique` | *Evaluator-Optimizer*: reviews the assembled decision and applies one **more-cautious** corrective pass (e.g. forces escalation of an un-escalated P1/P2) |

The **Reflection agent** is the newest addition — it implements Ng's *Reflection* / Anthropic's
*Evaluator-Optimizer* pattern: a dedicated critic that catches an inconsistent or unsafe aggregate
the individual workers can each miss, and can only make the outcome safer, never less safe.

**Shared state & memory.** A `CaseState` dataclass is threaded through the whole pipeline (working
memory). Completed cases are indexed by `sessionId` for **episodic recall** across visits
(`GET /api/sessions/{id}/history`).

#### Agent or policy node? — declared in code, not asserted in prose

CareRoute is a **hybrid**, not an all-LLM multi-agent system, and each worker says so itself. Every one
declares an **`AgentCapability`** ([`agents/capability.py`](../backend/app/agents/capability.py)) naming
its **reasoning**, **action space** (autonomy made countable), **memory** and **tool use**, plus a
classification. `enforce_capability()` refuses a declaration the implementation cannot support — you
cannot call your worker an agent without an inference step *and* more than one available outcome — and
**a policy node must carry an `upgrade_path`**, so the route to genuine agency lives beside the code.

| Worker | Class | Reasons? | Action space | Trained model |
|--------|-------|----------|--------------|---------------|
| Symptom-Intake | **orchestrator** (+ agent) | LLM extraction | open (sentence + keywords + language) + the fixed worker sequence | — |
| Severity-Classifier | **agent** | RandomForest → LLM → keywords | 5 acuity codes | **yes — the only one** |
| Safety-Override | **agent** | semantic NLP layer over the regex floor | confirm / add / add-nothing | — |
| Care-Routing | **agent** | constrained LLM clinic selection over verified nearby candidates | choose candidate / nearest fallback / safe offline route | — |
| Clinician-Handoff | **agent** | LLM summarisation, grounded + faithfulness-checked | packet content (deterministic template on fallback) | — |
| Human-in-the-Loop | policy node | no | escalate / don't | — |
| Reflection / Critic | policy node | no | reroute / force-escalate / pass | — |

Being deterministic is a *design choice*, not a shortcoming: a red-flag interlock that could be reasoned
around would not be an interlock. The upgrade pattern is therefore **additive, never a replacement** —
[`agents/reasoning.py`](../backend/app/agents/reasoning.py) keeps the deterministic result as the **floor**
and lets a reasoning layer only escalate, so a hallucinating layer can cost precision but **cannot cost
recall**, and with the LLM down behaviour is byte-identical. `safety.py` is the worked example.

Run the audit with `pytest -m capability`; the evaluation plan behind these claims is
[`app/evals/plan.py`](../backend/app/evals/plan.py) (`pytest -m eval`).

### Agent-to-agent message bus

Least-privilege publish/subscribe: an agent declares the intents it may publish and subscribe to, and
the framework refuses anything else at runtime. The [README](../README.md#4-how-the-agents-talk-to-each-other)
carries the same information as a table plus a sequence diagram of one real case.

![Publish and subscribe topology](diagrams/generated/a2a-pubsub.png)

<sub>Source: [`a2a-pubsub.mmd`](diagrams/src/a2a-pubsub.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

### Agent-to-agent (A2A) communication

On top of the shared `CaseState`, agents communicate **explicitly** via a typed message bus
([`backend/app/agents/messaging.py`](../backend/app/agents/messaging.py)). Every worker publishes an
**`AgentMessage`** (`sender`, `recipient`, `intent`, `payload`, `seq`) describing its handoff onto an
in-process, ordered **`MessageBus`**; the bus history is the auditable agent-to-agent conversation
(streamed as `agent_message` SSE events, written to the hash-chained audit trail, and returned on the
final payload as `messages`).

Each agent declares a **`COMMS`** interface — the intents it may **publish** and **subscribe** to —
which is least-privilege for messaging (sibling to the tool allow-list): `enforce_comms()` raises if an
agent emits an intent it didn't declare. The conversation for one case:

```
supervisor ──case.opened──▶ intake ──symptoms.normalised──▶ classifier
classifier ──acuity.classified──▶ (broadcast: safety, routing, hitl)
safety     ──safety.override──▶ (broadcast: routing, hitl, reflection)
routing    ──care.routed──▶ hitl, reflection
hitl       ──review.decision──▶ reflection
reflection ──decision.reviewed──▶ supervisor
```

The bus is in-process and synchronous **by design** — the safety-gated pipeline *order* is unchanged and
fully deterministic; a production system swaps `MessageBus` for a broker (NATS/Redis/Kafka) without
touching any agent's `COMMS` or `emit()`. Test it with `pytest -m comms`.

#### The receive side — agents act on what they are told

Publishing alone would be a broadcast, not a conversation. Before it calls any worker's `run()`, the
orchestrator hands that worker its **filtered inbox** (`PipelineOrchestrator._deliver` → `MessageBus.inbox`), so an
agent can reason about what its peers **asserted** rather than only about the `CaseState` they happened
to leave behind:

| Agent | Receives |
|---|---|
| `intake` | `case.opened` |
| `classifier` | `symptoms.normalised` |
| `safety` | `acuity.classified` |
| `routing` | `acuity.classified`, `safety.override` |
| `hitl` | `acuity.classified`, `safety.override`, `care.routed` |
| `handoff` | `safety.override`, `care.routed` *(escalated cases only)* |
| `reflection` | `safety.override`, `care.routed`, `review.decision` |

Every worker mixes in **`ConsumesMessages`**, which provides `consume(inbox)` and
`received_payload(intent)`. Reading is least-privileged in the same way publishing is —
`require_subscription()` raises if an agent reads an intent it never declared in `COMMS.subscribes`.

**Why this is load-bearing and not decoration.** `CaseState` holds only the *latest* value of a field;
the bus holds what each agent *claimed at the moment it acted*. So a downstream agent that silently
rewrites an upstream decision is invisible in the state and visible in the conversation.
`ReflectionAgent.verify_announcements()` is exactly that check: it flags a `care_tier` that no longer
matches what Care-Routing announced, and a Care-Routing that never announced at all. Withhold the
message and Reflection raises an issue it could not otherwise see — proven by
`test_reflection_detects_a_tier_changed_without_being_announced`. Delete the bus and this check cannot
exist.

Like every Reflection check it is **monotone** — it can only add issues, never clear them — and it is
skipped entirely when `consume()` was never called, so a standalone `run()` in a unit test behaves
exactly as before.

**If you own an agent:** the default `consume()` records your inbox and acts on nothing, which is
correct only for `intake` (nothing precedes it). For every other agent, override `consume()` or call
`received_payload()` inside `run()` so your decision genuinely depends on what your peers sent. Your
individual report needs this for §2 *"Communication and coordination logic"*. Verify with
`pytest -m comms`.

#### See it for yourself

```bash
git fetch origin
git checkout feature/<your>-agent          # your own branch
cd backend && . .venv/Scripts/activate     # Windows; use bin/activate on macOS/Linux

python scripts/show_a2a.py                 # watch the agents talk, for one case
python scripts/show_a2a.py "sore throat for two days"

python scripts/show_clarification_handshake.py   # the HITL ask/resume clarification handshake
python scripts/smoke_test_handoff_llm.py         # Clinician-Handoff against a real LLM
python scripts/try_agent.py                      # run a single agent interactively
```

`show_a2a.py` prints what each agent **received** from its peers, the ordered conversation on the bus,
and the Reflection critic's verdict.

All seven agents are implemented on every active branch, so this prints a full conversation — it is
the fastest check that an agent is genuinely participating and not just mutating `CaseState`.

The four checks that matter while you work — each is a CI gate:

```bash
pytest -m <your-agent>   # intake | classifier | safety | routing | hitl | handoff
pytest -m comms          # agent-to-agent messaging (bus, COMMS, consume/emit)
pytest -m capability     # are you an AGENT, or still a POLICY NODE?
pytest -m contract       # did you write to a CaseState field outside your lane?
```

### Agent ownership & isolated development

Each worker agent is **one `.py` file with one owner**, so five people can develop five agents in
parallel without stepping on each other. The agent's satellite modules (its real rules / model /
retrieval) are owned together with it.

| Owner | Agent file | Satellite modules it also owns |
|-------|-----------|--------------------------------|
| **Sham Goh** | [`app/agents/intake.py`](../backend/app/agents/intake.py) | — (self-contained) |
| **Koh Guan Chin James** | [`app/agents/classifier.py`](../backend/app/agents/classifier.py) | [`app/ml/`](../backend/app/ml/), [`app/rag.py`](../backend/app/rag.py), [`app/rag_onyx.py`](../backend/app/rag_onyx.py) |
| **Aaron Liew** | [`app/agents/safety.py`](../backend/app/agents/safety.py) | [`app/redflags.py`](../backend/app/redflags.py), [`app/safety_nlp/`](../backend/app/safety_nlp/) |
| **Marcus Teh** | [`app/agents/routing.py`](../backend/app/agents/routing.py) | clinic/tier tables (in-file), [`app/services/onemap.py`](../backend/app/services/onemap.py) |
| **Heriz Yusoff** | [`app/agents/hitl.py`](../backend/app/agents/hitl.py), [`app/agents/handoff.py`](../backend/app/agents/handoff.py) | — (escalation record is platform/`store.py`) |
| **Sham Goh** | [`app/agents/orchestration.py`](../backend/app/agents/orchestration.py) | the `PipelineOrchestrator` machinery, mixed into intake |
| **Platform (James)** | `reflection.py`, `base.py`, `capability.py`, `messaging.py` | `llm.py`, `models.py`, `store.py`, `main.py`, guardrail/redact/metrics/audit |

**How isolation is enforced (not just by convention).** Every agent declares an **`AgentContract`**
(`base.py`) — the set of `CaseState` fields it may **write** and the result keys it must **return**.
The shared harness [`tests/agents/harness.py`](../backend/tests/agents/harness.py) snapshots the
state, runs the agent, and **fails the build if an agent mutates a field outside its lane** or drops
a required return key. So if Care-Routing accidentally writes `acuity_code` (the Classifier/Safety
lane), `pytest -m contract` breaks — you find out immediately, not in a teammate's demo.

#### Developing & testing your own agent

Edit **only your agent file + your satellite modules + your test file**, then run *only your agent's*
tests for a fast inner loop:

```bash
cd backend
pytest -m intake        # Sham   — Symptom-Intake
pytest -m classifier    # James  — Severity-Classifier
pytest -m safety        # Aaron  — Safety-Override (+ redflags.py, safety_nlp/)
pytest -m routing       # Marcus — Care-Routing
pytest -m hitl          # Heriz  — Human-in-the-Loop
pytest -m handoff       # Heriz  — Clinician-Handoff
pytest -m reflection    # platform — Reflection / Critic

pytest -m contract      # boundary guard: did anyone write outside their lane?
pytest -m comms         # A2A guard: does every agent only publish declared intents?
pytest tests/agents     # everyone's isolated agent tests
pytest                  # FULL suite (integration + gates) — run before you push
```

Each member's tests live in [`tests/agents/test_<agent>.py`](../backend/tests/agents/) and carry a
matching `pytest` marker. The existing `tests/test_pipeline.py`, `tests/test_agents.py`, etc. remain
the **integration layer** (the whole orchestrated pipeline) and are unchanged.

#### Branches — where your work lives

```
main                                     last known-good release
uat                                      integration branch — everything lands here first
integration                              cross-agent integration
integration-all-agents-2026-08-31        all-agents integration line (current)
  ├── feature/symptom-intake-agent       Sham
  ├── feature/severity-classifier-agent  James
  ├── feature/safety-override-agent      Aaron
  ├── feature/care-routing-agent         Marcus
  └── feature/human-in-the-loop-agent    Heriz
```

Work on **your** branch, then merge into the integration line — not into `main`. `main` moves only
once integration is green.

Every agent must keep: `SLUG`, `TOOL_ALLOWLIST`, `AUTONOMY_LEVEL`, `PROMPT_PATTERN`, the `CONTRACT`
lane, the `COMMS` interface, `emit()`, and the `CAPABILITY` declaration — `pytest -m capability`
fails the build without it. `safety.py` is the worked example for layering a reasoning step over a
deterministic floor.

### Agentic loop engineering

The Reflection agent is a worked example of **loop engineering** (the 2026 practice of designing the
control loop — *trigger → goal → verifier → stop rules* — rather than prompting turn-by-turn). It is an
explicit **bounded Evaluator-Optimizer loop** with:
- **Trigger** — the verifier reports thin evidence / very low confidence;
- **Goal** — an internally-consistent, appropriately-cautious decision;
- **Verifier** — deterministic consistency + confidence checks (the reliable part is the *check*, not the model);
- **Stop rules** — iteration cap (`CAREROUTE_REFLECTION_MAX_ITERS`, default 1) + wall-clock budget
  (`REFLECTION_BUDGET_MS`); each pass can only **escalate**, so the loop is monotone-safe at any cap.

Every triage result carries the loop's telemetry (`iterations`, `stopReason`, …). The same pattern
recurs in the MLOps **monitor → retrain** loop (deterministic CI gates are its verifiers).

---

## The LLM provider chain

Configured in [`backend/app/llm.py`](../backend/app/llm.py) + [`config.py`](../backend/app/config.py),
tried in order until one succeeds; if all fail, agents use deterministic logic:

1. **OpenAI API** — Chat Completions REST; active only when `OPENAI_API_KEY` is set.
   The GP router uses strict Structured Outputs and then independently checks the returned clinic ID,
   confidence and explanation before accepting it. (A local CLI provider and an Ollama provider
   existed for development and were removed on 2026-09-23; neither exists in the deployed service.)
2. **Deterministic rules** — the always-available fallback.

Prompt/response **content is never logged** (healthcare data) — only provider names and outcomes.
Secrets are read from the environment only.

---

## The ML severity model, explainability & fairness

Implemented in [`backend/app/ml/`](../backend/app/ml/) — this is a **real** scikit-learn model, not a mock.

- **Model** — a `RandomForestClassifier` trained (once, seeded, cached) on a synthetic dataset with a
  realistic, *mitigable* bias: the **65+ band is under-represented** and follows a clinically
  realistic age-adjusted acuity rule. The served model oversamples under-represented bands to parity
  so it can learn that rule.
- **Explainability (SHAP)** — `shap.TreeExplainer` produces signed, urgency-oriented feature
  contributions over the symptoms the patient actually reported; surfaced to both patient and clinician.
- **Fairness audit** (`GET /api/fairness`) computes, on held-out data:
  - **Accuracy gap** before (age-blind baseline) → after (age-aware + rebalanced) mitigation
  - **Demographic Parity** (Statistical Parity Difference) and **Equal Opportunity** (TPR gap) — the
    two named classification-fairness metrics from the XRAI course
  - **Counterfactual fairness** — flips the protected attribute (sex) and reports the acuity flip rate
    (should be ~0; sex must not drive triage)
  - **Red-flag recall** on the safety-critical severe classes
- **Drift** — Population Stability Index (data / target / concept) against a shifted "production" sample.
  Data drift is the **maximum** per-feature PSI (plus the share of features over 0.25), with binary
  features scored as proportions — averaging across 27 binary flags had hidden every shift, and a
  missing drift metric is now a loud gate failure rather than "no drift".
- **Model coverage** — `zeroCoverageRate`: the share of cases lighting up **no symptom feature at all**,
  i.e. inputs the model has no signal for. Reported beside drift on every monitoring backend and computed
  from feature vectors the inference log already stores (no schema change; runs over historical logs).
  It answers a question drift cannot — not *"has the input distribution moved?"* but *"is the model blind
  to any of it?"* Added after evaluation E5 found the model had **no minor-trauma features at all** (19
  categories, all medical), so bruises, cuts and sprains scored an identical 0.448 confidence and were
  silently escalated to clinicians. Deliberately **reported, never gated**: gating on it would have made
  that over-escalation look intentional and hidden the defect.
- **MLOps** — each training run persists a **versioned model artifact** (`joblib`) and, if MLflow is
  installed, logs params/metrics and registers the model (`CareRouteTriageRF`). Inference latency is
  exposed as a Prometheus histogram.

See the **[Model Card](../backend/MODEL_CARD.md)** for intended use, data, metrics, and limitations.

---

## Security & safety controls

Mapped to the **OWASP LLM Top 10** and **Agentic (ASI)** risks — see
[`backend/SECURITY.md`](../backend/SECURITY.md) for the full register.

The **input guardrail is deterministic defence-in-depth** (no LLM dependency), not a single regex — six
layers, so an injection that evades one is caught by the next (`guardrail.py`):
**(1)** hygiene (empty/oversized) · **(2)** normalization (NFKC homoglyph fold, strip zero-width/RTL
controls, collapse whitespace, leetspeak variant) · **(3)** decode-and-rescan (base64 / hex / URL
payloads decoded, then re-screened) · **(4)** injection/jailbreak denylist · **(5)** structural
detection (`<|system|>`, `[INST]`, ```code fences```, role JSON) · **(6)** topical scoping — a positive
clinical-relevance signal (`assess_clinical_relevance()`) that rejects clearly off-scope requests with
zero clinical vocabulary (conservative, so real symptoms are never blocked).

| Control | Where | Risk addressed |
|---------|-------|----------------|
| **Input guardrail** (6-layer defence-in-depth, pre-agent) | `guardrail.screen()` | LLM01 prompt injection / jailbreak |
| **Output guardrail** (screens the rationale) | `guardrail.screen_output()` | LLM05 improper output handling / prompt-leak |
| **PII/PHI redaction** (NRIC, phone, email, MRN) | `redact.py` | LLM02 sensitive-information disclosure |
| **Least-privilege tool allow-lists** | `enforce_tool_access()` | LLM08 excessive agency |
| **Deterministic red-flag override** | `redflags.py` | Clinical-safety defence-in-depth (un-overridable) |
| **Rate limiting** (per-IP fixed window) | `ratelimit.py` | LLM10 unbounded consumption / DoS |
| **Kill switch** (`CAREROUTE_KILL_SWITCH`) | `config.py` + `llm.py` | ASI10 rogue agents — forces deterministic-only mode |
| **Tamper-evident audit log** (SHA-256 hash chain) | `audit.py` | ASI10 — detects any altered/removed entry |
| **Red-team + AppSec scanners** | `.gitlab-ci.yml`, `security/` | Continuous LLM & code security testing (see below) |

---

## Frontend surfaces

Next.js App Router + CSS Modules (`frontend/`). The Next.js server proxies `/api` →
`http://localhost:8000`. `BACKEND_URL` is read at **build time** (`next.config.mjs` rewrites are
baked by `next build`), which is why the Docker image takes it as a build `ARG`. **If the backend is
unreachable** (the fetch itself fails, nothing delivered) the UI falls back to an in-browser
simulation, clearly labelled, so it is always demonstrable; an HTTP error (429 rate limit, 422, 5xx),
a mid-stream drop or a guardrail block is shown **as an error**, never as a fabricated result.
The backend emits an SSE keepalive comment every 10 s while an agent is quiet, because Next's `/api`
rewrite aborts an upstream that is silent for 30 s without ending the browser response.

| Route | Surface | Shows |
|-------|---------|-------|
| `/` | **Patient triage** | Symptom + demographics intake → live pipeline → cited recommendation (acuity, tier, confidence, SHAP explanation) |
| `/staff/login` | **Staff login** | Simple mock login gating the staff portal |
| `/staff` → pipeline | **Agent pipeline** | The orchestrator activating each worker over live SSE + an event-log console |
| `/staff` → clinician | **Clinician HITL** | Escalation queue, case review (rationale/evidence/citations/SHAP), record-final-decision |
| `/staff` → governance | **Fairness audit** | Per-subgroup accuracy, fairness gap, parity/equal-opportunity, drift monitors |

### Built for public use (patient surface)
The patient page is designed for a non-technical member of the public, with safety and trust first:
- **Always-visible emergency banner** — a persistent "call **995** now" strip with a `tel:` link; the tool never gets between someone and emergency help.
- **Plain-language "What to do now"** — an actionable next step per acuity (call 995 / go to A&E / see a GP today / self-care), not just a care-tier label.
- **AI-use & privacy disclosure** — states clearly that it's AI-assisted (not a diagnosis), that a clinician reviews urgent cases, and that **personal identifiers are masked** before processing; confirmed inline when redaction fires.
- **Feedback / redress** — a "Was this helpful?" control wired to `POST /api/cases/{id}/feedback` (PDPC Stakeholder-Interaction pillar).
- **Print-friendly summary** — patients can print the recommendation to bring to a clinic.
- **Accessibility** — `aria-live` result region, labelled controls, visible focus states, semantic roles.
- **Offline-resilient** — if the backend is unreachable the UI falls back to a faithful in-browser simulation, so it is always usable/demonstrable.

---

## API reference

| Method & path | Purpose |
|---------------|---------|
| `GET  /api/health` | Liveness + active LLM provider, kill-switch, rate limit, metrics status |
| `POST /api/triage/stream` | **SSE** triage pipeline (rate-limited; each request runs on its own worker set). Body: `{text, language?, isVoice?, ageBand?, sex?, sessionId?, latitude?, longitude?, transportMode?, maxTravelTimeMin?, preferredClinicId?, accessibilityNeed?, preferredLanguage?, affordabilityPreference?}`. The `final` event carries the Care-Routing fields: `clinic`, `clinicLatitude/Longitude`, `travelEstimateSource`, `routeAvailable`, `routeInstructions`, `routeGeometry`, `transportMode`, `requestedTransportMode`, `routingReason`, `routingClarification`, `routingPlan`, `alternativeClinics`, `oneMapUrl` |
| `GET  /api/escalations` | Pending/decided clinician escalations |
| `GET  /api/escalations/{id}` | One escalation's full detail |
| `POST /api/escalations/{id}/decision` | Clinician records a final decision |
| `GET  /api/fairness` | Real fairness + drift audit from the trained model |
| `GET  /api/cases/{id}/audit` | Ordered, **hash-chain-verified** audit trail for a case |
| `POST /api/cases/{id}/feedback` | Patient rates/challenges a decision (into the audit trail) |
| `GET  /api/sessions/{id}/history` | Episodic recall of a session's prior cases |
| `GET  /metrics` | Prometheus exposition — operational counters (request/guardrail/escalation/rate-limit) + inference latency **and model-level metrics** (prediction-class mix, confidence histogram, model-version info, HITL agreement) |

---

## Configuration

All via environment (12-factor). For local development, put them in the repository-root `.env` file
(which is gitignored); deployments should supply normal environment variables.

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_PROVIDER_ORDER` | `openai` | Provider try-order (comma-separated) |
| `OPENAI_API_KEY` | *(unset)* | Enables the OpenAI provider (read from env only, never logged) |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model |
| `CAREROUTE_RATE_LIMIT_PER_MIN` | `30` | Triage requests per client per minute (0 disables) |
| `CAREROUTE_KILL_SWITCH` | `0` | `1` forces deterministic-only mode (no LLM) |
| `CAREROUTE_MODEL_DIR` | `models` | Where trained model artifacts are persisted |
| `CAREROUTE_REFLECTION_MAX_ITERS` | `1` | Reflection-loop iteration cap (monotone-safe to raise) |
| `ONYX_BASE_URL` / `ONYX_API_KEY` | *(unset)* | Route RAG retrieval to a self-hosted **Onyx** (Danswer) instance; falls back to in-process TF-IDF when unset |
| `ONEMAP_EMAIL` / `ONEMAP_PASSWORD` | *(unset)* | Free OneMap account for live travel estimates + route geometry; unset → explicitly labelled local estimate |
| `CAREROUTE_SSE_HEARTBEAT_SECONDS` | `10` | Keepalive comment cadence while the pipeline is silent (keep well under Next's 30 s proxy idle timeout; `0` disables) |
| `CAREROUTE_TRUSTED_PROXIES` | *(empty)* | IPs/CIDRs whose `X-Forwarded-For` the rate limiter trusts; empty = trust nobody |
| `CAREROUTE_CORS_ORIGINS` | the two local dev servers | Comma-separated browser origins allowed to call the API |
| `CAREROUTE_DEBUG_LLM` | `0` | `1` re-enables model-response excerpts in provider error messages (never on in production — healthcare data) |

### Use OpenAI for routing

Create an API key in the OpenAI Platform, then put it in the backend environment or its ignored `.env`
file; do not send it from the browser or commit it. To enable the OpenAI provider:

```dotenv
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-4o-mini
LLM_PROVIDER_ORDER=openai
```

The frontend/API input remains the normal triage request (`text` plus optional location and travel
preferences); the API key is server-only. Input is screened and PII-redacted before any provider call.
Router output is schema-constrained, allow-list validated against verified clinics, and output-screened;
an invalid, refused, or unavailable model response uses the deterministic routing fallback.

**Retrieval (RAG) & the Onyx upgrade path.** By default the Severity-Classifier grounds its citations with
an in-process TF-IDF retriever ([`app/rag.py`](../backend/app/rag.py)). Set `ONYX_BASE_URL` + `ONYX_API_KEY`
to route retrieval to a self-hosted **Onyx Community Edition** (free, MIT — real embeddings + hybrid
search + connectors), a **drop-in** with the same contract that **falls back to TF-IDF** on any error
([`app/rag_onyx.py`](../backend/app/rag_onyx.py); tests in `tests/test_rag_onyx.py`).

---

## Running it

**Prerequisites:** Node ≥ 18, Python ≥ 3.10. Optionally an `OPENAI_API_KEY` — it is not
required (deterministic fallback).

**Requirements files:** `requirements.txt` (runtime) · `requirements-dev.txt` (tests/lint) ·
`requirements-mlops.txt` (MLflow/DVC/Evidently, for the five pillars) · `requirements-safety-nlp.txt`
(optional models for the Safety-NLP semantic layer — without it that layer degrades to the
deterministic regex floor, which is a supported configuration).

> [!warning] **If you cloned into OneDrive** (as this repo's path is), do **not** create the venv
> inside the repo. OneDrive path lengths blow past Windows' `MAX_PATH` and pip installs fail with
> confusing errors partway through. Put it outside, e.g. `python -m venv C:\venvs\careroute`, and
> point your tooling at that interpreter. `dev.sh` still auto-detects a `backend/.venv` if you have
> one on a non-OneDrive checkout.

### Both, one command — recommended for local dev (hot-reload)
```bash
# one-time backend setup so ./dev.sh auto-detects the venv:
cd backend && python -m venv .venv && . .venv/Scripts/activate     # macOS/Linux: . .venv/bin/activate
pip install -r requirements.txt && cd ..

./dev.sh          # starts backend (:8000, --reload) + frontend (:5173, hot-reload); Ctrl+C stops both
```
`dev.sh` runs both natively with live-reload on each — no image rebuilds. It auto-detects
`backend/.venv` and runs `npm install` on first frontend start. Open **http://localhost:5173**.

### Backend only (port 8000)
```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate     # macOS/Linux: . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```
Health check: `curl http://localhost:8000/api/health`

### Frontend only (port 5173)
```bash
cd frontend
npm install
npm run dev            # open http://localhost:5173
```

### Docker (both + Prometheus/Grafana/Alertmanager, production-like images)
```bash
docker compose up --build    # from the repo root (this app/ directory) — open http://localhost:8080
```

Brings up five services: `backend`, `frontend`, `prometheus` (:9090), **`alertmanager` (:9093)** and
`grafana` (:3000).

---

## Testing

```bash
cd backend
. .venv/Scripts/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                                    # all tests as CI sees them (~15 min); see note below
pytest -q --cov=app --cov-report=term        # with coverage (85%; CI floor 80%)
pytest -v tests/test_pipeline.py             # just the end-to-end agent-pipeline test
```

End-to-end browser tests live separately, under `frontend/` — see
[Frontend end-to-end tests](#frontend-end-to-end-tests-playwright) below.

The suite forces the deterministic path (no network/LLM — see `conftest.py`), so it is reproducible and
directly exercises the **"runs without a model / without an LLM"** guarantee. It is JUnit-reported and
gated in CI behind an **80% coverage floor** (currently **85%**, `test:backend`).

> [!note] The suite's session-wide kill switch (`backend/conftest.py`) keeps every LLM call on the
> deterministic path, so a local run behaves exactly as CI does even with a provider configured.

**Full test inventory — 796 tests across 58 files** (783 passing, 13 skipped; the skips are the opt-in
markers that need `RUN_SAFETY_NLP_ARTIFACT_TESTS=1`, `RUN_LIVE_ROUTING_TESTS=1` or a reachable LLM).
Last measured 2026-09-11 on `integration-all-agents-2026-08-31`. The table below covers the principal
files; the per-agent, services and tools subdirectories add the remainder.

| Test file | # | What it verifies | CI job (gate) |
|-----------|---|------------------|---------------|
| `tests/agents/` (9 files) | 81 | **Per-agent isolation + A2A + capability declarations** (one test file per owner + platform guards): each worker's behaviour tested in isolation via `pytest -m <agent>`; `test_contracts.py` runs **every** agent through the `AgentContract` boundary check (fails if an agent writes a `CaseState` field outside its lane); `test_comms.py` verifies the message bus, pub/sub delivery, and that every agent only publishes intents it declared in its `COMMS` interface (`enforce_comms`); **`test_capability.py` (36)** validates every worker's `AgentCapability` against its implementation — you cannot call yourself an agent without a model call and a real action space, a policy node must carry an `upgrade_path`, and **exactly one** worker may set `uses_trained_model` (`pytest -m capability`) | `test:backend` |
| `test_eval_tool_access.py` | 35 | **Evaluation E6 — tool-access enforcement.** The full (agent, tool) permission matrix from `tests/fixtures/tool_access_cases.json`: every permitted pair must execute and every forbidden pair must raise. Block rate must be **1.0** (OWASP LLM06/LLM08, FR-12). Reframed from "tool-selection accuracy", which is not measurable here — no agent selects a tool at runtime | `test:backend` (`pytest -m eval`) |
| `test_eval_plan.py` | 23 | **The evaluation plan itself is code.** One `EvalSpec` per evaluation in `app/evals/plan.py`; the build fails if any spec is missing a **test dataset, expected outputs, metrics or acceptance criteria**, if a spec claims `implemented` without a real test file, or if a `planned` spec states no blocker. Stops the plan and the code drifting apart | `test:backend` (`pytest -m eval`) |
| `test_eval_hitl.py` | 5 | **Evaluation E5 — HITL trigger accuracy**, driven end-to-end through the real pipeline with the LLM disabled: red-flag **recall = 1.0** *and* **specificity ≥ 0.80** on must-NOT-escalate cases, every escalation attributable to a rule, and a guard that the dataset keeps both classes. The specificity half is what caught the minor-trauma model blind spot | `test:backend` (`pytest -m eval`) |
| `test_guardrail.py` | 28 | The **6-layer input guardrail**: clean input passes; 9 injection/jailbreak payloads blocked; empty + oversized caps; **base64**, **leetspeak**, **zero-width**, and **full-width homoglyph** obfuscated injections blocked (normalization/decode layers); 5 **structural** payloads (`<\|system\|>`, `[INST]`, code-fence, role-JSON); 4 **off-scope** requests blocked by topical scoping; a **false-positive guard** (clinical text with an off-scope word still passes); clinical-relevance scoring | `test:backend`, `ai-security:guardrail-regression` (**blocking**) |
| `test_ml_entrypoints.py` | 18 | The **MLOps CLI entrypoints**: `export_dataset` write+verify+main; `validate_data` pass / missing-snapshot / **tampered-hash** fail; `train` builds a gated artifact + rejects low recall + rejects a widened fairness gap; `monitor` writes a report + the **drift gate** + retrain-trigger **skip** and **loop-guard** + PSI fallback + **live-traffic** report (3 cases); inference-log round-trip + missing-file | `test:backend` (mirrors `data:validate`, `test:model-gate`, `monitor:evidently`) |
| `test_new_features.py` | 12 | **Responsible-AI + security features**: demographic-parity spread; equal-opportunity (TPR) gap on severe classes; counterfactual **sex-stability**; **PII redaction** (masks identifiers, keeps symptoms, leaves clean text); **rate limiter** over-limit + zero-disables; **output guardrail** flags a prompt-leak / passes clean; **audit hash-chain** tamper detection; **Reflection** forces escalation of an un-escalated high-acuity case / passes a consistent low-acuity case | `test:backend` |
| `test_api_e2e.py` | 14 | **End-to-end FastAPI** (over httpx): health; fairness-response shape; escalations list; **chest-pain → P1 escalation** via SSE; case audit available + unknown-case 404; **prompt-injection blocked before agents run**; escalation-decision status flip + unknown-id 404 | `test:backend` |
| `test_report.py` | 8 | **Executive report**: Markdown has all sections; renders the scan-results table; scan-count parsing; empty-when-no-artifacts; **`_notable_findings` lists actual findings**; PDF is written; uses the drift file when present; `main()` writes both PDF **and** Markdown | `test:backend` |
| `test_redflags.py` | 7 | **Deterministic safety override**: chest pain → P1; stroke signs → P1; suicidal ideation escalates; no red flag → not triggered; override can **raise**, **never lowers**, and is a no-op when nothing fired | `test:backend` |
| `test_ml.py` | 13 | **The real ML model**: feature extraction; dataset subgroups + valid labels; severe prediction with **real SHAP**; low confidence on vague input; fairness audit is real + **mitigation does not worsen the gap**; PSI = 0 on identical distributions; plus **model-coverage telemetry** (`has_no_feature_coverage` / `zero_coverage_rate`) including a **regression guard on the three minor-trauma cases** that evaluation E5 exposed | `test:backend` |
| `test_pipeline.py` | 7 | **End-to-end agent pipeline** (deterministic): workers activate in **safety-gated order**; the **ordered A2A conversation** is recorded (`case.opened`→…→`decision.reviewed`); red-flag forces P1 + escalates; low-confidence escalates; a clear mild case is **not** escalated; prompt-injection blocked before agents; an **ordered audit trail** is written | `test:pipeline` |
| `test_store.py` | 6 | **Store + HITL ground-truth**: `decide_escalation` records ground truth / handles no-label / unknown-id; `valid_session_id`; save + **session-scoped** episodic recall; fairness-response builder | `test:backend` |
| `test_agents.py` | 5 | **Agent resilience + least-privilege**: Symptom-Intake and Severity-Classifier **deterministic fallback**; ambiguous input → low confidence; `enforce_tool_access` **allows** a declared tool and **raises** for a disallowed one | `test:backend` |
| `test_fairness.py` | 11 | **Fairness metric helpers**: subgroup accuracies + gap; red-flag recall (perfect + missed); demographic parity + equal opportunity; PSI zero/positive; data/target/concept drift | `test:backend` |
| `test_rag_onyx.py` | 4 | **RAG backend**: Onyx disabled by default; retrieve **falls back to TF-IDF**; Onyx used when configured; network error falls back | `test:backend` |
| `test_triage_eval.py` | 3 | **Offline triage-eval benchmark** (gold vignette set): acuity accuracy above floor; **red-flag recall = 1.0** on must-escalate cases; every decision is **citation-grounded** | `test:triage-eval` |
| `test_model_gate.py` | 4 | **Hard release gate on the DEPLOYED artifact**: a persisted model must actually have been *loaded* (a silently fresh-trained one fails the gate); it meets **accuracy ≥ 0.75, red-flag recall ≥ 0.95, calibration ECE ≤ 0.05 and fairness gap ≤ 0.35**; carries integrity (SHA-256) metadata | `test:model-gate` (**blocking**) |
| `test_fairness_gate.py` | 1 | **Responsible-AI gate**: subgroup accuracy gap within the same 0.35 ceiling the release gate enforces (one shared constant) — fails on an absolute regression, not only relative to the age-blind baseline | `ai-security:fairness-gate` (**blocking**) |

> Also present: `test_robustness.py` — an **IBM ART** black-box evasion attack (HopSkipJump) against the RandomForest, asserting resistance to small (≤ ε) perturbations. It is `importorskip`-guarded, so it runs only when `adversarial-robustness-toolbox` is installed (CI job `test:robustness`, advisory).

**Blocking test gates** (a failure stops the release): `test:model-gate`, `test:data-lineage`, `data:validate`, `test:pipeline`, `test:e2e` (blocking since 2026-09-02), `ai-security:guardrail-regression`, `ai-security:fairness-gate`, and the `test:backend` **coverage floor**. `test:triage-eval` and `test:robustness` are advisory.

### Frontend end-to-end tests (Playwright)

```bash
cd frontend
npm install               # not optional — see the note below
npm run test:e2e          # the BACKEND must already be running on :8000
```

Three spec files, **20 specs, all passing** (~3 min — it builds and starts a production frontend
itself; only the backend must be up):

> [!note] If `npm run test:e2e` fails with **`error: unknown command 'test'`**, `@playwright/test` is
> missing from `frontend/node_modules` and the bare `playwright` on your `PATH` has resolved to the
> **Python** Playwright CLI instead (e.g. `…/Python312/Scripts/playwright`), which has no `test`
> subcommand. `npm install` in `frontend/` fixes it. Note that npm ≥ 11 may then rewrite
> `package-lock.json` to strip `libc` fields from optional Linux binaries — don't commit that churn;
> CI's Linux runner needs those constraints.

- `agents.spec.js` — drives the real UI against the real agent pipeline (red-flag escalation, benign case,
  every worker reporting, grounded citations) plus two direct `:8000` contract tests.
- `routing_ui.spec.js` — the Care-Routing result panel (route, replan banner, alternative clinics) with a
  **mocked** stream replayed from a real run, so it is deterministic in CI without OneMap credentials.
- `failure_paths.spec.js` — HTTP errors surfaced instead of simulated, a mid-stream drop keeps the partial
  real run, guardrail `error` events rendered, routing explanations, and a clinician decision that fails
  to save never shows as recorded.

**Why these tests look paranoid.** `lib/api.js` degrades to an **in-browser simulation** when the backend
is unreachable, rendering a complete and plausible triage result from hardcoded keyword rules. A
conventional end-to-end test — "submit symptoms, assert a recommendation appears" — would therefore **pass
with the entire agent pipeline switched off**. Every real-pipeline test additionally asserts that the
"backend offline" banner is absent and that the case id is not a client-generated `SIM-…` id.

**History, so nobody re-investigates it.** Until 2026-09-02 the three red-flag specs failed with the UI
stuck on "Triaging…". Root cause, measured from inside Chrome: Next's `/api` rewrite proxies through
`http-proxy` with a 30 s idle `proxyTimeout` that aborts a silent upstream **without ending the browser
response**; the red-flag path is quiet ~30 s on the LLM and Care-Routing 24–40 s on OneMap. Fixed with a
backend SSE keepalive (`CAREROUTE_SSE_HEARTBEAT_SECONDS`), after which the suite went green and the CI job
was made blocking.

Browser selection is environment-aware. Locally the config drives the **already-installed Google Chrome**
(`channel: 'chrome'`), because `npx playwright install` fails behind a TLS-intercepting proxy with
`SELF_SIGNED_CERT_IN_CHAIN` — and the fix for that is emphatically not to disable certificate
verification while downloading an executable. In CI, `PLAYWRIGHT_CHANNEL=bundled` selects the browsers
baked into the `mcr.microsoft.com/playwright` image, so the runner downloads nothing either.

---

## CI/CD — the MLSecOps pipeline

[`.gitlab-ci.yml`](../.gitlab-ci.yml) runs a **10-stage**, **all free/open-source** MLOps + security pipeline
(`lint → train → test → monitor → ai-security → security-scan → report → build → deploy → post-deploy`).
Details and local run commands are in [`security/README.md`](../security/README.md). Two views follow:
**(a)** what each *stage* does, and **(b)** a full table of **every one of the 42 security & AI
scanners and gates** (tool, what it checks, blocking vs advisory, and its artifact) — plus the two Locust
load-test jobs and the agent-graph lint, listed in the same table so the runtime and structural evidence
sits next to the scanners that produce the rest of it.

### What each stage does (MLOps lens)

The pipeline runs left→right; a failure in a **blocking** job stops the release before build/deploy.

| Stage | Jobs | MLOps / MLSecOps function |
|-------|------|---------------------------|
| **lint** | `lint:backend` (ruff), `lint:frontend` (eslint) | Static hygiene — **ruff is blocking** (a gate that can't fail is decoration); eslint is advisory but no longer silenced. Ruff is **pinned (`0.16.3`)** with its rule set declared in [`backend/ruff.toml`](../backend/ruff.toml), so the gate cannot go red on someone else's release schedule |
| **train** | `train:model`, `data:version`, `publish:model` | **Train/serve split** — the *one* place a model is built: seeded train → **training release gate** (acc ≥ 0.75, **red-flag recall ≥ 0.95** — the SAME floors as `test:model-gate`, so a failing model is never persisted or registered) → content-addressed `joblib` + SHA-256 → `model_audit.json` → **MLflow-registers** `CareRouteTriageRF`. `data:version` snapshots + hashes the dataset (**Pillar 2**, DVC-tracked via the committed `.dvc` pointer); `publish:model` pushes the release to GitLab's **Package Registry** (durable store). Also runs on **scheduled pipelines** (Continuous Training). |
| **test** | `test:backend` (+ coverage gate), `test:pipeline`, **`data:validate`**, **`test:model-gate`**, **`test:data-lineage`**, `test:triage-eval`, `test:robustness`, `test:e2e`, **`test:load-locust`**, **`test:api-fuzz-schemathesis`**, **`test:agent-graph`** | Unit + e2e (with a **coverage floor**) + the **data-validation gate** (blocking: schema/domain/label-balance/hash-consistency of the versioned dataset) + the **hard model-quality gate** (blocking: acc ≥ 0.75, **red-flag recall ≥ 0.95**) + the **data-lineage gate** (blocking: the versioned dataset's hash **==** the model's training-data hash) + offline triage-eval + IBM ART robustness + **`test:e2e`** (Playwright, advisory: browser tests against the real pipeline, run on the `mcr.microsoft.com/playwright` image so no browser is downloaded) + a **Locust load test** and **Schemathesis OpenAPI fuzzing**, both against a backend booted inside the job. |
| **monitor** | `monitor:evidently` | **Pillar 3 + 4** — an **Evidently AI** data-drift + model-performance report (synthetic shifted sample, or **real logged traffic** with `CAREROUTE_MONITOR_SOURCE=live`); degrades to a built-in PSI report. The **drift→retrain loop is closed**: a drift breach beyond `CAREROUTE_DRIFT_THRESHOLD` calls the GitLab trigger API to fire a retrain pipeline (with a loop guard so a triggered pipeline never re-triggers). Publishes an HTML dashboard + JSON. Runtime alerting is separate: Prometheus evaluates `monitoring/alert.rules.yml` and **Alertmanager** routes what fires. |
| **ai-security** | guardrail-regression, **`guardrail-score` (scored)**, **fairness-gate** (Fairlearn), Promptfoo, Garak, DeepTeam, PyRIT | Responsible-AI subgroup-parity gate + LLM red-teaming (OWASP LLM Top 10). |
| **security-scan** | **30 jobs** — **Gitleaks (blocking)**, TruffleHog, detect-secrets, GitLab Secret Detection, **no-live-credentials (blocking)** · Semgrep, Bandit, ruff-security, njsscan, ESLint-security, Horusec, SonarQube, GitLab SAST · **Trivy-fs (blocks on CRITICAL)**, deps-audit, OSV, Grype, Dependency-Check, Retire.js · Checkov, KICS, Hadolint · **modelscan**, Fickling, **SBOM + AI-BOM**, licences · ZAP-api, ZAP-full, Nikto, Nuclei | Supply-chain + AppSec + **DAST**. Secrets and CRITICAL CVEs **block the release**; every scanner also writes a machine-readable JSON **and** a human-readable `.txt` artifact. The **AI-BOM** inventories the model artifact + integrity hashes (LLM03). The four **DAST** jobs live here rather than in `post-deploy` because each boots its *own* disposable backend — they do not need a deployment, and `report` runs before `post-deploy`, so results produced there could never reach the PDF. |
| **report** | `report:pdf`, **`scan:pii-egress`** | **Executive report** — renders a management-facing **PDF + Markdown twin** (`app/ml/report.py`) covering model quality, release gates, fairness, calibration, data lineage, drift, and a **security "Findings summary"** that lists the actual scanner findings (CVE IDs, rule names, advisory titles) with a CRITICAL/blocking count. Runs after `security-scan` so it can read the scanners' artifacts. |
| **build** | `build:frontend`, `build:images`, container-image scan (Trivy), Dockle | Ships images and scans the *exact* built image. |
| **deploy** | **`deploy:push-images`**, **`deploy:ecs`**, **`deploy:shadow-model`** (env: staging), **`deploy:promote-production`**, **`rollback:production`** | Shadow deploy in a tracked **staging** `environment:` → manual **promote to production**, guarded by the **champion–challenger gate** (the candidate's audited accuracy + red-flag recall must be ≥ the current Production model's registry metrics, else the job fails) → MLflow `Production` stage transition → **`deploy:ecs`** rolls the image out on ECS and fails unless ECS ends up serving it; **rollback:** rolls ECS back onto the previous verified image tag and re-pins the previous Production model version. ECS also rolls back on its own on a 5xx-rate alarm. **Pillar 5**. |
| **post-deploy** | `dast:owasp-zap`, `loadtest:staging`, **`notify:pipeline-outcome`** | The two jobs that genuinely need a **deployed** target: ZAP's passive baseline against `$DAST_TARGET` and a Locust run against `$LOAD_TARGET`. The self-booting DAST scanners moved to `security-scan` (see above). **`notify:pipeline-outcome`** closes the feedback half of CI/CD — it runs `when: always` with `needs: []`, so it reports on a pipeline that has *already* broken (a notifier that only runs on success is decoration); failures lead the digest and an `allow_failure` job going red is reported under the verdict, not in it. |

### Every security & AI scanner (detailed)

**42 scanners and gates** across six stages, plus the **2 Locust load-test jobs** and the
**agent-graph lint** (45 rows below).
*Blocking* = a finding fails the pipeline; *advisory* = the finding is published (JSON + human-readable
output + into the report's Findings summary) but does not block. All tools are **free / open-source**.

**On the deliberate overlap.** Four secret scanners, four dependency scanners, two IaC scanners and four DAST
scanners is not an accident and not padding. Each tool carries a different rule corpus or advisory database
(OSV vs NVD vs Anchore vs GitHub advisories; Checkov's policies vs KICS's queries), so a finding one misses
another catches — and where two tools disagree about the same file, that disagreement is itself a signal worth
reading. The cost is wall-clock: the `security-scan` stage now holds 30 jobs and a cold
`scan:deps-dependency-check` alone downloads the full NVD feed. That trade was made explicitly in favour of
coverage; every scanner runs on **every push**.

| Job | Tool | Stage | What it checks | Gate | Output artifact |
|-----|------|-------|----------------|------|-----------------|
| `ai-security:guardrail-regression` | our pytest suite | ai-security | Prompt-injection is blocked **and** the deterministic safety-override is un-overridable (a subset of `test_guardrail`/`test_api_e2e`/`test_pipeline`) | **Blocking** | `security-report.xml` (JUnit) |
| `ai-security:fairness-gate` | **Fairlearn** | ai-security | Subgroup **accuracy-parity** of the deployed model — fails if it regresses to the age-blind baseline | **Blocking** | `fairness-report.xml` |
| `ai-security:promptfoo` | **Promptfoo** (OpenAI) | ai-security | LLM **red-team regression** (per push/PR) — prompt-injection, jailbreak, leakage | Advisory · needs `OPENAI_API_KEY` | `redteam-results.json`, `redteam-report.html` |
| `ai-security:garak` | **Garak** (NVIDIA) | ai-security | Full LLM **vulnerability scan** — promptinject, encoding, DAN, leakreplay, toxicity, malwaregen probes | Advisory · manual, release cadence | `garak.*.report.*` |
| `ai-security:deepteam` | **DeepTeam** (Confident AI) | ai-security | **OWASP LLM Top 10** mapped red-team attack suite | Advisory · manual | `deepteam-report.*` |
| `ai-security:pyrit` | **PyRIT** (Microsoft) | ai-security | Novel **multi-turn** adversarial orchestration (Crescendo/TAP) | Advisory · manual, quarterly | — |
| `scan:secrets-gitleaks` | **Gitleaks** | security-scan | **Committed secrets** / keys / tokens in the repo | **Blocking** | `gitleaks.json` |
| `scan:secrets-trufflehog` | **TruffleHog** | security-scan | Secrets, then **authenticates each candidate against the live provider** — a hit means "this key works", not "this looks like a key" | Advisory job; a **VERIFIED** finding is flagged **BLOCKING** in the report | `trufflehog.json`, `trufflehog.txt` |
| `scan:secrets-detect-secrets` | **detect-secrets** (Yelp) | security-scan | Secrets via a committed **baseline**, so only what is *new* since triage is reported | Advisory | `detect-secrets.json` |
| `secret_detection` | **GitLab Secret Detection** | security-scan | GitLab's native secret analyzer (**runs on the Free tier**; only the MR security widget needs Ultimate) | Advisory | `gl-secret-detection-report.json` |
| `scan:sast-semgrep` | **Semgrep** | security-scan | **SAST** (Python + JavaScript rulesets) over `backend` + `frontend` | Advisory | `semgrep.sarif`, `semgrep.txt` |
| `scan:sast-bandit` | **Bandit** | security-scan | **Python-specific SAST** (medium+ severity) | Advisory | `bandit.json`, `bandit.txt` |
| `scan:sast-ruff-security` | **Ruff** (`S` / flake8-bandit) | security-scan | The Bandit rule family over `backend/scripts` + `backend/tests` — the code `scan:sast-bandit` does **not** cover | Advisory | `ruff-security.{json,txt}` |
| `scan:sast-njsscan` | **njsscan** | security-scan | **Node/Next.js-specific SAST** (template injection, insecure crypto, framework misconfig) | Advisory | `njsscan.sarif`, `njsscan.txt` |
| `scan:sast-eslint-security` | **eslint-plugin-security** | security-scan | Frontend SAST via an **inline** config, so the project's own `npm run lint` keeps its current meaning | Advisory | `eslint.sarif`, `eslint-security.txt` |
| `scan:sast-horusec` | **Horusec** | security-scan | Multi-language SAST **aggregator** — cross-checks the single-language scanners | Advisory | `horusec.{json,txt}` |
| `scan:sast-sonarqube` | **SonarQube** | security-scan | Code quality + **security hotspots**. Named in the module notes. The one scanner that cannot be self-contained (the CLI always uploads to a server) | Advisory · needs `SONAR_TOKEN` + `SONAR_HOST_URL` (SonarCloud is free for public projects) | — (server-side) |
| `sast` | **GitLab SAST** | security-scan | GitLab's native analyzer selection (**runs on the Free tier**) | Advisory | `gl-sast-report.json` |
| `scan:trivy-fs` | **Trivy** | security-scan | Filesystem **vuln + secret + IaC-misconfig** scan | **Blocking on CRITICAL** | `trivy-fs.json`, `trivy-fs.txt` |
| `scan:deps-audit` | **pip-audit** + **Safety** + **npm audit** | security-scan | **Dependency CVEs** (Python + Node) | Advisory | `pip-audit.{json,txt}`, `npm-audit.{json,txt}` |
| `scan:deps-osv` | **OSV-Scanner** (Google) | security-scan | Dependency CVEs against **osv.dev** (GHSA + PyPA + distro advisories) — a different corpus from Trivy/pip-audit | Advisory | `osv.sarif`, `osv.txt` |
| `scan:deps-grype` | **Grype** (Anchore) | security-scan | Dependency CVEs against **Anchore's** feed — third opinion on the same dependency set | Advisory | `grype.sarif`, `grype.txt` |
| `scan:deps-dependency-check` | **OWASP Dependency-Check** | security-scan | Dependency CVEs against the **NVD**. Slow on a cold run (full NVD download); the feed is cached between pipelines. Set `NVD_API_KEY` to lift the anonymous rate limit | Advisory | `dependency-check.sarif`, `dc-report/` |
| `scan:deps-retirejs` | **Retire.js** | security-scan | Known-vulnerable **JS libraries that are bundled or vendored** — which lockfile scanners miss | Advisory | `retire.{json,txt}` |
| `scan:iac-checkov` | **Checkov** | security-scan | IaC/config policy over **Dockerfiles, `docker-compose.yml` and `.gitlab-ci.yml` itself** (no `.tf` files exist in this repo) | Advisory | `checkov.sarif`, `checkov.txt` |
| `scan:iac-kics` | **KICS** (Checkmarx) | security-scan | Same targets as Checkov with an **independent query set** | Advisory | `kics.sarif`, `results.html` |
| `scan:licenses` | **pip-licenses** + **license-checker** | security-scan | **Licence compliance** — a copyleft dependency in a clinical product is a release blocker of a different kind | Advisory | `pip-licenses.{json,txt}`, `npm-licenses.{json,txt}` |
| `scan:model-fickling` | **Fickling** (Trail of Bits) | security-scan | **Decompiles the pickle program** inside the model artifact and reports what it would execute on load — the depth complement to `modelscan`'s operator check | Advisory | `fickling.txt` |
| `scan:modelscan` | **modelscan** (ProtectAI) | security-scan | **Malicious serialized-model** code (pickle/torch/keras deserialization) in `backend/models` | Advisory | `modelscan.json`, `modelscan.txt` |
| `scan:sbom-cyclonedx` | **CycloneDX** | security-scan | **SBOM** (Python + Node) **+ AI-BOM** (model artifact + integrity hashes) — OWASP LLM03 supply-chain | Advisory | `sbom-*.cdx.json`, `ai-bom.json` |
| `scan:dockerfile-hadolint` | **Hadolint** | security-scan | **Dockerfile** best-practice / security lint (per file) | Advisory | `hadolint-backend.json`, `hadolint-frontend.json`, `hadolint.txt` |
| `scan:container-image` | **Trivy** (image) | build | **CVE + secret + misconfig** on the **exact built** backend + frontend images | Advisory | `trivy-*-image.json` |
| `scan:container-dockle` | **Dockle** | build | Container **CIS / hardening** lint (no-root, no secrets in layers, minimal caps) | Advisory | — (list output) |
| `dast:owasp-zap` | **OWASP ZAP** | post-deploy | **DAST** baseline against the **running** app | Advisory · needs `DAST_TARGET` | `zap-report.html` |
| `dast:zap-api` | **OWASP ZAP** (api-scan) | security-scan | **API-aware DAST** driven by the OpenAPI schema at `/openapi.json`, so ZAP knows the real endpoints and parameters. For an API with no crawlable HTML this is the ZAP job carrying real signal | Advisory | `zap-api.{json,html}` |
| `dast:zap-full` | **OWASP ZAP** (full-scan) | security-scan | **Active attack** scan (injection, traversal, XSS) — safe here precisely because the target is a throwaway backend booted inside the job | Advisory | `zap-full.{json,html}` |
| `dast:nikto` | **Nikto** | security-scan | **Web-server** misconfiguration (dangerous methods, leaked headers, stale files) — a server-level question, not an app-level one | Advisory | `nikto.{json,txt}` |
| `dast:nuclei` | **Nuclei** (ProjectDiscovery) | security-scan | Community **template corpus** sweep — breadth-first, complementing ZAP's depth on our endpoints | Advisory | `nuclei.json` |
| `test:api-fuzz-schemathesis` | **Schemathesis** | test | **Property-based API fuzzing** generated from our own OpenAPI contract: asserts the server never 500s and never violates its declared schema. Excludes the SSE endpoint, which streams by design | Advisory | `schemathesis.txt`, JUnit |
| `test:load-locust` | **Locust** | test | **Load test** against a backend booted in-job: p95 latency + error-rate SLOs, checked in [`locustfile.py`](../backend/tests/load/locustfile.py) so the same gate runs on a laptop | Advisory *(no baseline yet — see below)* | `locust-summary.json`, `locust_*.csv` |
| `loadtest:staging` | **Locust** | post-deploy | Same locustfile against a **real deployed** target, so the two runs are directly comparable | Advisory · needs `LOAD_TARGET` | `locust-summary-staging.json` |
| `ai-security:guardrail-score` | **E9 scorer** (own) | ai-security | **Scored** guardrail effectiveness over a 55-case labelled corpus: injection **bypass rate**, recall, and false positives. The rate form the pass/fail regression job cannot express | **Blocking** — bypass ≤ 2%, recall ≥ 0.95, false positives ≤ 0.05 | `guardrail-score.json` |
| `scan:pii-egress` | **Presidio** + `redact.py` | report | **PII egress = 0** over every published artifact (inference log, ground-truth log, drift report, the PDF's Markdown twin). Runs in `report` because it audits what has already been published | **Blocking**. Findings carry the **masked** line, never the identifier | `pii-egress.json` |
| `scan:no-live-credentials` | own guard | security-scan | **"Nothing below production spends money"** — asserts no *agent-reachable* live credential (OneMap, LLM provider, Onyx) is present outside production. CI plumbing tokens are deliberately out of scope | **Blocking** in CI; inert on a developer machine | `no-live-credentials.json` |
| `test:agent-graph` | pytest | test | **Graph lint + loop safety**: every declared worker must be reached by a real `orchestrate()` run, and one triage may not exceed **9** worker steps (measured 6–7) | **Blocking** | JUnit |

Every scanner's findings are also consolidated into the **executive report's "Findings summary"** (`report:pdf`),
which lists each individual finding (CVE IDs, rule names, advisory titles) with its severity — see the `report` stage above.

**Why the load gate is advisory (for now).** `test:load-locust` computes a real verdict — the locustfile sets a
non-zero exit code when p95 or the error rate breaches its threshold, verified in both directions — but the job
carries `allow_failure: true` because **no baseline on CI hardware exists yet**. A latency threshold guessed
before the first measurement is a flaky gate, not a gate. Once a few pipelines establish the real distribution,
promoting it is deleting that one line, exactly as `test:e2e` was promoted on 2026-09-02 once its failure mode
was understood. For reference, a first local run (Windows dev box, 10 users, cold model load) measured
**p95 ≈ 5 s** against the default 800 ms threshold — largely first-request model loading and single-worker
contention, and precisely the kind of number that has to be measured rather than assumed.

### MLOps course alignment — the module toolchain, mapped

The *Integrating & Deploying AI Solutions* module prescribes a specific MLOps toolchain (Note 02, "CI/CD
in MLOps"). CareRoute implements it end-to-end:

| # | Module tool category | Named tool | Where it lives in CareRoute |
|---|----------------------|-----------|------------------------------|
| 1 | Version Control | Git / GitLab | The repo + [`.gitlab-ci.yml`](../.gitlab-ci.yml) |
| 2 | CI/CD Orchestration | GitLab CI/CD | The 10-stage pipeline |
| 3 | Data / Feature Mgmt | Feast | ⏸ *deferred* — features computed in-process ([`ml/features.py`](../backend/app/ml/features.py)); see below |
| 4 | Experiment Tracking | **MLflow** | [`ml/train.py`](../backend/app/ml/train.py) `_mlflow_log()` — **Pillar 1** |
| 5 | Model Registry | **MLflow** | `CareRouteTriageRF`, versioned on every gated train. Destination follows `MLFLOW_TRACKING_URI`: unset → the local SQLite store; set to GitLab's MLflow-compatible registry → there. The GitLab path is configured but the default local path is what runs today |
| 6 | Pipeline Orchestration | Kubeflow | ⏸ *deferred* — the GitLab stage-DAG is the lightweight stand-in |
| 7 | Testing | **Pytest** | `backend/tests/` + the blocking CI gates |
| 8 | Containerization | **Docker** | `backend/Dockerfile`, `frontend/Dockerfile`, `docker-compose.yml` |
| 9 | Deployment (serving) | **FastAPI** | [`app/main.py`](../backend/app/main.py) |
| 10 | Monitoring | **Evidently** + **Prometheus/Grafana** | [`ml/monitor.py`](../backend/app/ml/monitor.py) (drift) + `/metrics` (Prometheus) + Grafana dashboards + **Alertmanager** routing in compose |

### The five MLOps pillars (implemented)

All five run locally, no cloud needed. From `backend/` with the venv active and
`pip install -r requirements.txt -r requirements-mlops.txt`:

1. **Experiment tracking + Model Registry** — `python -m app.ml.train` logs params/metrics and registers
   `CareRouteTriageRF`. Unset `MLFLOW_TRACKING_URI` → a local SQLite store at `backend/mlflow.db`
   (browse with `mlflow ui --backend-store-uri sqlite:///mlflow.db`; plain `mlflow ui` looks in
   `./mlruns` and will find nothing). It is SQLite rather than `./mlruns` because mlflow 3.x put the
   filesystem store in maintenance mode and refuses it. Set `MLFLOW_TRACKING_URI`
   to `${CI_API_V4_URL}/projects/<ID>/ml/mlflow` (+ `MLFLOW_TRACKING_TOKEN`) → GitLab's registry.
2. **Data versioning + lineage** — `python -m app.ml.export_dataset` snapshots the dataset + a
   content-addressed `dataSha256`; **DVC is initialized in this repo** (`.dvc/` + the committed
   `backend/data/triage_dataset.npz.dvc` pointer version the binary). `python -m app.ml.validate_data`
   is the schema/quality gate on that snapshot, and the `test:data-lineage` CI gate proves the model
   trained on exactly that dataset.
3. **Monitoring (drift + performance)** — `python -m app.ml.monitor` emits an Evidently drift + accuracy
   report (PSI fallback if Evidently is absent). Serving writes a de-identified **inference log**
   (`app/ml/inference_log.py`) + model-level Prometheus metrics (prediction mix, confidence, model
   version), so `CAREROUTE_MONITOR_SOURCE=live` monitors **real traffic**, not just a simulated shift.
   Runtime **alerting** closes the loop: Prometheus evaluates `monitoring/alert.rules.yml` (inference and
   agent p95 latency, guardrail-block spikes, escalation-rate anomalies, backend-down) and
   **Alertmanager** (`monitoring/alertmanager.yml`, :9093) groups, routes and de-duplicates what fires —
   availability and guardrail alerts skip the batching delay, and a backend-down alert inhibits the
   latency alerts it would otherwise cause. Receivers are intentionally empty in the repo: a webhook URL
   is a credential, so it belongs in deployment config, not in git.
4. **Continuous Training** — `train:model` fires on a **GitLab scheduled pipeline** (weekly retrain →
   re-gate → re-register), and the **drift→retrain loop is closed**: a monitor drift breach triggers a
   retrain pipeline via the GitLab API (set `CAREROUTE_PIPELINE_TRIGGER_TOKEN`; loop-guarded). The
   **HITL ground-truth log** (clinician `finalAcuity` on escalation decisions → `ground_truth.jsonl` +
   agreement metrics) supplies real labels for measuring live accuracy.
5. **Deployment lifecycle** — GitLab **staging/production environments**, MLflow **Dev→Staging→Production**
   stage transitions on promotion, and **rollback** as a registry stage change + version pin.

### Deliberately deferred (industry "best" that is overkill here)

Taking the *best* of industry MLOps means also knowing what **not** to add to a prototype:

- **Feast (feature store)** — solves online/offline feature consistency and cross-model reuse. CareRoute
  has one model and computes features in-process, so a feature store adds infrastructure without value.
  *Adopt when* features are served online and shared across models/teams.
- **Kubeflow / Vertex / SageMaker Pipelines** — DAG orchestration on elastic (GPU) compute. The GitLab
  stage-DAG covers this at prototype scale. *Adopt when* you need elastic training/serving at scale.
- **ELK aggregated logging** — CareRoute uses per-case `key=value` operator logs + Prometheus metrics,
  enough for one service. *Adopt when* many agents/services across containers need centralized log search
  (JSON structured logging + OpenTelemetry tracing would be the first step).

### Platform choice — why GitLab (and what would beat it)

**GitLab CI/CD is a production-grade MLOps orchestration backbone and the right choice here** (it is also
the stack named in the CareRoute proposal §2.5/§2.6.5). **GitLab ships native, MLflow-API-compatible
Model Registry + Model Experiments**, so `app/ml/train.py` targets it with no code change. Like any CI
engine, GitLab is not a *full* ML platform — feature store, DAG lineage and live monitoring are bolt-ons
(MLflow, DVC, Evidently here). A dedicated platform (**SageMaker / Vertex / Azure ML / Kubeflow**) only
wins when you need managed, scalable training/serving infra — overkill for this prototype. **Decision:
keep GitLab as the backbone and augment it**, not migrate.

---

## Project structure

```
.
├── backend/
│   ├── app/
│   │   ├── main.py          FastAPI app, SSE pipeline, all endpoints
│   │   ├── agents/          one file per agent (owner-scoped) + shared base.py
│   │   │   ├── base.py          CaseState + AgentContract + tool allow-list (platform)
│   │   │   ├── messaging.py     A2A message bus + AgentMessage + COMMS interface (platform)
│   │   │   ├── capability.py    AgentCapability declarations + enforcement (platform)
│   │   │   ├── reasoning.py     additive reasoning-layer template, floor-preserving (platform)
│   │   │   ├── intake.py        Symptom-Intake        (Sham Goh)
│   │   │   ├── classifier.py    Severity-Classifier   (Koh Guan Chin James)
│   │   │   ├── safety.py        Safety-Override       (Aaron Liew)
│   │   │   ├── routing.py       Care-Routing          (Marcus Teh)
│   │   │   ├── hitl.py          Human-in-the-Loop     (Heriz Yusoff)
│   │   │   ├── handoff.py       Clinician-Handoff     (Heriz Yusoff; escalated cases only)
│   │   │   ├── reflection.py    Reflection / Critic   (platform)
│   │   │   ├── orchestration.py  PipelineOrchestrator  (Sham & mixed into intake)
│   │   │   └── supervisor.py     DEPRECATED alias      (see its docstring)
│   │   ├── safety_nlp/      semantic red-flag layer over the regex floor (Aaron Liew):
│   │   │                    classifier · ner · assertion (negation) · similarity · context ·
│   │   │                    translation · benchmark · manifest-governed artifact loading
│   │   ├── services/        external-provider clients (OneMap)
│   │   ├── llm.py           OpenAI provider chain (breaker, router, cache)
│   │   ├── guardrail.py     input + output guardrails
│   │   ├── redact.py        PII/PHI redaction (LLM02)
│   │   ├── ratelimit.py     rate limiter (LLM10)
│   │   ├── redflags.py      deterministic safety-override rules
│   │   ├── audit.py         hash-chained tamper-evident audit trail
│   │   ├── metrics.py       Prometheus telemetry
│   │   ├── rag.py           TF-IDF vector retrieval of guidance citations
│   │   ├── rag_onyx.py      optional Onyx (Danswer) RAG backend, TF-IDF fallback
│   │   ├── store.py         in-memory cases/escalations + episodic index + HITL ground-truth log
│   │   ├── models.py        Pydantic request/response models
│   │   ├── config.py        typed env configuration
│   │   ├── evals/           the evaluation plan as CODE (spec.py + plan.py), CI-validated
│   │   └── ml/              real severity model + MLOps entrypoints:
│   │       ├── model.py         RandomForest + SHAP + calibration + counterfactual + post-processing + fairness/CV/agreement audit
│   │       ├── data.py, features.py, fairness.py
│   │       ├── train.py         build + release-gate + persist + MLflow-register
│   │       ├── export_dataset.py / validate_data.py   dataset snapshot + data-validation gate
│   │       ├── monitor.py       Evidently drift + drift→retrain trigger (live-traffic capable)
│   │       ├── inference_log.py de-identified per-prediction log (live monitoring feed)
│   │       └── report.py        executive PDF + Markdown report (incl. security findings summary)
│   ├── tests/               796 tests across 58 files: unit + e2e + pipeline + fairness/guardrail/report + ML entrypoints
│   ├── scripts/             show_a2a.py · show_clarification_handshake.py · smoke_test_handoff_llm.py · try_agent.py
│   ├── models/safety/manifest.json   Safety-NLP GOVERNANCE RECORD (checked in, not a build artifact):
│   │                                 which optional models may load, under which approvals
│   ├── ruff.toml            pinned lint rule set — keeps the blocking lint gate deterministic
│   ├── MODEL_CARD.md        model documentation
│   └── SECURITY.md          OWASP-mapped risk register
├── frontend/                Next.js App Router + CSS Modules (patient + staff portal)
│   ├── playwright.config.js browser-test config (system Chrome locally, bundled in CI)
│   └── tests/e2e/           Playwright agent-pipeline tests that cannot pass on the simulation fallback
├── monitoring/              Prometheus scrape + alert rules, Alertmanager routing, Grafana dashboards
├── dev.sh                   one-command local dev: backend + frontend, both hot-reload
├── security/                red-team scanner configs + docs
├── .gitlab-ci.yml           MLSecOps CI/CD pipeline
├── docker-compose.yml       backend + frontend + Prometheus + Alertmanager + Grafana
├── ASPECTS.md               course-requirement → code traceability
└── README.md                (this file)
```

---

## Further documentation
- **[`docs/vault/`](./vault/)** — the project **Obsidian vault** (open the folder in Obsidian):
  App Overview, MLOps Pipeline, Loop Engineering, RAG and Onyx, Roadmap, Changelog — cross-linked, the doc source-of-truth
  - **[Agent Capability Audit](./vault/Agent%20Capability%20Audit.md)** — which "agents" are genuinely agents, and the upgrade path for each policy node
  - **[Evaluation Plan](./vault/Evaluation%20Plan.md)** — the eight evaluations (E1–E8), their datasets, metrics and acceptance criteria, plus the findings they surfaced
  - **[Proposal Review Feedback](./vault/Proposal%20Review%20Feedback.md)** — the reviewer's three points and how each is answered *in code*
- **[`ASPECTS.md`](../ASPECTS.md)** — every course requirement mapped to where it lives in code
- **[`backend/MODEL_CARD.md`](../backend/MODEL_CARD.md)** — model intended use, data, metrics, limitations
- **[`backend/SECURITY.md`](../backend/SECURITY.md)** — OWASP LLM Top 10 risk register
- **[`docs/GOVERNANCE.md`](./GOVERNANCE.md)** — PDPC Model AI Governance self-assessment (4 pillars)
- **[`security/README.md`](../security/README.md)** — the full scanner catalogue + how to run each locally
