---
tags: [moc, careroute]
updated: 2026-07-17
---
# CareRoute AI — Project Vault (Home)

> **Map of Content (MOC).** This vault is the **doc source-of-truth** for CareRoute AI.
> **Workflow rule:** *always read the relevant note here BEFORE working on a task*, and
> **update [[Changelog]] + [[App Overview]] on every code change.** Link new notes with `[[wikilinks]]`.
> System: **expert PARA-lite + Zettelkasten** (flat, links > tags, MOC navigation) — see [[_Conventions]].
> Capture rough notes in [[Inbox]].

CareRoute AI is a multi-agent, bias-audited healthcare **triage assistant** built for the NUS-ISS
*Architecting AI Systems* Practice Module (Team 3, Proposal 3). Deterministic safety + human-in-the-loop
keep a clinician in control. See the repo [`README.md`](../../README.md).

## Core notes
- [[App Overview]] — architecture, agents, ML model, security controls (the "what is this")
- [[MLOps Pipeline]] — the GitLab CI/CD pipeline, the 5 MLOps pillars, course alignment
- [[Courseware Alignment]] — DOAIS courseware gates mapped onto this pipeline, and what does not apply
- [[Loop Engineering]] — the 2026 trend + how CareRoute already embodies it
- [[RAG and Onyx]] — the retrieval layer + the Onyx upgrade path
- [[Lecture Alignment]] — the 5 lecture decks vs our code, slide by slide, with the 7 gaps
- [[Cross-Module Alignment]] — the other three modules (AIC, AAS, XRAI): OWASP renumbering, the agentic taxonomy, the classical attack surface
- [[Intake Handoff Guide]] — **read before wiring your agent to intake** (Sham): what you get, the empty-keyword trap, how to test in isolation
- [[Proposal - Intake as Orchestrator]] — open architecture question: should intake route the other agents? (recommendation: no)
- [[Roadmap]] — future movement / planned changes
- [[Leftover Jobs 2026-09-02]] — **start here**: the tick-list of everything still open after 2026-09-02,
  in the order to do it (James items, infra items, watch items, roadmap)
- [[Open Work 2026-09-01]] — **handover**: what is still open on the integration branch, what is
  already ruled out, and the exact commands to pick each item up
- [[Infra-Dependent Work 2026-09-02]] — what the 2026-09-02 whole-code audit could NOT fix without
  infrastructure or a decision (auth, Docker, runner, DVC remote, MLflow, Evidently, proxies, TLS)
- [[Changelog]] — dated log of code + doc changes
- [[Progress Log - Sham]] — per-update progress for reporting (Symptom-Intake lane)
- [[Clinician Handoff UI]] — frontend wiring for the handoff packet; unblocked now the agent is merged
- [[Clinician Handoff Pipeline Integration]] — Heriz's integration spec (**done**; its §3.3 targets the pre-takeover `supervisor.py`)
- [[Clarification Resume API Handoff]] — Heriz's spec for the ask/resume path (**not started**: a patient still cannot answer a clarifying question)

## Design & review notes
- [[Agent Capability Audit]] — which "agents" are actually agents, and each policy node's route to agency
- [[Evaluation Plan]] — one spec per evaluation, with its acceptance criteria and its honest blocker
- [[Clarifying Questions Design]] — active elicitation: asking the patient one question instead of guessing
- [[HITL Clarification Handshake]] — #active handoff to Heriz: the proposal is delivered, nothing reads it yet
- [[HITL Handshake Runbook]] — copy-exact steps to reproduce that verified state on Heriz's branch (LLM-safe)
- [[Proposal Review Feedback]] — Junhua's review points and where each is answered

## Vault meta
- [[_Conventions]] — how this vault is organised (read once) · [[Inbox]] — capture & triage

## Graded course modules (this project demonstrates all four)
| Module | Primary notes |
|--------|---------------|
| Explainable & Responsible AI | [[App Overview]] (SHAP, fairness, drift) |
| AI & Cybersecurity | [[App Overview]] (guardrails, red-flag override, audit) |
| Architecting Agentic AI | [[App Overview]] + [[Loop Engineering]] |
| Integrating & Deploying (MLSecOps) | [[MLOps Pipeline]] |

## Quick pointers
- Requirement → code traceability: [`ASPECTS.md`](../../ASPECTS.md)
- Governance self-assessment: [`docs/GOVERNANCE.md`](../GOVERNANCE.md)
- Model card: [`backend/MODEL_CARD.md`](../../backend/MODEL_CARD.md)
- Security register: [`backend/SECURITY.md`](../../backend/SECURITY.md)
