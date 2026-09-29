# Safety-Override LLM and NLP Implementation Plan

## 1. Objective

Enhance the Safety-Override worker from an incomplete deterministic policy implementation into a bounded hybrid AI agent that:

- Preserves deterministic red-flag rules as an authoritative safety floor.
- Uses a staged local clinical NLP pipeline to detect symptoms, assertion context, ownership,
  temporality, paraphrases, and non-English red flags.
- Uses a bounded LLM adjudicator for uncertain semantic cases.
- Supports an educational prototype semantic-activation mode that demonstrates live AI-assisted
  escalation while remaining monotone, feature-flagged, and explicitly not clinically validated.
- Communicates explicitly with the Supervisor through the existing typed message bus.
- Understands assertion context such as negation, temporality, hypothetical statements, and the person experiencing the symptoms.
- Can only maintain or raise urgency; it can never lower acuity or clear a deterministic trigger in the initial release.
- Continues to operate safely when every model is disabled or unavailable.

The school-project release will optimize for local-first demonstration and the Singapore core
language scope: English, Mandarin Chinese, Malay, and Tamil. Licensing, clinical validation and
governance approvals are documented as real-world production blockers, not blockers for the
educational prototype demonstration.

## 2. Current State

The repository already contains part of the intended hybrid design:

- `backend/app/redflags.py` contains seven deterministic red-flag categories and the escalation-only `apply_override()` function.
- `backend/app/agents/safety.py` contains `SemanticRedFlagLayer`, a closed-vocabulary LLM prompt, response parsing, and semantic-result conversion.
- `SafetyOverrideAgent.areason()` invokes the shared LLM provider chain.
- `SafetyOverrideAgent.prescreen()` and `SafetyOverrideAgent.run()` remain implementation templates that raise `NotImplementedError`.
- The Supervisor invokes semantic reasoning before the synchronous Safety gate.
- Safety publishes `safety.override`, but the Supervisor does not subscribe to or act on that message.
- Safety subscribes to `acuity.classified`, but currently only records its inbox rather than making the message part of its decision contract.
- Existing automated tests disable LLM access and do not evaluate semantic Safety behavior.

The implementation must complete this existing foundation rather than replace it.

## 3. Safety Invariants

The following invariants are mandatory:

1. A deterministic red-flag match always remains effective.
2. No NLP or LLM result may clear a deterministic match in the initial release.
3. No channel may lower acuity.
4. Forced acuity must come from the authoritative deterministic rule table, never directly from a model response.
5. Models may only return categories from the closed `RED_FLAG_RULES` vocabulary.
6. Model failure, timeout, malformed output, disabled features, or missing artifacts must fall back to deterministic behavior.
7. A known deterministic red flag must not wait for network or model inference before escalation.
8. Raw patient text must not be included in message-bus or audit payloads.
9. Ambiguous model findings must result in either no additional trigger or clinician review, never autonomous de-escalation.
10. The global kill switch and independent Safety model feature flags must remain available.

## 4. Target Architecture

Use a local-first additive cascade. The deterministic gate runs first and a positive result is
effective immediately; model inference may continue for shadow evaluation but must not delay the
authoritative escalation:

```text
Patient text (original + Intake-normalized)
    |
    v
1. Deterministic red-flag rules
    | positive -> immediate authoritative escalation; do not wait for models
    v
2. Language routing
    | English -> direct English clinical pipeline
    | Mandarin / Malay / Tamil -> translation benchmark + direct multilingual baseline
    v
3. Per-mention clinical NLP
    | symptom NER -> assertion -> subject/temporality context
    v
4. Red-flag candidate mapping
    | medical similarity -> future fine-tuned multi-label classifier
    | high-confidence accepted finding -> additive escalation
    | uncertain / stage disagreement -> bounded LLM adjudication or HITL
    v
5. Bounded LLM classifier
    | positive -> additive escalation
    | negative/unavailable -> deterministic result remains
    v
Monotone merge -> final Safety result -> Supervisor gate
```

The effective trigger is:

```text
deterministic_positive OR high_confidence_nlp_positive OR llm_positive
```

Translation never replaces `CaseState.raw_text`, and translated text is never the only detection path.
For non-English input, compare translation-based English processing with a direct multilingual NLI
baseline; disagreement is an uncertainty signal, not permission to suppress a trigger.

The LLM is not asked to assign acuity, provide medical advice, or decide that a patient is safe. It only classifies whether the presentation maps to a known red-flag category and describes the context of that mention.

For the educational prototype, accepted high-confidence semantic findings may add an escalation or
force HITL review when `CAREROUTE_SAFETY_PROTOTYPE_SEMANTIC_ACTIVATION=1`. This mode must be
visibly labelled as a demo capability and must not be described as clinically validated or
production-approved.

## 5. Supervisor Communication

### 5.1 Required Conversation

Add an explicit request/response protocol:

```text
Supervisor
    |
    | safety.assessment.requested
    v
Safety-Override
    |
    | safety.override
    v
Supervisor
    |
    | verified safety gate
    v
Care-Routing -> HITL -> Reflection
```

The Supervisor must not begin Care-Routing until it has received and verified `safety.override` for the current request.

### 5.2 Supervisor Request

Add `safety.assessment.requested` to:

- `Supervisor.COMMS.publishes`
- `SafetyOverrideAgent.COMMS.subscribes`

The request is directed to `safety` and should contain no patient text:

```json
{
  "classifierAcuity": "P3_URGENT",
  "classifierConfidence": 0.48,
  "classifierMessageSeq": 2,
  "fastPathDetected": false
}
```

Safety must consume both `acuity.classified` and `safety.assessment.requested`. It should verify that the request and classifier announcement agree before producing its result.

### 5.3 Safety Response

Retain the existing `safety.override` intent and expand its payload:

```json
{
  "triggered": true,
  "rule": "breathlessness",
  "priorAcuity": "P3_URGENT",
  "forcedAcuity": "P1_RESUSCITATION",
  "channel": "nlp",
  "assertion": "present",
  "confidence": 0.94,
  "requiresHumanReview": true,
  "requestSeq": 3,
  "model": {
    "type": "huggingface",
    "name": "selected-model",
    "revision": "pinned-revision"
  }
}
```

Model evidence stored in the message must be sanitized. Raw patient passages stay in `CaseState` and must not enter the audited bus history.

### 5.4 Supervisor Verification

Make `Supervisor` consume messages and add `safety.override` to `Supervisor.COMMS.subscribes`. After Safety publishes its response, the Supervisor must verify:

- A response exists for the current request.
- `requestSeq` correlates to the current request.
- `priorAcuity` agrees with the classifier announcement.
- `forcedAcuity` is not less severe than `priorAcuity`.
- `rule` belongs to the closed red-flag vocabulary.
- A model has not supplied an arbitrary acuity code.
- Routing starts only after verification succeeds or a fail-safe is applied.

A missing or inconsistent response must:

- Produce an audit issue.
- Preserve the deterministic Safety result.
- Force clinician review.
- Never lower acuity.

This makes the message bus operationally load-bearing rather than only an audit stream.

## 6. Safety Signal Contract

Introduce a common structured representation for findings from all three channels. A `SafetySignal` should contain at least:

```text
channel             deterministic | nlp | llm
triggered           boolean
category            known red-flag category or null
assertion           present | possible | negated | conditional | unknown
temporality         current | recent | remote | unknown
subject             patient | care_subject | other_person | unknown
confidence          calibrated score where available
evidence            sanitized evidence reference
mention_id           stable in-memory identifier for the source mention
source_language      language of the original patient text
stage                deterministic | translation | ner | assertion | context | similarity | classifier | llm
model_name          model identifier or deterministic policy name
model_revision      pinned revision or policy version
latency_ms          channel latency
status              success | disabled | unavailable | timeout | invalid_output
```

