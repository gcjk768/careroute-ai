# CareRoute AI — Course-Aspect Traceability Matrix

This document maps the **Practice Module** requirements (from
`Briefing/AAS Practice Module Briefing v4.1_NCS.pdf`) to exactly where each aspect is demonstrated
in this codebase. It is the evidence that the implementation "adequately utilises and demonstrates
the skills learned in the course modules."

The briefing's *Scope of Work* asks the system to demonstrate: **assurance, trust, fairness,
accountability, ethics, explainability, governance, security, agent autonomy & orchestration, and
MLOps/LLMOps practices.** Each is traced below.

---

## 1. Four course modules → code

### Module 1 — Explainable & Responsible AI
| Aspect | Where in code |
|--------|---------------|
| **Explainability (real SHAP)** | `backend/app/ml/model.py` → `TriageModel.predict()` runs `shap.TreeExplainer` on the trained RandomForest to produce signed, urgency-oriented feature contributions over the reported symptoms; surfaced in the `final` SSE event, the Recommendation card (`PatientTriage.jsx`) and clinician review (`ClinicianDashboard.jsx`). A keyword surrogate (`SeverityClassifierAgent.explain()`) remains as a fallback. |
| **Rationale / traceable reasoning** | `build_rationale()` (`app/agents/orchestration.py`); shown to patient and clinician. |
| **Fairness / bias audit (real)** | `backend/app/ml/fairness.py` + `model.py` compute REAL per-subgroup accuracy, before→after fairness gap (age-blind baseline vs age-aware + subgroup-rebalanced mitigation), and red-flag recall on held-out synthetic data; served at `GET /api/fairness`; `GovernanceDashboard.jsx`. Seeded snapshot in `store.py` is the fallback only. |
| **Named fairness metrics (Demographic Parity, Equal Opportunity, Equalized Odds, Disparate Impact)** | `fairness.demographic_parity()` / `equal_opportunity()` / `equalized_odds()` / `disparate_impact()` — the four named classification-fairness metrics from XRAI Day 2; all in the `/api/fairness` payload. Equalized Odds adds the FPR half that Equal Opportunity is blind to (over-triaging a group can score a perfect EO gap); Disparate Impact is the four-fifths **ratio**, reported not gated — age-adjusted acuity is correct medicine, so a 0.8 floor across age bands would gate against this project's own fairness mitigation. |
| **Counterfactual fairness** | `model._counterfactual_audit()` flips the protected attribute (sex) and reports the acuity flip rate + mean delta (should be ~0). |
| **Counterfactual explanation (patient-facing)** | `TriageModel._counterfactual()` — bounded single-edit search on the calibrated model ("would move from P4 to P3 if breathlessness were also reported"); demographics never edited; shown under the SHAP bars in `PatientTriage.jsx`; `tests/test_counterfactual.py`. |
| **Explanation faithfulness (SHAP vs local surrogate)** | `model._explanation_agreement()` — in-repo LIME-style local linear surrogate vs TreeExplainer SHAP on held-out rows: same leading reported symptom (gated), feature agreement@3 over all flags, rank correlation, sign agreement (Krishna et al. 2022); floored at `MIN_EXPLANATION_AGREEMENT` in `train.validate`. |
| **K-fold reliability** | `model._cross_validate()` — 5-fold StratifiedKFold over the served pipeline; mean ± std for accuracy, red-flag recall and fairness gap on `/api/fairness` (`crossValidation`). |
| **Post-processing fairness mitigation + gated Equal Opportunity** | `fairness.equal_opportunity_thresholds()` — raise-only, deterministic, per-group P(severe) thresholds applied at serving; before/after on `/api/fairness` (`postProcessing`); `MAX_EQUAL_OPPORTUNITY_GAP` enforced in `train.validate` and re-verified with Fairlearn `MetricFrame` in `tests/test_fairness_gate.py`. |
| **Live labelled monitoring (HITL loop closed)** | `inference_log.load_labelled()` joins clinician ground truth to the inference log by case id; `monitor.py` reports live accuracy, red-flag recall, subgroup accuracy and the Equal Opportunity gap (`performance.labelled`). |
| **Model Card (traceability doc)** | `backend/MODEL_CARD.md` — intended use, data, hyperparameters, metrics, limitations (PDPC §4.30). |
| **Datasheet for Datasets (Gebru et al.)** | `backend/DATASHEET.md` — motivation, composition, collection, preprocessing, uses, maintenance, limitations. Every falsifiable number is pinned against `triage_dataset.meta.json` by `tests/test_datasheet.py`, so a dataset change that is not reflected in the datasheet fails the build. |
| **PDPC governance self-assessment** | `docs/GOVERNANCE.md` Part 1 — maps features to the 4 PDPC Model AI Governance pillars. |
| **XRAI Day 3 maturity instruments** | `docs/GOVERNANCE.md` Part 2 — AI Governance Maturity (assessed **Level 4** on validation/monitoring, Level 3 partial on cataloguing, Level 5 blocked by having no deployment target), Accenture AI Maturity, IEEE AI Ethics Readiness, and AIRI. Organisational dimensions are marked **N/A** with the reason rather than self-scored flatteringly. |
| **Stakeholder interaction / feedback (PDPC pillar 4)** | AI-use disclosure in the patient UI + `POST /api/cases/{id}/feedback` (recorded into the audit trail). |
| **Accountability & human authority (HITL)** | `HumanInTheLoopAgent` (`app/agents/hitl.py`), escalation flow, `ClinicianDashboard.jsx` — "AI assists, the clinician decides." |
| **Grounding / citations (anti-hallucination)** | `backend/app/rag.py` → `retrieve()`: real **TF-IDF vector-similarity** retrieval (cosine) over the guidance corpus; citations attached to every response. |

