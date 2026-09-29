# Safety NLP Small-Adapter Pipeline Guide

## Purpose

Phase 4 should not start by downloading five large NLP models. It should start
with a stable pipeline shape that can run with fake adapters. Once that shape is
tested, each fake adapter can be replaced by one real model-backed adapter at a
time.

This guide explains how to build the real Safety NLP pipeline around small
adapters. It is scoped to Safety-Override only.

## Current Scaffold

The model-free scaffold now lives in:

| File | Purpose |
|---|---|
| `backend/app/safety_nlp/contracts.py` | Typed contracts between stages |
| `backend/app/safety_nlp/fake_adapters.py` | Fake deterministic adapters for tests |
| `backend/app/safety_nlp/pipeline.py` | `classify(text, language)` facade and conservative fusion |
| `backend/tests/test_safety_nlp.py` | Model-free tests; no downloads, no heavy imports |

The public contract is:

```python
from app.safety_nlp import classify

signals = classify(text, language)
```

It returns a list of `SafetySignal` objects. In Phase 4 these signals are
shadow-mode evidence only. They must not change `state.acuity_code`.

## Mental Model

You are not building one giant NLP model.

You are building a chain of small adapters:

```text
text
  -> translation adapter
  -> symptom NER adapter
  -> assertion adapter
  -> context adapter
  -> category adapter
  -> fusion into SafetySignal
```

Each adapter has one job, one input type, and one output type. If a model is
disabled, unavailable, times out, or returns unusable output, its adapter returns
a typed status instead of raising through triage.

## Stage 1: Translation Adapter

### Job

For Mandarin Chinese, Malay and Tamil, create an English working copy while
keeping the original text intact.

### Contract

```python
translate(text: str, language: str) -> TranslationResult
```

### Candidate Models

| Model | Source | Notes |
|---|---|---|
| `facebook/nllb-200-distilled-600M` | Hugging Face | Research/shadow only in this project because of CC-BY-NC-4.0 licensing. |
| `google/madlad400-3b-mt` | Hugging Face | Apache-2.0, but much larger. Benchmark separately. |

### Implementation Notes

- Do not replace `CaseState.raw_text`.
- Load only one translation model at a time.
- Add max input length and timeout.
- Return `status="unavailable"` if the model artifact is missing.
- Return `status="disabled"` if the feature flag is off.
- Never write original text or translated text to benchmark artifacts or audit logs.

## Stage 2: Symptom NER Adapter

### Job

Find symptom or biomedical entity spans in the English working text.

### Contract

```python
extract(text: str, language: str) -> list[MentionSpan]
```

### Candidate Model

| Model | Source | Notes |
|---|---|---|
| `d4data/biomedical-ner-all` | Hugging Face | Token-classification model. Validate carefully because it is trained on biomedical/case-report style text, not colloquial patient text. |

### Implementation Notes

- Preserve stable `mention_id`.
- Preserve `start` and `end` offsets against the working text.
- Keep a mapping back to the original-language mention when translation was used.
- If no mentions are found, return an empty list, not an exception.

## Stage 3: Assertion Adapter

### Job

For each mention, decide whether the symptom is present, negated, possible,
conditional or unknown.

### Contract

```python
classify(text: str, mention: MentionSpan) -> AssertionResult
```

### Candidate Model

| Model | Source | Notes |
|---|---|---|
| `bvanaken/clinical-assertion-negation-bert` | Hugging Face | Clinical assertion/negation classifier. It does not solve subject or temporality by itself. |

### Implementation Notes

- Mark the entity span before sending text to the model if the model expects a marked entity.
- Do not treat model confidence as calibrated probability.
- If output labels do not map cleanly to CareRoute labels, return `invalid_output`.

## Stage 4: Context Adapter

### Job

Detect who has the symptom and when it applies.

### Contract

```python
classify(text: str, mention: MentionSpan) -> ContextResult
```

### Library

| Library | Source | Notes |
|---|---|---|
| `medspaCy` ConText | Python package | Rule-based clinical context. Use in shadow mode and mark rules as not clinically reviewed in this project. |

### Implementation Notes

- Subject values: `patient`, `care_subject`, `other_person`, `unknown`.
- Temporality values: `current`, `recent`, `remote`, `unknown`.
- Conditional wording is not always suppression. Conditional self-harm remains safety-relevant.
- Keep every custom rule local to Safety NLP.

## Stage 5: Category Adapter

### Job

Map each mention to one of the known red-flag categories.

### Contract