Support multiple findings because one presentation may contain several red flags with different contexts. The final forced acuity remains the most severe authoritative acuity associated with an accepted category.

## 7. Local Clinical NLP Pipeline

### 7.1 Model and Tool Registry

The Safety NLP path is a staged pipeline, not a single model. Model IDs are selected here; exact
Hugging Face revisions, tokenizer revisions and artifact hashes remain `TBD` until the artifacts are
reviewed and pinned. Do not use an unpinned `main` revision in a release.

| Stage | Candidate | Intended role | Constraints and release position |
|---|---|---|---|
| Symptom extraction | `d4data/biomedical-ner-all` | Extract candidate symptom and biomedical-entity spans | English, 66.4M parameters, Apache-2.0, trained on MACCROBAT case reports. Prototype candidate; validate on colloquial patient text. |
| Assertion | `bvanaken/clinical-assertion-negation-bert` | Classify one marked entity as `PRESENT`, `ABSENT` or `POSSIBLE` | English, approximately 100M parameters, trained on 2010 i2b2 assertion data. It does not determine subject, temporality or conditionality. |
| Subject and temporality | medspaCy ConText | Apply rules for patient/care-subject/other-person and current/recent/remote context | MIT library, but bundled ConText rules are primarily English. Add and clinically review CareRoute-specific rules after translation. |
| Translation candidate A | `facebook/nllb-200-distilled-600M` | Translate Mandarin, Malay and Tamil to an English working copy | CC-BY-NC-4.0; research-only, general-domain, not intended for medical or production use, and trained for inputs up to 512 tokens. Allowed only for labelled educational prototype evaluation/activation in this project unless licensing and governance approve real-world use. |
| Translation candidate B | `google/madlad400-3b-mt` | Benchmark alternative to NLLB | Apache-2.0 but 3B parameters (about 11.8 GB unquantized; quantized variants require separate validation). General-domain and not assessed for clinical production use. |
| Medical similarity | `FremyCompany/BioLORD-2023` | Rank synthetic red-flag examples for each mention in this project; clinician-approved examples would be required before activation claims | English, approximately 100M parameters and 768-dimensional embeddings. UMLS/SNOMED-derived training data creates IHTSDO/NLM licensing obligations that must be approved before activation. |
| Future category classifier | Fine-tuned `emilyalsentzer/Bio_ClinicalBERT` | Multi-label classification into the seven `RED_FLAG_RULES` categories | English, MIT base encoder trained on MIMIC notes. The published checkpoint is a fill-mask/base encoder, not a ready red-flag classifier; it requires externally clinician-reviewed fine-tuning data before any real-world activation. |
| Direct multilingual baseline | `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli` | Detect category entailment without relying solely on translation | Retain as the direct multilingual comparator for all four target languages. |
| General similarity baseline | `sentence-transformers/LaBSE` | Compare general multilingual similarity against translated BioLORD processing | Benchmark comparator, not the preferred medical similarity model. |

### 7.2 Pipeline Stages

Expose one least-privilege tool contract:

```text
safety_nlp.classify(text, language) -> list[SafetySignal]
```

Internally, use typed stage results (`TranslationResult`, `MentionSpan`, `AssertionResult`,
`ContextResult`, `CategoryCandidate`, `SafetySignal`) and process each mention independently:

1. Run the deterministic red-flag gate first. A positive result becomes effective immediately and
   skips blocking NLP/LLM work; optional shadow inference must happen out of the critical path.
2. Route English directly to the clinical pipeline. For Mandarin, Malay and Tamil, benchmark NLLB
   and MADLAD separately and retain the original text alongside the translated in-memory working copy.
3. Run `d4data/biomedical-ner-all` on English text and bridge its token spans into spaCy entities.
4. Mark each entity with `[entity]` tokens and classify assertion with
   `bvanaken/clinical-assertion-negation-bert`.
5. Apply CareRoute medspaCy ConText rules for subject, care subject, temporality and conditional
   context. The assertion model must not be treated as supplying these labels.
6. Rank synthetic category examples with `FremyCompany/BioLORD-2023` for prototype benchmarking;
   compare its recall-at-k with LaBSE and direct multilingual NLI. Do not describe these examples as
   clinician-approved in this project.
7. Add a fine-tuned `Bio_ClinicalBERT` multi-label classifier only after sufficient externally
   clinician-reviewed data exists. Until then, its stage is disabled rather than simulated.
8. Fuse stage outputs conservatively. Accepted categories map back to authoritative
   `RED_FLAG_RULES`; ambiguous context, translation disagreement or model disagreement goes to the
   bounded LLM or HITL.

For translated cases, evidence references must point back to an original-language `mention_id`.
Original or translated patient passages remain in memory only and must not enter bus, audit or
application-log payloads.

### 7.3 Conservative Fusion Policy

- Deterministic triggers remain active regardless of every downstream model result.
- Models return categories and context only; arbitrary model-supplied acuity values are ignored.
- `negated`, `remote`, `other_person` and ambiguous findings may prevent a new semantic trigger but
  may not clear a deterministic trigger.
- `possible`, `unknown`, translation disagreement and cross-model disagreement default to LLM
  adjudication or clinician review rather than autonomous suppression.
- Conditional self-harm remains safety-relevant through an explicit category policy.
- Translation is never the sole path: compare it with direct multilingual NLI and preserve the
  deterministic floor.
- A disabled, unavailable, timed-out or malformed stage returns a typed status and does not raise
  through the triage pipeline.

### 7.4 Translation Benchmark and Selection

Benchmark both `facebook/nllb-200-distilled-600M` and `google/madlad400-3b-mt` before selecting a
default. Run them one at a time rather than loading both into the service. Selection criteria are:

- Critical symptom and red-flag concept preservation.
- Negation, polarity, subject and temporality preservation.
- End-to-end red-flag recall and specificity after translation.
- Results independently for Mandarin, Malay and Tamil, including code-switched input.
- CPU/GPU latency, peak memory, model size and quantization impact.
- License suitability and clinical/governance approval for real-world production; for this school
  project, unresolved approvals are documented and the feature is labelled educational prototype use.

NLLB may be used for non-commercial educational prototype evaluation/activation in this project but
must not be described as production-ready. MADLAD's Apache-2.0 license does not remove the need for
clinical-domain validation before any real-world deployment.

### 7.5 Production Requirements

- Pin every model ID, revision, tokenizer revision, license and artifact SHA-256 in a versioned
  Safety model manifest.
- Prefer safetensors where available and prohibit runtime model downloads in release environments.
- Validate all four target languages independently.
- Load only selected/activated stages and warm them at application startup; benchmark candidates
  load one at a time.
- Keep CPU-bound inference outside the async event loop with `asyncio.to_thread()` or an executor.
- Configure model cache, device, per-stage timeout, total pipeline budget and maximum input length.
- Add `CAREROUTE_SAFETY_NLP` plus stage-specific enablement/model settings; the global kill switch
  takes precedence.
- Fall back without raising if dependencies, models or cache artifacts are unavailable.
- Consider ONNX Runtime or quantized inference only after accuracy-equivalence tests pass.

### 7.6 Later Fine-Tuning

Do not fine-tune `Bio_ClinicalBERT` during the first NLP implementation. Fine-tuning begins only
after a sufficiently large externally clinician-reviewed dataset exists, which is outside this project
setting. Use multi-label output because one presentation may contain multiple red flags, preserve a
patient/session-independent holdout, record training lineage, and keep the classifier in shadow mode
until every release gate passes.