### Module 2 — AI & Cybersecurity
| Aspect | Where in code |
|--------|---------------|
| **Prompt-injection / jailbreak defence (LLM01)** | `backend/app/guardrail.py` `screen()` — deterministic regex screen that runs **before** any agent; blocked input never reaches the LLM. |
| **Output-handling defence (LLM05)** | `guardrail.screen_output()` — screens the (LLM-influenced) rationale for prompt-leak/injection before it is shown to a clinician; suppressed + audited on a hit. |
| **Sensitive-info disclosure (LLM02)** | `backend/app/redact.py` — masks NRIC/phone/email/MRN before the text reaches any LLM or is stored. |
| **Unbounded consumption / DoS (LLM10)** | `backend/app/ratelimit.py` — per-client fixed-window rate limit on `/api/triage/stream` (429 over-limit). |
| **Least-privilege tool access (LLM06 Excessive Agency)** | Per-agent `TOOL_ALLOWLIST` in each `app/agents/<agent>.py` + `enforce_tool_access()` in `app/agents/base.py`. (LLM06 under the **2025** list; this was LLM08 in the 2023 edition.) |
| **Supply chain (LLM03)** | Pinned deps + CycloneDX SBOM per build; Trivy blocks CRITICAL CVEs; `modelscan` + `fickling` scan the serialized model itself. |
| **System prompt leakage (LLM07)** | `guardrail.screen_output()` suppresses and audits a rationale that echoes system-prompt or injected text before a clinician sees it. |
| **Full OWASP Agentic Top 10 (ASI01–ASI10)** | Position stated per threat — control, or N/A-with-reason — in `backend/SECURITY.md` → "Agentic threats". |
| **Memory & context poisoning (ASI06)** | `store.memory_digest()` SHA-256 fingerprints every case as it enters episodic memory; `recall_session()` re-verifies and **excludes** any record that fails or has no digest (fails closed), and `verify_episodic_memory()` reports what was excluded. Same tamper-evidence idea as the hash-chained audit log, applied to the memory that feeds later reasoning. |
| **Cascading failures (ASI08)** | Circuit breaker on the LLM provider chain (`app/llm.py`): 3 consecutive failures skip a provider until a cooldown elapses, then one half-open trial decides. Caps the 5×-per-case timeout amplification a dead provider would otherwise cause; state exposed on `/api/health`. |
| **Rogue-agent controls (ASI10)** | `CAREROUTE_KILL_SWITCH` forces deterministic-only mode (`config.py`+`llm.py`); the audit log is **SHA-256 hash-chained** and tamper-evident (`audit.py`, verified at `GET /api/cases/{id}/audit`). |
| **Classical ML attack surface (AIC Day 1)** | Evasion **measured** by ART HopSkipJump (`tests/test_robustness.py`; the `test:robustness` job is `allow_failure`, i.e. advisory, not a gate); poisoning + membership inference gated by `tests/test_ml_attacks.py` (`ai-security:ml-attacks`); model extraction measured and accepted as a risk in `SECURITY.md`. |
| **Model-side defences (AIC Day 1)** | **Ensemble learning** — the deployed model is a 200-tree RandomForest. **Adversarial re-training** — `app/ml/adversarial.py`, implemented and measured; the result was **inconclusive at a CI-affordable attack budget** (a 3-sample difference over 24 attacked rows), so the deployed model is unchanged on the basis of a measurement rather than an omission. Written up in `docs/vault/Cross-Module Alignment.md`. |
| **Deterministic safety-override (defence-in-depth)** | `backend/app/redflags.py` — hard-coded rules that can only **raise** acuity; un-overridable by the LLM. |
| **AI security risk register** | `backend/SECURITY.md` — risks mapped to OWASP LLM Top 10 with concrete controls. |
| **AI security tests** | `backend/tests/test_guardrail.py`, `test_api_e2e.py` (injection payloads blocked end-to-end); CI-gated. |
| **LLM red-team scanning** | `security/` (repo root) + CI `ai-security` stage: **Promptfoo** (per-PR regression when an API key is configured; advisory), **Garak** (manual per-release scan of the vendor model), **DeepTeam** (manual, OWASP LLM Top 10, vendor model with a stand-in prompt), **PyRIT** (placeholder job — orchestration script not yet written). See `security/README.md`. |
| **AppSec scanning** | CI `security-scan` stage: **Gitleaks** (secrets), **Semgrep** + **Bandit** (SAST), **Trivy** (deps/secrets/IaC/image), **pip-audit** + **Safety** / **npm audit** (SCA), **modelscan** (serialized-model), **OWASP ZAP** (DAST), **Hadolint** (Dockerfile). |
| **Responsible-AI fairness gate** | CI `ai-security:fairness-gate` — **Fairlearn** subgroup accuracy-parity gate (`tests/test_fairness_gate.py`); fails if the model regresses to the age-blind baseline. |
| **Input hardening** | size/empty caps in `guardrail.py`; short LLM timeouts in `llm.py`. |

