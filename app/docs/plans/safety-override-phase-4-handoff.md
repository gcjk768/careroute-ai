# Safety Override Phase 4 Handoff

Date: 2026-08-18

This handoff captures the current Safety NLP/LLM implementation state so a new session can continue without rediscovering local decisions.

## Current State

- Main plan: `docs/plans/safety-override-llm-nlp-implementation-plan.md`
- Current phase: Phase 4, `Local NLP Benchmark and Prototype-Ready Mode`
- Strategic change: this school project is allowed to demonstrate labelled educational prototype semantic activation, while real-world production claims remain blocked without clinical/legal/governance approvals.
- Safety A2A remains intact. Safety still publishes `safety.override` and subscribes to `acuity.classified` plus `safety.assessment.requested`.
- NLP/model additions are still isolated from the bus payload and must not emit patient text to A2A/audit logs.

## Implemented Phase 4 Pieces

- Translation benchmark scaffold: `backend/app/safety_nlp/benchmark.py`
- Safety model manifest loader/guardrails: `backend/app/safety_nlp/manifest.py`
- Biomedical NER adapter: `backend/app/safety_nlp/ner.py`
- Clinical assertion adapter: `backend/app/safety_nlp/assertion.py`
- Context rule adapter: `backend/app/safety_nlp/context.py`
- Similarity/NLI adapters: `backend/app/safety_nlp/similarity.py`
- Category recall benchmark: `backend/app/safety_nlp/category_benchmark.py`
- Optional Safety NLP dependency file: `backend/requirements-safety-nlp.txt`
- Safety NLP env/config settings: `backend/app/config.py`, `backend/.env.example`

## Local Model Artifacts

The local model artifacts were downloaded outside Git into:

- `backend/models/safety/biolord-2023`
- `backend/models/safety/labse`
- `backend/models/safety/mdeberta-v3-base-mnli-xnli`

Manifest:

- `backend/models/safety/manifest.json`

Recorded model hashes/revisions in the local manifest:

- BioLORD revision: `167aab527b238a50ca65224e6319215d2ff4fc9f`
- BioLORD directory hash: `ac5d32a4c8831e7526275c6dce68a74b3df775829f522c78ef5f105e18d6f058`
- LaBSE revision: `836121a0533e5664b21c7aacc5d22951f2b8b25b`
- LaBSE directory hash: `0690d30f183ffd38f69ac747cda6ee6df5ee814d5862079cc1da3f7a6a5cad06`
- mDeBERTa NLI revision: `8adb042d524ecd5c26d3e3ba0e3fbcf7e2d0864c`
- mDeBERTa NLI directory hash: `77a7c0c2d7002b68bd0beda7967a686a90abe3edbae4e71648073c05f793bc74`

## Benchmark Snapshot

Artifact-backed smoke benchmark over the current synthetic fixture:

| Model | English recall@1 | English recall@3 | Notes |
| --- | ---: | ---: | --- |
| BioLORD | 1.0000 | 1.0000 | Recommended runtime prototype model |
| LaBSE | 0.9524 | 1.0000 | Keep as benchmark comparator |
| mDeBERTa NLI | 0.6667 | 0.8095 | Keep as direct multilingual/NLI comparator |

Small non-English fixture results were weaker and should be treated as prototype evidence only, not clinical validation.

## Recommended Next Task

Implement selected-model runtime loading and startup warm-up.

Recommended scope:

- Select BioLORD as the minimum prototype runtime model.
- Keep LaBSE and mDeBERTa as benchmark comparators, not default runtime models.
- Load only the selected model from `backend/models/safety/manifest.json`.
- Use `local_files_only=True`; do not download models at runtime.
- Warm up once at startup or first use.
- Keep semantic activation default-off behind `CAREROUTE_SAFETY_PROTOTYPE_SEMANTIC_ACTIVATION=1`.
- Preserve monotone safety behavior: semantic output may only add escalation, never suppress deterministic red flags.
- Preserve A2A privacy: no patient text in `safety.override` payload.

No additional local model download should be needed for this task if BioLORD is selected.

## Tests Last Run

Safety NLP suite:

```powershell
C:\venvs\careroute\Scripts\python.exe -m pytest -p no:cacheprovider tests\test_safety_nlp.py tests\test_safety_nlp_benchmark.py tests\test_safety_nlp_manifest.py tests\test_safety_nlp_category_benchmark.py
```

Result: `60 passed`

Safety and A2A suite:

```powershell
C:\venvs\careroute\Scripts\python.exe -m pytest -p no:cacheprovider tests\agents\test_safety.py tests\agents\test_routing_a2a.py
```

Result: `31 passed`