## 8. Bounded LLM Adjudication

Strengthen the existing `SemanticRedFlagLayer` rather than replacing it.

The LLM should be called only when:

- Deterministic rules did not trigger; and
- The local NLP model is uncertain, reports an ambiguous context, or produces a candidate requiring adjudication.

Use a strict schema, preferably validated with Pydantic:

```json
{
  "triggered": true,
  "category": "stroke_signs",
  "assertion": "present",
  "temporality": "current",
  "subject": "patient",
  "confidence": 0.91,
  "evidenceSpan": "face drooping and speech is slurred",
  "rationale": "The text describes current FAST stroke signs."
}
```

Validation requirements:

- Reject categories outside `RED_FLAG_RULES`.
- Validate `evidenceSpan` against the input instead of accepting fabricated evidence.
- Do not treat model-reported confidence as calibrated probability.
- Clear stale semantic state before each new assessment.
- Distinguish no finding from disabled, timeout, provider failure, parse failure, and invalid output.
- Prefer a local Ollama provider for Safety, with cloud LLMs as an optional fallback.
- Give Safety a provider order that can be configured independently of other agents.

RAG is not required for red-flag detection. Retrieval may support citations later, but it should not sit in the critical detection path.

## 9. Assertion-Context End Goal

The end goal is to answer:

> Does the text describe a red flag that is currently relevant to the person being triaged?

Context labels are not automatic suppression rules:

- `I had a seizure ten years ago` differs from `I had a seizure five minutes ago`.
- `If this continues, I might kill myself` remains an urgent safety concern despite conditional wording.
- `My child cannot breathe` may describe the actual care subject.
- `I have no chest pain, but I cannot breathe` negates one category while asserting another.

For the first release, context may prevent NLP or LLM from adding an unsupported semantic trigger, but it may not clear a deterministic trigger. Deterministic/context conflicts must be recorded for evaluation and sent to HITL.

## 10. Context Benchmark

### 10.1 Dataset

Create:

```text
backend/tests/fixtures/safety_context_cases.json
```

Keep this separate from `triage_vignettes.json`, which evaluates the deterministic no-LLM pipeline.

Suggested row schema:

```json
{
  "id": "negated-chest-pain-en-01",
  "language": "en",
  "text": "I do not have chest pain, only a mild sore throat.",
  "referenceTranslation": null,
  "mentions": [
    {
      "mentionId": "m1",
      "text": "chest pain",
      "start": 14,
      "end": 24,
      "entityType": "Sign_symptom"
    }
  ],
  "category": "cardiac_chest_pain",
  "assertion": "negated",
  "subject": "patient",
  "temporality": "current",
  "goldTrigger": false,
  "regexTriggers": true,
  "goldAction": "do_not_add_semantic_trigger",
  "rationale": "Explicit denial of current chest pain."
}
```

For this project setting, the dataset is a synthetic benchmark scaffold rather than a clinically
validated corpus. Include balanced positive and hard-negative examples across all seven categories
and all four target languages. Non-English rows should carry synthetic English reference
translations and mention-level alignment sufficient to test whether translation-preservation labels
exist for the critical symptom, negation, subject and temporality. Because clinician review is not
available, this benchmark must not be described as clinician-approved, clinically validated, or
sufficient for real-world deployment approval. Development and untouched holdout partitions must be
patient/session independent.

### 10.2 Explicit Benchmark Categories

#### Negation

- Direct denial of a red flag.
- Partial or uncertain denial.
- One category negated while another is asserted.
- Double negation and colloquial negation.

#### Historical Symptoms

- Remote resolved events.
- Recent events that remain urgent.
- Recurrent conditions without a current episode.
- Unknown timing that requires clinician review.

#### Hypothetical and Conditional Statements

- Generic informational statements.
- Fear of a future event.
- Conditional self-harm statements that remain safety-relevant.
- Instructions or example text pasted into the symptom field.

#### Person and Care Subject

- Family medical history.
- A current emergency involving another person.
- A caregiver entering symptoms for the actual patient.
- Comparison statements involving a family member.

#### Compositional Cases

- Negated red flag plus a different active red flag.
- Historical event plus new active symptoms.
- Multiple categories with different assertion contexts.
- Code-switched, colloquial, and voice-transcription variants.

### 10.3 Dataset Dimensions

Each row should label:

| Dimension | Values |
|---|---|
| Assertion | `present`, `possible`, `negated`, `conditional`, `unknown` |
| Temporality | `current`, `recent`, `remote`, `unknown` |
| Subject | `patient`, `care_subject`, `other_person`, `unknown` |
| Category | One of the seven existing categories, or null |
| Language | English, Mandarin Chinese, Malay, Tamil |
| Mention spans | Original-language offsets, entity type, and stable `mentionId` |
| Reference translation | Synthetic English text for non-English rows; not clinician-approved |
| Translation preservation | Symptom, negation/polarity, subject, and temporality preserved or lost |
| Gold action | `escalate`, `do_not_add`, `clinician_review` |
| Regex result | `triggered`, `not_triggered` |
| Semantic result | Expected category, context, and acceptable confidence range |

### 10.4 Stage-Level Metrics

Report both end-to-end Safety performance and the failure location inside the NLP pipeline:

- Symptom NER precision, recall and F1 overall and by entity type/language path.
- Assertion macro F1 for `PRESENT`, `ABSENT` and `POSSIBLE`.
- Subject and temporality macro F1 for the CareRoute medspaCy rules.
- Translation critical-concept recall and negation/polarity flip rate.
- Translation preservation of subject and temporality.
- BioLORD and LaBSE category recall-at-k and ranking quality.
- Direct multilingual NLI versus translation-pipeline agreement.
- Future Bio_ClinicalBERT multi-label micro/macro F1 and per-category recall.
- End-to-end active red-flag recall, specificity, abstention/HITL rate and latency.
- Ablation results showing the contribution of translation, NER, context, similarity and classifier stages.

Stage thresholds can be reported for prototype comparison only. In this project setting they cannot be
treated as clinically approved release thresholds because clinician review is unavailable. End-to-end
Safety gates remain the engineering authority for the prototype; a strong component score cannot
compensate for a red-flag recall regression.

## 11. Evaluation Modes

### 11.1 Educational Prototype Additive Gate

This is the school-project live-demo behavior when prototype semantic activation is enabled:

- Deterministic positives remain authoritative.
- Context may stop NLP or LLM from adding a false semantic trigger.
- Context cannot clear a deterministic trigger.
- Conflicts are recorded as `deterministic_context_conflict`.
- Conflict cases require clinician review.
- High-confidence NLP or bounded LLM positives may add an escalation using only authoritative
  `RED_FLAG_RULES` mappings.
- Uncertain semantic positives may force HITL review without forcing acuity.

Proposed acceptance criteria:

- Deterministic red-flag recall remains `1.0` on the existing regression set.
- Zero acuity-lowering outcomes.
- Active semantic red-flag recall is at least `0.95` overall and by supported language.
- Context classification macro F1 is at least `0.90`.
- Semantic specificity is at least `0.90`.
- End-to-end HITL specificity remains at least the existing `0.80` floor.
- Every model result includes category, assertion, subject, temporality, confidence, and provenance.
- Disabled or unavailable models produce deterministic-only behavior.

Thresholds introduced here are proposed demo thresholds. They justify educational prototype behavior
only. They must not be used as clinical release thresholds without an external clinical-review process
that is out of scope for this project.