### Module 3 — Architecting Agentic AI Solutions
| Aspect | Where in code |
|--------|---------------|
| **Multi-agent system (≥3 agents)** | Seven agents, one module each under `app/agents/`: Supervisor + Symptom-Intake, Severity-Classifier, Safety-Override, Care-Routing, Human-in-the-Loop, **Reflection/Critic**. |
| **Reflection / Evaluator-Optimizer pattern** | `ReflectionAgent` — a **deterministic** critic (three rule checks, no LLM call) that reviews the assembled decision and applies one more-cautious corrective pass (e.g. forces escalation of an un-escalated P1/P2) before the Supervisor finalises. Its `upgrade_path` describes the additive LLM critic that would complete the pattern. |
| **Supervisor / orchestration pattern** | `Supervisor` class — activates workers in a fixed, safety-gated order and aggregates the cited response. (Grounded in `Course Notes/.../Supervisor Agent — pattern reference (CareRoute).md`.) |
| **Autonomy (Spectrum of Agency)** | `AUTONOMY_LEVEL` per agent (L1–L3); reasoning + fallback per worker. |
| **Reasoning / tool use** | Each worker's LLM prompt (reasoning) + deterministic fallback + declared tools (`TOOL_ALLOWLIST`). Care-Routing runs a bounded ReAct loop over a two-tool registry (`routing.py`). There is **no** goal decomposition / re-planning step — the workflow order is fixed and safety-gated. |
| **Shared working + episodic memory** | `CaseState` threaded through the pipeline (working memory); per-`sessionId` case index for cross-visit recall (`store.recall_session`, `GET /api/sessions/{id}/history`). |
| **RAG grounding (lexical)** | `rag.py` — TF-IDF vectors + cosine-similarity retrieval over a git-committed guidance corpus. This is **lexical** retrieval, not dense-embedding vector search: there is no embedding model, chunking or vector database. An optional Onyx connector (`rag_onyx.py`) is the path to a real vector store; it is not wired by default. |
| **Inter-agent communication** | Typed dict results + the shared `CaseState`; Supervisor routes between them. |
| **Prompt patterns & fallback strategies** | `PROMPT_PATTERN` note per agent; every worker has an LLM-first / rule-based-fallback path. |
| **Simple UI prototype** | `frontend/` — four React surfaces incl. the live pipeline visualizer. |

