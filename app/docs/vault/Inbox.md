---
tags: [inbox]
updated: 2026-07-17
---
# Inbox — capture & triage

Back to [[Home]]. See [[_Conventions]].

Dump rough notes, links, and TODOs here as they arise; **triage** them into proper atomic notes
(or [[Roadmap]] items) during a weekly pass. Keep this short — a full inbox is a smell.

## Untriaged
- 2026-09-23 — 4 RAG/eval tests fail on this workstation and **also fail at the pre-merge commit**,
  so they are not merge damage: `test_eval_context` (x2), `test_eval_retrieval`,
  `test_eval_continuity`. Cause looks environmental — `rag_embed` logs *"dense retrieval skipped on
  this thread: onnxruntime has not been loaded on the main thread yet"*, so the hybrid path silently
  degrades to lexical and the continuity fixture stops reaching the ask branch. Decide whether the
  tests should `warm()` (or skip explicitly) rather than assert on a degraded retriever. See
  [[Changelog]] 2026-09-23 · [[RAG and Onyx]] · [[Evaluation Plan]].

## Recently triaged → where it went
- 2026-07-17 — Loop-engineering trend → [[Loop Engineering]]
- 2026-07-17 — Onyx RAG evaluation → [[RAG and Onyx]]
- 2026-07-17 — MLOps 5 pillars → [[MLOps Pipeline]]