Phase 6 currently uses conservative provisional intervention thresholds of `0.85` for NLP and `0.85`
for bounded LLM findings, plus `0.60` for review-only uncertain findings. They are environment
overridable through `CAREROUTE_SAFETY_NLP_ACTIVATION_THRESHOLD`,
`CAREROUTE_SAFETY_LLM_ACTIVATION_THRESHOLD`, and
`CAREROUTE_SAFETY_SEMANTIC_REVIEW_THRESHOLD`. These values remain provisional until a versioned
artifact-backed benchmark calibrates them; activation stays disabled by default.

### 11.2 Shadow Suppression Evaluation

Calculate what would happen if a context finding could clear a deterministic regex result, but do not change live acuity.

Measure:

- Potential false escalations avoided.
- Potential true emergencies suppressed.
- Results by red-flag category and language.
- NLP/LLM disagreement.
- Model/clinician disagreement.
- Confidence calibration.
- Cases assigned to clinician review.

### 11.3 Future Suppression Gate

Suppression may leave shadow mode only if:

- A clinician approves the suppression policy.
- No true emergency is suppressed in the safety holdout.
- Per-language acceptance criteria pass independently.
- Explicit deterministic context logic and model inference agree.
- Ambiguous temporality or subject always routes to HITL.
- The behavior has a separate feature flag and immediate rollback path.
- The deterministic safety guarantee, Model Card, governance documents, and user-facing claims are revised explicitly.

Model-only suppression is out of scope and is not recommended.

## 12. Observability and Audit

Add metrics and audit fields for:

- Trigger channel: deterministic, NLP, LLM, or multiple.
- Semantic-only detections.
- Deterministic/context conflicts.
- NLP/LLM disagreements.
- Translation/direct-multilingual disagreements and selected translation candidate.
- Per-stage abstention, failure and disagreement counts.
- Model failures by status and provider.
- Per-channel and per-NLP-stage latency, plus peak-memory measurements in offline benchmarks.
- Per-language recall and specificity in offline evaluation.
- False-escalation and clinician-override rates.
- Model name, revision, and policy version.

Prompt and patient content must remain excluded from logs. Audit records should carry sanitized findings and provenance only.

## 13. Tests and Release Gates

### 13.1 Deterministic Tests

- Implement and test `prescreen()`.
- Implement and test monotone `run()` behavior.
- Preserve all existing `test_redflags.py` guarantees.
- Add tests that Reflection reapplication does not duplicate model inference.
- Add no-model and kill-switch regression tests.

### 13.2 Semantic Unit Tests

- Prompt construction.
- Strict schema parsing.
- Unknown-category rejection.
- Evidence-span validation.
- Confidence threshold behavior.
- Semantic-only matches.
- Multiple findings and most-severe selection.
- Disabled, timeout, malformed JSON, and provider-error states.
- Stale semantic-state clearing.
- Prompt-injection attempts embedded in patient text.

### 13.3 Communication Tests

- Supervisor sends `safety.assessment.requested` directly to Safety.
- Safety consumes the request and `acuity.classified` announcement.
- Safety emits a correlated `safety.override` response.
- Supervisor consumes the response before routing.
- Missing responses force HITL.
- Inconsistent or acuity-lowering responses are rejected.
- Raw patient text never appears in message payloads.
- Ordered sequence numbers and audit history remain deterministic.

### 13.4 Benchmark Tests

- Symptom-span extraction precision, recall, F1, and offset integrity.
- Assertion classification for present, absent, and possible mentions using required entity markers.
- CareRoute medspaCy subject, care-subject, temporality, and conditional rules.
- NLLB versus MADLAD clinical-concept and context preservation by language.
- BioLORD versus LaBSE and direct multilingual NLI candidate recall-at-k.
- Future fine-tuned Bio_ClinicalBERT multi-label category performance and calibration.
- Category performance by language.
- Assertion, temporality, and subject classification.
- Active red-flag recall and specificity.
- Compositional-context handling.
- Deterministic/context conflict attribution.
- Additive mode and shadow suppression metrics.
- Latency and model-unavailability degradation.
- Model-free unit tests must use fake adapters; artifact-backed benchmarks must be separately marked
  and must use a pre-populated offline cache rather than downloading models during tests.

### 13.5 CI Correction

Ensure the advertised Safety regression job directly selects the Safety, red-flag, and relevant pipeline tests. Do not rely only on a broad `-k` expression that may omit the required red-flag cases.

## 14. Benchmark Artifacts and Governance

Adding Hugging Face models changes the Safety worker's reproducibility and governance scope, but this
project no longer requires MLflow for Safety NLP. Safety NLP benchmark tracking should use local,
versioned artifacts committed or published by CI instead of an experiment-tracking server.

The live deterministic Safety decision path must never depend on benchmark tracking or artifact
publication. A missing tracking artifact can block a prototype release check, but it cannot delay
triage or weaken the deterministic rule floor.

### 14.1 Safety NLP Benchmark Artifacts

For each benchmark run, write sanitized aggregate outputs under a local artifact directory such as
`backend/reports/safety_nlp/` or the CI job's artifact directory.

Record only configuration, aggregate metrics and sanitized artifacts:

| Artifact item | Safety NLP content |
|---|---|
| Configuration | Pipeline version; enabled stages; model/tokenizer IDs and revisions; translation candidate; thresholds; maximum lengths; device; quantization/ONNX mode; medspaCy rule version; red-flag policy version; LLM prompt/provider version for Phase 5 |
| Lineage | Git commit SHA; benchmark dataset SHA-256; split ID; model/tokenizer artifact SHA-256; license identifier; execution environment; deterministic policy hash |
| Metrics | Per-language and per-category recall/specificity; NER F1; assertion/context macro F1; translation concept recall and polarity-flip rate; similarity recall-at-k; classifier multi-label F1/calibration; disagreement/abstention rates; HITL rate; latency and peak memory |
| Artifacts | Safety model manifest; aggregate benchmark JSON; confusion matrices; per-language summaries; threshold report; release-gate result; model-card draft; AI-BOM fragment; sanitized failure taxonomy |

Never write patient text, translated text, prompts, model responses, evidence spans, mention text, case
IDs, session IDs or row-level benchmark content to benchmark artifacts. Dataset rows remain in the
versioned benchmark; artifacts receive only the dataset hash, split identifier and aggregate results.
Add an automated privacy test that inspects generated files for forbidden content.

### 14.2 Reproducibility and Lineage

- Pin model and tokenizer revisions and store artifact integrity hashes.
- Record model license, source, base model and training dataset provenance.
- Version the benchmark dataset, split assignment, medspaCy rules, red-flag policy, thresholds and LLM
  prompt independently.
- Record the exact component IDs and artifact hashes in `backend/models/safety/manifest.json`; the
  manifest hash links benchmark metrics to the model files used.
- For future Bio_ClinicalBERT fine-tuning, log seed, hyperparameters, label schema, class weights,
  train/validation/holdout hashes and the resulting checkpoint hash.
- Export benchmark and release-gate summaries as durable CI artifacts so verification does not depend
  on a tracking server.

### 14.3 Model Selection and Rollback Without MLflow

Do not use an MLflow registry for Safety NLP. The selected shadow-mode pipeline is identified by:

- the committed Safety model manifest,
- local/offline model artifact hashes,
- the benchmark dataset hash,
- the release-gate JSON artifact,
- the code commit SHA.

Runtime loading resolves a deployment-pinned manifest path or explicit model IDs from configuration,
never an unqualified `latest` model. Rollback means restoring the prior manifest/configuration and
disabling additive activation without changing deterministic rules.

