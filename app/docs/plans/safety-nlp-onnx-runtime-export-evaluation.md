# Safety NLP ONNX Runtime Export Evaluation

Date: 2026-08-23

## Decision

Do not export the selected Phase 4 BioLORD prototype model to ONNX yet.

## Context

- Selected runtime prototype model: `similarity.biolord`
- Runtime wrapper: `SafetyNlpRuntime`
- Local artifact smoke test: `tests/test_safety_nlp_artifact_backed.py`
- Current runtime loading mode: local files only, no Hugging Face downloads at request time
- Current activation mode: disabled by default; semantic activation requires `CAREROUTE_SAFETY_PROTOTYPE_SEMANTIC_ACTIVATION=1`

## Local Tooling Check

The current project environment does not include ONNX export/runtime tooling:

- `onnxruntime`: not installed
- `onnx`: not installed
- `optimum`: not installed

Adding these dependencies would increase setup size and complexity, and would require a separate accuracy-equivalence test before the exported model could be trusted.

## Evaluation

ONNX Runtime export is not needed for the current Phase 4 objective because:

- BioLORD has already been selected as the minimum prototype model.
- The selected runtime loader warms only BioLORD and does not load LaBSE or mDeBERTa.
- The opt-in artifact-backed smoke test proves local BioLORD loading works without runtime downloads.
- Normal tests remain model-free and lightweight.
- Phase 4 is still a benchmark/prototype-ready phase, not a latency-optimized deployment phase.

ONNX export should be reconsidered only when:

- the prototype path has stable thresholds,
- artifact-backed benchmark results are preserved as release artifacts,
- `onnx`, `onnxruntime`, and any export helper such as `optimum` are added to the optional Safety NLP dependency set,
- exported-model outputs are compared against the PyTorch/SentenceTransformer baseline,
- latency and memory improvements are large enough to justify the extra artifact and test burden.

## Required Future Gate

Before ONNX can replace the selected runtime artifact, add:

- export command documentation,
- exported artifact hash in `backend/models/safety/manifest.json`,
- parity test comparing ONNX and baseline top-k category rankings,
- latency and peak-memory benchmark comparison,
- rollback note restoring the non-ONNX BioLORD manifest entry.

