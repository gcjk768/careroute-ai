---
tags: [rag, onyx, retrieval, careroute]
updated: 2026-09-25
---
# RAG and Onyx

Back to [[Home]]. Related: [[App Overview]] · [[MLOps Pipeline]] · [[Loop Engineering]] · [[Roadmap]] · [[Changelog]]

The Severity-Classifier ([[App Overview]]) grounds each triage decision with **cited clinical guidance**
via `rag.retrieve()`. Two backends now sit behind one contract `retrieve(query, top_k) -> [{title, snippet, source}]`:

## 1. TF-IDF (default, in-process)
[`app/rag.py`](../../backend/app/rag.py) — real vector-space cosine similarity over a curated guidance
corpus. Zero infra, always demonstrable, keyword-overlap fallback if scikit-learn is missing.

## 2. Onyx (formerly Danswer) — the production upgrade path
[`app/rag_onyx.py`](../../backend/app/rag_onyx.py) — a **drop-in**: when `ONYX_BASE_URL` + `ONYX_API_KEY`
are set, `retrieve()` queries a self-hosted **Onyx Community Edition** (free, MIT) for chunks and maps
them into the same contract; **any failure falls back to TF-IDF** so citations are never lost.

- **Free?** Yes — CE is MIT-licensed & self-hostable (real embeddings + hybrid search + 40+ connectors).
  EE (SSO/RBAC/analytics) and Onyx Cloud (~$20/user/mo) are paid.
- **Why it fits:** self-hosted → PHI stays in-house (PDPC-relevant); the grown-up successor to TF-IDF.
- **Caveat:** it's a separate service to run (Postgres + Vespa) — worth it only with real doc corpora.
- **Endpoint (verified 2026-09-25 against Onyx CE v2.11.4):** `POST /api/admin/search`, body
  `{query, filters: {}}`, response `{documents: [SearchDoc]}`.
  - The old Danswer `/api/query/document-search` is gone.
  - This is the only document-search endpoint that needs no LLM. It ranks by Vespa keyword (BM25),
    not hybrid, and it needs an admin or curator key when authentication is on.
  - In v4.x, `POST /api/search` always runs an LLM selection step.
  - v2.12+ removed `AUTH_TYPE=disabled`.
- **Measured (32-doc corpus, 14 scored queries, top_k=3):** hit@3 was 1.0 for TF-IDF, hybrid and
  Onyx. Onyx had MRR@3 0.964 and 1.86 off-topic hits per query, against 0.50 for hybrid. Mean
  latency was 19.6 ms for Onyx and 3.0 ms for hybrid. **No quality gain at this corpus size.**
  Evidence is in `report/review/onyx_results.md` and `report/screenshots/onyx/`.

Tests: [`tests/test_rag_onyx.py`](../../backend/tests/test_rag_onyx.py) — proves default-off fallback,
the configured mapping, and network-error fallback.

## Demo (5 steps)
1. `docker compose up` Onyx CE → 2. ingest guidance docs → 3. make an API key →
4. `export ONYX_BASE_URL=... ONYX_API_KEY=...` → 5. show the same triage query with the vars set (Onyx
corpus) vs unset (TF-IDF) — identical UI, better retrieval.

## Lighter alternative
If you only want better retrieval (not a whole platform): swap TF-IDF for **pgvector / Chroma / FAISS**
+ transformer embeddings, staying embedded in FastAPI. See [[Roadmap]].