For any future fine-tuned classifier, do not advertise the unfine-tuned
`emilyalsentzer/Bio_ClinicalBERT` base encoder as a Safety classifier.

### 14.4 Operational and Governance Requirements

- Keep benchmark artifact generation optional for local deterministic tests and absent from live
  inference.
- Require successful manifest/hash/release-gate artifact generation before a Safety NLP prototype
  release can be accepted in CI.
- Add every activated model and tokenizer to the AI-BOM.
- Add a Safety model card or extend the existing Model Card clearly.
- Monitor language/category drift and clinician disagreement where enough live labels exist, writing
  aggregate monitoring artifacts without patient content.
- Revisit the capability test that currently requires only the Severity-Classifier to use a trained model.
- Add a Safety-specific evaluation to `backend/app/evals/plan.py`.
- Update README, architecture, security and capability documentation consistently.

## 15. Phased Delivery

Phase 3 builds the typed contracts and synthetic context benchmark needed to exercise model-evaluation
plumbing, but it does not create a clinically validated dataset and does not activate a model pipeline.
Clinical review is unavailable in this project setting, so any NLP/LLM benchmark numbers remain
prototype evidence only. **Local NLP implementation starts in Phase 4** and remains in shadow mode.
**Bounded LLM adjudication implementation starts in Phase 5**, strengthening the existing
`SemanticRedFlagLayer` scaffold and invoking it only for uncertain NLP cases without a deterministic
hit. Additive model-driven escalation must not be represented as clinically approved without an
external review process beyond this project.

### Phase 1: Restore the Safety Baseline

- Implement `SafetyOverrideAgent.prescreen()`.
- Implement deterministic and monotone `SafetyOverrideAgent.run()`.
- Record `prior_acuity_code` on every call.
- Add signed Safety explanation contributions without duplication.
- Restore Safety, contract, pipeline, and API tests.

Exit criteria: the deterministic pipeline is fully functional and all existing Safety guarantees pass without an LLM.

### Phase 2: Supervisor Request/Response

- Add `safety.assessment.requested`.
- Make Safety consume the request and classifier announcement.
- Expand `safety.override`.
- Make Supervisor consume and verify the response.
- Gate routing on successful response verification or fail-safe HITL.
- Add communication and pipeline-order tests.

Exit criteria: removing or corrupting the Safety response changes Supervisor behavior and forces the fail-safe, proving the bus is load-bearing.

### Phase 3: Context Benchmark

- Define and validate the dataset schema.
- Create synthetic, non-clinically-validated cases across categories, contexts, and languages.
- Add deterministic and model benchmark runners.
- Add Safety evaluation specifications and metrics.

Exit criteria: baseline regex and semantic candidates can be compared on an untouched synthetic holdout
set for prototype evaluation only. The benchmark is explicitly not clinician-approved and cannot be used
as real-world deployment evidence.

### Phase 4: Local NLP Benchmark and Prototype-Ready Mode

- Implement a narrow `safety_nlp.classify` tool.
- Implement typed translation, mention, assertion, context, candidate and signal results.
- Benchmark NLLB and MADLAD separately for Mandarin, Malay and Tamil; do not load both in service.
- Implement `d4data/biomedical-ner-all` symptom extraction and the transformer-span to spaCy bridge.
- Implement entity-marked assertion inference with `bvanaken/clinical-assertion-negation-bert`.
- Add CareRoute medspaCy ConText rules for subject and temporality in shadow mode; mark them as
  not clinically reviewed in this project setting.
- Benchmark BioLORD medical similarity against LaBSE and direct multilingual NLI.
- Defer fine-tuned Bio_ClinicalBERT activation until externally clinician-reviewed training data exists.
- Select and pin the minimum model set justified by prototype benchmark performance, latency,
  hardware and documented license limitations; do not claim clinical performance without external
  review.
- Add startup loading, inference isolation, feature flags, typed failure statuses and telemetry.
- Track pipeline benchmarks and component comparisons as sanitized local/CI artifacts.
- Identify the selected prototype pipeline by manifest hash, dataset hash, release-gate artifact and commit SHA.
- Record all model findings in shadow mode by default, and prepare them for Phase 6 monotone
  prototype activation behind an explicit feature flag.

Exit criteria: the selected staged NLP pipeline meets prototype per-stage, context and multilingual
thresholds; translation and licensing decisions are documented; model output changes live acuity only
when the Phase 6 educational prototype activation flag is enabled.

### Phase 4.5: Live NLP Runtime and Natural-Uncertainty Handoff

The Phase 4 contracts, adapters, manifests and benchmark wrappers are not sufficient by themselves:
the live Safety path must construct and use a model-backed adapter bundle rather than silently falling
back to model-free test adapters. This phase makes the Phase 5 “uncertain NLP case” condition real for
the educational demo. It does not activate semantic findings or change acuity.

- Construct the selected local `SafetyNlpRuntime` from the pinned manifest during FastAPI startup,
  warm it outside the event loop, and degrade to typed unavailable status if a local artifact cannot load.
- Build the live adapter bundle from the selected local NER, assertion, context, translation and category
  components. Keep `fake_adapters()` limited to unit tests and explicit development harnesses.
- Pass the live adapter bundle into `safety_nlp.classify()` from `SafetyOverrideAgent.shadow_nlp()`.
- Run `shadow_nlp()` in the orchestrator immediately before `SafetyOverrideAgent.areason()`.
- Use a single telemetry vocabulary (`uncertaintyCount` and `disagreementCount`) and route either value,
  ambiguous assertion/subject/temporality, or typed NLP failure to bounded LLM adjudication.
- Add a default-off development-only forced-uncertainty switch for repeatable browser/API integration
  testing. It may add sanitized telemetry only; it must not inject a clinical category or alter acuity.
- Add API/SSE integration tests proving a naturally ambiguous synthetic paraphrase reaches the LLM through
  the real frontend -> proxy -> backend -> orchestrator path, and that the resulting semantic record is
  sanitized and shadow-only.

Exit criteria: with the development forcing switch disabled, at least one synthetic ambiguous paraphrase
produces model-derived uncertainty or disagreement and invokes the bounded LLM through the live API path.
The final acuity remains unchanged until Phase 6 activation. This is an educational-demo capability;
external clinical or production approval is not a project blocker and no production claim is made.

### Phase 5: Bounded LLM Adjudication

- Upgrade `SemanticRedFlagLayer` to the common signal schema.
- Add strict response validation and evidence checks.
- Invoke only for uncertain NLP cases without deterministic hits.
- Prefer local Ollama and retain optional provider fallback.
- Log aggregate LLM evaluation metrics and prompt/provider version identifiers to sanitized local/CI
  artifacts without prompts, responses, evidence spans or patient text.
- Add direct LLM-path and adversarial tests.

Exit criteria: the hybrid path passes benchmark, security, latency, and failure-mode tests.

### Phase 6: Educational Prototype Additive Activation

- Enable high-confidence NLP and accepted LLM positives to add triggers.
- Keep deterministic/context conflicts in HITL.
- Monitor false escalations, disagreements, clinician overrides, and per-language results.
- Preserve independent model kill switches.
- Gate all semantic intervention behind `CAREROUTE_SAFETY_PROTOTYPE_SEMANTIC_ACTIVATION`.
- Label additive activation as educational prototype behavior in logs, API/debug metadata and docs.
- Keep additive activation prototype-only; any real deployment would require external release,
  clinical, licensing, governance and champion-versus-challenger gates. Rollback restores the prior
  manifest and disables semantic activation.

Exit criteria: live-demo evaluation preserves deterministic recall and HITL specificity while showing
semantic-only red-flag recall improvements under the prototype activation flag.