```python
rank(text: str, mention: MentionSpan) -> list[CategoryCandidate]
```

### Candidate Models

| Model | Source | Notes |
|---|---|---|
| `FremyCompany/BioLORD-2023` | Hugging Face / sentence-transformers | Medical embedding model. UMLS/SNOMED licensing obligations must be reviewed before activation claims. |
| `sentence-transformers/LaBSE` | Hugging Face / sentence-transformers | General multilingual embedding baseline. Useful comparator, not preferred clinical model. |
| `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli` | Hugging Face | Direct multilingual NLI comparator. Helps avoid relying only on translation. |

### Implementation Notes

- Candidate categories must be restricted to `RED_FLAG_RULES`.
- Invalid categories must be dropped.
- For this project, compare against synthetic examples only.
- Do not claim clinician-approved similarity examples.

## Future Classifier

`emilyalsentzer/Bio_ClinicalBERT` is not a ready red-flag classifier. It is a
base encoder/checkpoint. Do not activate it as a classifier in Phase 4.

Only add a classifier adapter contract now. Real fine-tuning would require an
external clinician-reviewed dataset, which is outside this project setting.

## Fusion Rules

Fusion turns stage outputs into `SafetySignal` objects.

Allowed to trigger in shadow mode:

- category is in `RED_FLAG_RULES`
- assertion is `present`
- subject is `patient` or `care_subject`
- temporality is `current` or `recent`
- all required stages returned `success`

Special case:

- conditional `suicidal_ideation` remains safety-relevant.

Not allowed to trigger:

- negated symptoms
- remote symptoms
- other-person symptoms
- unknown category
- disabled/unavailable/timeout/invalid model stages

Important: in Phase 4, even accepted NLP positives are only recorded as
shadow-mode signals. They do not change acuity yet.

## Recommended Implementation Order

1. Keep `backend/app/safety_nlp/contracts.py` stable.
2. Keep `backend/app/safety_nlp/pipeline.py` model-free by default.
3. Add `requirements-safety-nlp.txt` with optional heavy dependencies.
4. Add a model manifest under `backend/models/safety/manifest.json`.
5. Add feature flags such as `CAREROUTE_SAFETY_NLP`.
6. Implement one real adapter at a time behind a feature flag.
7. Add fake-adapter unit tests for every branch before adding artifact-backed tests.
8. Add artifact-backed tests under a separate marker so normal CI never downloads models.
9. Record latency, availability, abstention and disagreement metrics.
10. Keep Safety deterministic behavior unchanged until Phase 6.

## How To Add A Real Adapter

All real adapters should use the common optional-adapter interface in
`backend/app/safety_nlp/contracts.py`:

- `AdapterConfig` stores model name, revision, enablement, artifact requirement, timeout, device and input length.
- `OptionalModelAdapter.availability_status(text)` returns `success`, `disabled`, `unavailable`, `timeout` or `invalid_output`.
- Stage-specific adapter methods convert that status into their own result type instead of raising through triage.

Use this pattern for every model-backed adapter:

```python
class RealXAdapter:
    def __init__(self, manifest_entry, *, enabled: bool, timeout_ms: int):
        self.enabled = enabled
        self.timeout_ms = timeout_ms
        self.model_name = manifest_entry["model"]
        self.model_revision = manifest_entry["revision"]
        self._model = None

    def load(self):
        if not self.enabled:
            return
        # Load model from local cache/artifact only.
        # Do not download at request time.

    def classify_or_translate_or_extract(...):
        if not self.enabled:
            return Result(status="disabled", ...)
        if self._model is None:
            return Result(status="unavailable", ...)
        try:
            # Run inference outside async event loop.
            return Result(status="success", ...)
        except TimeoutError:
            return Result(status="timeout", ...)
        except Exception:
            return Result(status="invalid_output", ...)
```

## Testing Strategy

Normal tests:

```bash
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_safety_nlp.py
```

These must pass without:

- internet
- Hugging Face downloads
- PyTorch
- transformers
- spaCy
- sentence-transformers

Future artifact-backed tests should be opt-in, for example:

```bash
pytest -m safety_nlp_models
```

Do not mix model downloads into ordinary `pytest`.

## What Success Looks Like

At the end of Phase 4, the pipeline can say:

```text
The deterministic Safety floor is unchanged.
The NLP pipeline can run in shadow mode.
Each stage has typed success/failure results.
Missing model artifacts do not break triage.
Benchmark metrics can be produced without logging patient text.
No NLP result changes acuity yet.
```
