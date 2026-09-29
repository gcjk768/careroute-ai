# Bundle B/D — Agentic depth, governance and remaining no-infrastructure gaps

Date: 2026-09-16. Branch: `integration-all-agents-2026-08-31`. Owner: James (platform).
Scope instruction from the user: "do those you can do first" — every open courseware gap that
needs no new infrastructure (no deployment target, MLflow server, API key or CI runner change).

## Constraints discovered before design

- `OPENAI_API_KEY` is empty, so the course's `text-embedding-3-small` cannot run live. A local
  CLI provider and a local Ollama were available at the time.
- `fastembed==0.8.0` (ONNX `BAAI/bge-small-en-v1.5`, 384-d, no torch) and `mcp==1.9.4` resolve with
  every existing package pinned — additions only, no upgrades. Newer `mcp` releases force
  Starlette 1.6 / Pydantic 2.13 and are rejected. Both go in an OPTIONAL `requirements-agentic.txt`;
  core requirements and CI are unchanged, and every feature degrades when they are absent.
- Ownership: orchestration.py (Sham), hitl.py + handoff.py (Heriz), routing.py (Marcus), safety.py
  (Aaron) are teammate files. Everything below lives in platform files (reflection.py, rag.py,
  llm.py, tools/registry.py, main.py, store.py) or James's classifier.py, except two one-line hooks
  in orchestration.py, which carry owner notes.

## Items

| # | Item | Courseware anchor | Where |
|---|------|-------------------|-------|
| B1 | Central tool registry + gateway (schema-validated args, allow-list, metrics) + `GET /api/tools` | ArchAAS Day 3 AM tool registry / gateway | `app/tools/registry.py`, `main.py` |
| B2 | Dense + hybrid RAG: chunking, local ONNX embeddings (OpenAI backend when a key is set), in-process vector index, reciprocal-rank fusion with TF-IDF, retrieval eval (recall@k, MRR) | ArchAAS Day 1 embeddings / vector DB, Day 4 RAG | `app/rag.py`, `app/rag_embed.py`, `tests/fixtures/rag_queries.json` |
| B3 | Retrieval BEFORE generation in the classifier LLM prompt, plus prior-visit memory in the prompt | ArchAAS RAG + memory in reasoning | `agents/classifier.py` |
| B4 | LLM router: task → model tier per provider, exact-match TTL cache for cacheable tasks, router/cache metrics; semantic caching deliberately rejected for clinical decisions | ArchAAS Day 3 AM LLM router, caching | `app/llm.py`, `app/config.py` |
| B5 | LLM critic in Reflection: bounded ReAct over registry tools, RAG-grounded prompt, decides the loop's next edge {accept, escalate, rerun}; monotone merge; Reflection becomes an AGENT | ArchAAS Day 2 agency levels, reflection, ReAct; Day 3 guardrails | `agents/reflection.py`, 2 hooks in `orchestration.py` |
| B6 | MCP stdio server exposing the registry's read-only tools | ArchAAS Day 3 MCP | `app/mcp_server.py` |
| B7 | Evaluations E10 (retrieval) and E11 (critic decision validity) in the plan; capability audit updated | ArchAAS Day 3 evaluation | `app/evals/plan.py`, tests |
| B8 | OWASP Agentic Threats T1–T17 table | ArchAAS agentic threats guide, AIC Day 3 | `backend/SECURITY.md` |
| D1 | Impact assessment: stakeholders, harm matrix per acuity band, derived oversight level | XRAI Day 1 harm analysis, PDPC 3.1 | `docs/IMPACT_ASSESSMENT.md` |
| D2 | AI security policy following the CompanyXYZ template, NIST CSF 2.0 function mapping, prompt-injection incident runbook | AIC Day 3 policy crafting | `docs/AI_SECURITY_POLICY.md`, `security/incident-runbook.md` |
| D3 | Patient decision-review request (free text, redacted) → audit + clinician queue | PDPC 5.6 / 5.13 decision review | `main.py`, `store.py`, `PatientTriage.jsx`, `ClinicianDashboard.jsx` |
| D4 | Public "About the AI" page from the model card + live headline metrics | PDPC 5.x disclosure | `frontend/app/about-the-ai` |
| A1 | Behavioural anomaly control: per-client guardrail-block rate → temporary block + audit | AIC LLM10 monitoring & anomaly detection | `app/ratelimit.py`, `main.py` |
| A2 | Feature-space out-of-distribution detector (IsolationForest) → raise-only review signal | AIC Day 1 adversarial-example detection | `app/ml/model.py` |
| A3 | AI-for-cyber-defence security brief from scanner output (LLM with deterministic fallback) | AIC Day 3 Detect/Respond | `app/security_brief.py` |
| A4 | Advanced injection cases (adversarial suffix, multi-turn crescendo) in the scored corpus | AIC Day 2 advanced techniques | `tests/fixtures/guardrail_corpus.json` |

## Key decisions

- **Critic is additive and monotone.** It can add issues, force escalation or request one bounded
  re-run; it can never clear a deterministic issue or lower acuity. With the LLM unavailable the
  pipeline is byte-identical to before. The loop's existing iteration cap and budget bound cost.
- **The critic does not consume the trained model.** It reads the classifier's SHAP explanation and
  counterfactual from the case, so "exactly one agent uses the trained model" stays true.
- **Local embeddings first.** No new PHI egress path; OpenAI embeddings only when explicitly
  configured. `retrieve()` keeps its `{title, snippet, source}` contract; provenance is exposed
  through `retrieve_detailed()`.
- **Exact-match cache only, opt-in per task.** Two different symptom descriptions must never share
  a cached triage, so semantic caching is rejected in writing.

## Out of scope (needs infrastructure)

Registry push / staging / rollback, MLflow registry serving, live LLM evaluation in CI,
PyRIT orchestration against a model target.