### Phase 7: Suppression Review, Optional

- Run shadow suppression for a meaningful evaluation period.
- Review all potential suppressed emergencies with a clinician.
- Decide whether a narrowly scoped deterministic context policy is justified.
- Keep model-only suppression prohibited.

Exit criteria: explicit clinical and governance approval, all future suppression gates satisfied, and immediate rollback available.

## 16. File-Level Work Breakdown

Expected primary changes:

| File | Scope |
|---|---|
| `backend/app/agents/safety.py` | Complete baseline; consume messages; NLP/LLM signals; monotone merge; expanded response |
| `backend/app/redflags.py` | Preserve authoritative rule mapping; add policy versioning and any separately approved deterministic context logic |
| `backend/app/agents/supervisor.py` | Request/response protocol; response consumption and verification; route gate |
| `backend/app/agents/messaging.py` | Correlation helper only if needed; preserve typed ordered bus |
| `backend/app/agents/base.py` | Structured Safety state fields if not kept inside a dedicated result object |
| `backend/app/agents/reasoning.py` | Typed failure statuses and stricter reasoning result handling |
| `backend/app/safety_nlp/` | Typed NLP facade and adapters for translation, NER, assertion, context, similarity, classification, model manifest and fallback |
| `backend/app/safety_nlp/benchmark.py` | Offline staged benchmark runner and release-gate summary generation |
| `backend/app/safety_nlp/artifacts.py` | Privacy-safe aggregate benchmark artifacts and lineage summaries; never imported by live inference |
| `backend/app/config.py` | NLP and Safety-specific LLM flags, model revision, thresholds, and timeouts |
| `backend/app/evals/plan.py` | Safety semantic/context evaluation specification |
| `backend/tests/fixtures/safety_context_cases.json` | Multilingual synthetic context benchmark; explicitly not clinically validated |
| `backend/tests/agents/test_safety.py` | Agent behavior, NLP/LLM parsing, monotone merge |
| `backend/tests/agents/test_comms.py` | Supervisor/Safety request-response and fail-safe tests |
| `backend/tests/test_pipeline.py` | End-to-end order, gating, and failure behavior |
| `backend/tests/test_api_e2e.py` | SSE and final payload behavior |
| `backend/tests/test_eval_safety.py` | Offline benchmark and acceptance gates |
| `backend/requirements-safety-nlp.txt` | Optional heavy dependencies (`transformers`, PyTorch/ONNX Runtime, sentence-transformers, spaCy/medspaCy, sentencepiece) pinned separately from the deterministic runtime |
| `backend/models/safety/manifest.json` | Selected model IDs, revisions, tokenizer revisions, licenses, hashes and activation status; no model weights committed to Git |
| `backend/.env.example` | New feature flags and model configuration |
| `backend/MODEL_CARD.md` | Safety model intended use, metrics, limits, and provenance |
| `.gitlab-ci.yml` | Safety NLP benchmark and artifact publication jobs; no MLflow or registry integration required |
| `README.md` and security/governance docs | Consistent architecture and safety claims |

## 17. Implementation Checklist

Use this checklist as the delivery tracker. Complete Phases 1 through 6 for the initial release. Phase 7 remains optional and requires separate clinical and governance approval.

### Phase 1: Deterministic Safety Baseline

- [x] Implement non-mutating, deterministic-only `SafetyOverrideAgent.prescreen()`.
- [x] Make `prescreen()` enforce access to `redflags.evaluate` and degrade to `False` on failure.
- [x] Add a permission test proving `prescreen()` raises `ToolAccessError` when `redflags.evaluate` is absent from Safety's allow-list.
- [x] Add an evaluator-failure test proving `prescreen()` returns `False` when `redflags.evaluate` raises unexpectedly.
- [x] Add a regression test proving `prescreen()` never invokes the semantic layer or an LLM.
- [x] Implement synchronous `SafetyOverrideAgent.run()`.
- [x] Evaluate both raw and normalized symptom text without dropping either source.
- [x] Record `state.prior_acuity_code` on every invocation.
- [x] Merge deterministic and existing semantic findings monotonically.
- [x] Derive every forced acuity from `RED_FLAG_RULES`.
- [x] Update `state.safety_triggered`, `safety_rule`, and `safety_reason` consistently.
- [x] Append one signed Safety explanation contribution when an override fires.
- [x] Prevent duplicate Safety explanation contributions during Reflection reruns.
- [x] Return every key required by `SafetyOverrideAgent.CONTRACT`.
- [x] Add deterministic hit, no-hit, multiple-hit, and never-lower-acuity tests.
- [x] Verify `pytest -m safety`, `pytest -m contract`, and existing red-flag tests pass without model access.

### Phase 2: Supervisor and Safety Communication

Current implementation status: Safety can receive and validate the planned request and emit an enriched response. Supervisor/platform integration is now in place for the directed request, response verification, route gate, fail-safe handling, and pipeline message-order tests. Full bus-wide/audit-wide patient-text exclusion remains a follow-up because the pre-existing classifier announcement can still include `state.evidence`.

- [x] Add the `safety.assessment.requested` message intent.
- [x] Add `safety.assessment.requested` to `Supervisor.COMMS.publishes`.
- [x] Add `safety.assessment.requested` to `SafetyOverrideAgent.COMMS.subscribes`.
- [x] Make the Supervisor publish a directed Safety request after classification.
- [x] Exclude raw and normalized patient text from the request payload.
- [x] Make Safety consume both `acuity.classified` and `safety.assessment.requested`.
- [x] Validate agreement between the request, classifier announcement, and current state.
- [x] Add request-sequence correlation to the Safety response.
- [x] Expand `safety.override` with channel, context, confidence, review requirement, and model provenance.
- [x] Add `safety.override` to `Supervisor.COMMS.subscribes`.
- [x] Make the Supervisor consume the Safety response before Care-Routing starts.
- [x] Validate category membership and escalation-only acuity in the Supervisor.
- [x] Force HITL and audit an issue when the response is missing, stale, or inconsistent.
- [x] Preserve the deterministic result on every communication failure.
- [x] Add request delivery, response delivery, correlation, and route-gating tests.
- [x] Add a test proving that removing the response changes behavior and activates the fail-safe.
- [ ] Add a test proving no patient text is present in message or audit payloads. Partial: Safety request/response payloads are covered; full bus-wide/audit-wide coverage remains pending because `acuity.classified` can include classifier evidence.
- [x] Update pipeline message-order assertions.

### Phase 3: Safety Signal and Context Benchmark

- [x] Define a typed `SafetySignal` contract shared by deterministic, NLP, and LLM channels.
- [x] Support multiple signals for presentations containing multiple red flags.
- [x] Define assertion, temporality, subject, channel, and failure-status enums or validated literals.
- [x] Add model and policy version fields to Safety results.
- [x] Create `backend/tests/fixtures/safety_context_cases.json`.
- [x] Add schema validation for every benchmark row.
- [x] Add active and negated examples for all seven red-flag categories.
- [x] Add current, recent, remote, and unknown-temporality examples.
- [x] Add patient, care-subject, other-person, and unknown-subject examples.
- [x] Add hypothetical and conditional examples, including safety-relevant self-harm cases.
- [x] Add compositional cases with different contexts for different symptoms.
- [x] Add colloquial, code-switched, and voice-transcription variants.
- [x] Cover English, Mandarin Chinese, Malay, and Tamil independently.
- [x] Label original-language symptom spans with stable mention IDs and offsets.
- [x] Add synthetic English reference translations for every non-English row.
- [x] Label whether translation preserved symptoms, negation/polarity, subject, and temporality.
- [x] Balance active positives with benign and context-based hard negatives.
- [x] Separate development and untouched holdout sets.
- [x] Mark the benchmark as not clinically validated because clinician review is unavailable in this project setting.
- [x] Document that clinical approval is out of scope and required before using this benchmark for real-world deployment or additive activation approval.
- [x] Add a Safety-specific `EvalSpec` to `backend/app/evals/plan.py`.
- [x] Implement `backend/tests/test_eval_safety.py`.
- [x] Report category, context, escalation, and per-language metrics.