### Module 4 — Integrating & Deploying (MLSecOps / LLMSecOps)
| Aspect | Where in code |
|--------|---------------|
| **CI/CD pipeline** | `.gitlab-ci.yml` (repo root) — 10 stages: lint → train → test → monitor → ai-security → security-scan → report → build → deploy → post-deploy. |
| **Automated testing (incl. AI-security tests)** | `backend/tests/` (unit + e2e + security); wired into CI. |
| **Logging & traceability / audit trail** | `backend/app/audit.py` (`audit_log`, SHA-256 hash-chained) + Python `logging`; `GET /api/cases/{id}/audit` returns entries + a `verified` flag. |
| **Metrics / observability (Prometheus)** | `backend/app/metrics.py` + `GET /metrics` — request/guardrail/escalation counters and model-inference latency histogram (Grafana is the prod dashboard). |
| **LLM tracing (Langfuse / LangSmith)** | `backend/app/tracing.py` — per-case trace of every agent run and every served LLM call (masked prompt, output, model, latency), keyed by case id; off unless keys are set. Self-hosted Langfuse via `docker compose --profile tracing up`. |
| **LangGraph** | `backend/app/agents/graph.py` — the pipeline as a `StateGraph` over the existing agents, edges compiled from `planner.ALLOWED_TRANSITIONS`; demo/diagram surface (`scripts/show_graph.py`), not the production path. |
| **Monitoring / drift (real)** | `backend/app/ml/fairness.py` computes REAL data/target/concept drift via Population Stability Index against a shifted "production" sample; served in `GET /api/fairness`; `GovernanceDashboard.jsx` drift monitors. (Evidently AI is the prod drop-in.) |
| **Experiment tracking + Model Registry** | `app/ml/train.py` (`_mlflow_log()`) + `app/ml/model.py` (`save_artifact()`) — persists a versioned, content-addressed `joblib` + SHA-256 and, when MLflow is installed, logs params/metrics + registers `CareRouteTriageRF`. |
| **Model versioning** | `modelVersion` from the trained model (`app/ml/model.py`), surfaced in the fairness audit. |
| **Deployment strategy** | Designed, not yet exercised: `deploy:*` jobs in `.gitlab-ci.yml` (shadow, promote, canary, rollback) are manual + `allow_failure` stubs with the canary logic unit-tested (`app/ml/canary.py`); the Terraform plan job is a placeholder. Target architecture in `docs/ARCHITECTURE.md`. |

---

## 2. Briefing "Success criteria" → status
| Success-criteria deliverable | Status | Location |
|------------------------------|--------|----------|
| Presentation slides | out of code scope | `../report/`, `../unfiled/Presentation Guideline.pdf` |
| System architecture document | ✅ | `../diagrams/`, `../source_diagrams/`, `../docs/` |
| Agent design documentation | ✅ | code comments + this file + `docs/ARCHITECTURE.md` + `docs/vault/Agent Capability Audit.md` |
| Explainable & Responsible AI report | ✅ (evidence) | fairness endpoint + `GovernanceDashboard`; narrative in `../docs/` |
| AI security risk register | ✅ | `backend/SECURITY.md` |
| MLSecOps / LLMSecOps pipeline design | ✅ | `.gitlab-ci.yml` (repo root) |
| Well-structured, documented source repo | ✅ | `app/backend`, `app/frontend` (modular + commented) |
| Testing artifacts (unit + e2e + AI-security) | ✅ | `backend/tests/` |
| Simple UI prototype | ✅ | `app/frontend` (4 surfaces) |

---

## 3. How to demonstrate at the presentation
1. **Autonomy & orchestration** — run a chest-pain case on `/pipeline`; watch the Supervisor activate each worker over live SSE.
2. **Safety / security** — submit a prompt-injection string; it is blocked at the guardrail before any agent runs. Submit chest pain; the deterministic red-flag forces **P1** and cannot be lowered.
3. **Explainability** — the recommendation shows REAL SHAP feature contributions (from the trained RandomForest) plus cited evidence.
4. **Responsible AI / HITL** — the low-confidence / red-flag case escalates to the Clinician dashboard for a human decision.
5. **Governance** — the Governance dashboard shows per-subgroup fairness, gap reduction, and drift.
6. **MLSecOps** — `.gitlab-ci.yml` runs tests + AI-security regression on every push; `GET /api/cases/{id}/audit` shows the full decision trail.