### Phase 4: Local NLP Benchmark and Prototype-Ready Mode

School-project scope note: external legal, licensing, clinical and governance reviews are not
available for this NUS-ISS prototype. Checklist items that would require those reviews are satisfied
by recording the limitation and blocking real-world production claims. Educational prototype
semantic activation is allowed for demo purposes when explicitly feature-flagged, monotone, and
labelled as not clinically validated. Do not mark any model as clinically approved or
production-approved.

- [x] Define a narrow `safety_nlp.classify` tool contract.
- [x] Define typed `TranslationResult`, `MentionSpan`, `AssertionResult`, `ContextResult`, and `CategoryCandidate` contracts.
- [x] Add the tool to Safety's least-privilege allow-list and capability declaration.
- [x] Add the tool to the tool-access evaluation matrix.
- [x] Implement a common optional model-adapter interface with typed disabled/unavailable/timeout/invalid-output statuses.
- [x] Implement NLLB translation in shadow mode without replacing original text.
- [x] Implement MADLAD translation in shadow mode without replacing original text.
- [x] Benchmark NLLB and MADLAD separately for Mandarin, Malay, Tamil, and code-switched cases.
- [x] Measure translation critical-concept recall and negation/polarity, subject, and temporality preservation.
- [x] Record that NLLB is CC-BY-NC research-only for production and label any educational prototype use as non-production.
- [x] Measure MADLAD unquantized and candidate quantized memory, latency, and accuracy.
- [x] Implement `d4data/biomedical-ner-all` symptom extraction.
- [x] Bridge transformer token spans into spaCy entities while retaining original mention IDs.
- [x] Implement entity-marked assertion inference with `bvanaken/clinical-assertion-negation-bert`.
- [x] Add CareRoute medspaCy ConText rules for subject, care subject, temporality, and conditional context.
- [x] Add custom medspaCy rules in shadow mode and mark them as not clinically reviewed.
- [x] Implement BioLORD similarity against synthetic red-flag examples; require external clinical review before activation claims.
- [x] Benchmark BioLORD recall-at-k against LaBSE and direct multilingual mDeBERTa NLI.
- [x] Record that UMLS/SNOMED/IHTSDO/NLM license review is unavailable in this school-project setting and block BioLORD production claims while allowing labelled educational prototype benchmarking/activation.
- [x] Define the future multi-label Bio_ClinicalBERT training contract and keep the unfine-tuned base encoder disabled as a classifier.
- [x] Compare recall, specificity, calibration, abstention, model size, peak memory, and latency by stage and language.
- [x] Select the minimum prototype model set using recorded benchmark, hardware, latency, and documented license limitations.
- [x] Pin each selected model ID, revision, tokenizer revision, license, and artifact hash in a Safety model manifest.
- [x] Implement selected-model loading and startup warm-up without loading all benchmark candidates.
- [x] Move CPU-bound inference outside the async event loop.
- [x] Add maximum input length, timeout, device, and cache configuration.
- [x] Add `CAREROUTE_SAFETY_NLP` and related settings to configuration and `.env.example`.
- [x] Add optional heavy dependencies required by implemented Phase 4 adapters to `requirements-safety-nlp.txt`, not the deterministic runtime requirements; spaCy/medspaCy stay deferred while the local context-rule adapter is used.
- [x] Prohibit runtime Hugging Face downloads in release environments and verify offline-cache startup.
- [x] Degrade safely when dependencies or model artifacts are unavailable.
- [x] Compare translation-based findings with direct multilingual NLI and route disagreements to review telemetry.
- [x] Record NLP signals without emitting patient text to bus/audit logs; by default keep them shadow-only, and let Phase 6 feature flags decide whether accepted signals can change acuity.
- [x] Add model-free unit tests with fake adapters for every stage and fusion branch.
- [x] Add optional artifact-backed benchmark tests under a separate marker so normal tests never download models.
- [x] Add NLP success, uncertainty, disagreement, timeout, disabled, unavailable, and invalid-output tests.
- [x] Add per-stage latency, availability, abstention, disagreement, and memory metrics.
- [x] Add privacy-safe benchmark artifact generation that is never imported by the live inference path.
- [x] Write one aggregate benchmark summary per pipeline run and per-candidate/stage/language summaries where useful.
- [x] Record model/rule/threshold parameters, immutable lineage fields, aggregate metrics, and sanitized benchmark artifacts.
- [x] Add a local artifact-generation test for required fields and files.
- [x] Add a privacy test proving generated artifacts contain no patient text, translations, prompts, responses, evidence spans, case IDs, or session IDs.
- [x] Verify benchmark execution and deterministic tests do not require any tracking server.
- [x] Identify the selected prototype wrapper/manifest by commit SHA, benchmark hash, release-gate artifact and manifest hash.
- [x] Keep semantic activation disabled by default during Phase 4; require the Phase 6 prototype flag before model outputs can affect acuity.
- [x] Evaluate ONNX Runtime export after model selection and document deferral.

### Phase 5: Bounded LLM Adjudication

- [x] Upgrade `SemanticRedFlagLayer` to return the common `SafetySignal` structure.
- [x] Define and enforce a strict Pydantic response schema.
- [x] Restrict output categories to the authoritative rule vocabulary.
- [x] Validate evidence spans against patient input.
- [x] Clear stale semantic findings before each assessment.
- [x] Record disabled, timeout, provider failure, parse failure, and invalid-output statuses separately.
- [x] Skip the LLM when deterministic rules already trigger.
- [x] Call the LLM only for uncertain NLP cases in the initial hybrid path.
- [x] Add a Safety-specific LLM provider order with local Ollama preferred.
- [x] Keep cloud providers optional and covered by existing PII redaction controls.
- [x] Add Safety-specific model, timeout, and feature-flag settings to `.env.example`.
- [x] Add valid, invalid, unknown-category, fabricated-evidence, and low-confidence response tests.
- [x] Add patient-text prompt-injection and instruction-override tests.
- [x] Verify LLM failure leaves deterministic behavior unchanged.
- [x] Write only aggregate LLM evaluation metrics plus prompt/provider version identifiers to sanitized local/CI artifacts.
- [x] Add an artifact privacy regression proving prompts, responses and evidence text are never written.

### Phase 4.5: Live NLP Runtime and Natural-Uncertainty Handoff

Current implementation status: FastAPI constructs and warms a pinned, local-files-only runtime and
injects its adapter bundle into Safety. Missing optional stage artifacts produce typed unavailable
telemetry rather than fake-model fallback. A forcing-disabled artifact-backed SSE test proves that
natural BioLORD/mDeBERTa disagreement invokes bounded LLM adjudication without changing acuity.

- [x] Run `SafetyOverrideAgent.shadow_nlp()` in the orchestrator before `areason()`.
- [x] Align the LLM gate with telemetry's `uncertaintyCount` and `disagreementCount` keys while accepting
  legacy keys during migration.
- [x] Add a default-off `CAREROUTE_SAFETY_NLP_TEST_FORCE_UNCERTAINTY` development integration switch that
  changes sanitized telemetry only and cannot change acuity.
- [x] Add an SSE-pipeline test proving forced uncertainty invokes the bounded LLM and remains shadow-only.
- [x] Construct `SafetyNlpRuntime` from the pinned local manifest during FastAPI startup and warm it outside
  the event loop when Safety NLP is enabled.
- [x] Build and inject a model-backed live adapter bundle; keep `fake_adapters()` limited to unit tests and
  explicit development harnesses.
- [x] Carry real adapter confidence, assertion, subject, temporality, typed failure, and cross-model
  disagreement into the sanitized telemetry consumed by the LLM gate.
- [x] Add a live-artifact API/SSE test with the forcing switch disabled, proving a synthetic ambiguous
  paraphrase naturally invokes the LLM and preserves shadow-only acuity behavior.
- [x] Add startup, missing-artifact, timeout, and kill-switch tests for the live model-backed path.

### Phase 6: Educational Prototype Additive Activation and Release Gates

- [ ] Define calibrated NLP and LLM activation thresholds from benchmark results.
- [x] Add `CAREROUTE_SAFETY_PROTOTYPE_SEMANTIC_ACTIVATION` and keep it disabled by default.
- [x] Enable accepted high-confidence NLP positives to add a trigger.
- [x] Enable accepted LLM positives to add a trigger.
- [x] Label all semantic interventions as educational prototype behavior in response/debug/audit metadata.
- [x] Keep deterministic triggers authoritative in every merge path.
- [x] Route deterministic/context conflicts to HITL.
- [x] Verify deterministic red-flag recall remains `1.0` on the regression set.
- [x] Verify zero acuity-lowering outcomes.
- [ ] Verify active semantic red-flag recall meets the prototype overall threshold.
- [ ] Verify active semantic red-flag recall meets the threshold for each supported language.
- [ ] Verify context-classification macro F1 meets the prototype threshold.
- [ ] Verify semantic specificity meets the prototype threshold.
- [ ] Verify end-to-end HITL specificity remains at least `0.80`.
- [x] Add model-disabled, kill-switch, timeout, and artifact-missing pipeline tests.
- [x] Add per-channel and end-to-end latency gates.
- [x] Correct CI test selection so Safety regression tests are definitely executed.
- [ ] Add CI jobs for Safety NLP benchmark execution and sanitized artifact publication. The explicit
  benchmark job and privacy-safe JUnit publication are in place; publishing the aggregate benchmark
  and release-gate JSON remains pending.
- [x] Block real-world production claims when benchmark hash, manifest hash, release-gate artifact, or required approvals are missing; for this school project, permit only labelled educational prototype activation.
- [ ] Pin the selected prototype manifest only after all prototype gates pass.
- [ ] Verify rollback restores the prior pinned manifest and disables semantic activation without changing deterministic behavior.
- [ ] Run Safety, communication, contract, capability, evaluation, pipeline, API, and full backend test suites.
- [ ] Run lint and type checks required by the repository.

### MLOps, Security, and Documentation

- [x] Reconcile `uses_trained_model` capability semantics and its single-model-agent test.
- [ ] Add every selected Safety model, tokenizer, revision, license and hash to the AI-BOM.
- [ ] Link the Safety manifest to benchmark hash, release-gate artifact, model artifact hashes and commit SHA.
- [ ] Export benchmark/release summaries as durable CI artifacts.
- [ ] Document local artifact paths and CI artifact publication.
- [x] Verify missing benchmark artifacts can block prototype acceptance but can never block or delay live triage.
- [x] Record NLLB's CC-BY-NC/research-only restriction and MADLAD's clinical-domain limitation.
- [x] Record BioLORD's IHTSDO/NLM licensing obligations and the school-project approval status as unavailable/not approved.
- [ ] Extend the Model Card with intended use, provenance, metrics, and limitations.
- [ ] Version the Safety benchmark, policy, prompt, thresholds, model, and tokenizer.
- [ ] Add benchmark and per-language results to release artifacts.
- [ ] If deployment constraints require ONNX, add optional ONNX dependencies, export the selected Safety model, record exported artifact hashes, and prove parity against the baseline runtime before activation.
- [ ] Add semantic-channel, disagreement, conflict, failure, latency, and clinician-override metrics.
- [ ] Verify prompts, responses, and patient text are absent from application logs.
- [ ] Update `README.md`, backend documentation, architecture diagrams, security documentation, and capability documentation.
- [ ] Document independent NLP and LLM kill switches and rollback procedures.
- [x] Record that clinical approval of benchmark labels and release thresholds is unavailable in this project setting.
- [x] Record that additive activation is prototype-only and would require external security, clinical, and governance approval before real-world use.

### Phase 7: Optional Suppression Review

- [ ] Implement suppression calculations in shadow mode only.
- [ ] Measure potential false escalations avoided and true emergencies suppressed.
- [ ] Review every potential suppressed emergency with a clinician.
- [ ] Confirm all supported languages pass independently.
- [ ] Require deterministic context logic and model agreement for any proposed suppression.
- [ ] Route ambiguous subject or temporality to HITL.
- [ ] Define a separate suppression feature flag and immediate rollback path.
- [ ] Update safety claims and governance documentation before activation.
- [ ] Obtain explicit clinical and governance approval.
- [ ] Keep model-only suppression prohibited.

## 18. Definition of Done

The enhancement is complete when:

- The deterministic Safety path works with no model dependencies.
- The Supervisor and Safety worker have a tested, load-bearing request/response conversation.
- The selected local NLP pipeline and each prototype-activated model are pinned, documented, and evaluated across the four target languages as prototype evidence only.
- NLLB and MADLAD have been benchmarked separately, with a documented translation selection or an
  explicit decision to keep translation shadow-only.
- Symptom extraction, assertion, subject/temporality context, and category mapping have independent
  stage metrics and typed failure behavior.
- The unfine-tuned Bio_ClinicalBERT base checkpoint is never presented as a functioning red-flag
  classifier; any real-world activated classifier would require clinician-reviewed multi-label
  fine-tuning lineage outside this project scope.
- NLLB, MADLAD and BioLORD license/domain constraints are recorded; unresolved approvals block
  production claims but do not block labelled educational prototype activation.
- Safety NLP benchmarks are reproducibly tracked with sanitized local/CI artifacts, immutable
  dataset/model/policy lineage and aggregate component/language summaries.
- The deployed Safety configuration resolves to a pinned manifest with tested rollback; benchmark
  artifact generation remains absent from the live decision path, and semantic intervention requires
  an explicit educational prototype activation flag.
- Automated privacy tests prove benchmark artifacts contain no patient text, translations, prompts,
  responses, evidence spans, case IDs or session IDs.
- The LLM is bounded to closed-category context classification and only handles uncertain cases.
- With the development forcing switch disabled, at least one synthetic ambiguous paraphrase produces
  model-derived uncertainty or disagreement and reaches the bounded LLM through the live API path.
- All activated model outputs merge additively and cannot lower acuity.
- Context benchmarks include negation, history, hypothetical language, care subject, other-person statements, and compositional cases.
- Deterministic/context conflicts are visible and sent to HITL.
- Prototype gates cover recall, specificity, context classification, failure behavior, latency, and
  per-language performance; clinical release gates require external review that is unavailable here.
- Model unavailability and kill switches produce safe deterministic behavior.
- Raw patient text remains absent from message and audit payloads.
- MLOps, AI-BOM, Model Card, capability declarations, and architecture documentation reflect the additional model.
- Any suppression capability remains shadow-only unless separately approved under a future external
  clinical and governance review process.
