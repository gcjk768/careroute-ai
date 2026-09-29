"""Safety NLP shadow pipeline.

Phase 4 starts model-free: typed contracts plus fake adapters that behave like
the future model adapters without importing transformers, PyTorch, spaCy or
sentence-transformers. Live Safety can keep using the deterministic floor while
this package gives tests and benchmark code a stable interface to build on.
"""
from __future__ import annotations

from .artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    FORBIDDEN_ARTIFACT_KEYS,
    artifact_to_json,
    benchmark_artifact_hash,
    build_benchmark_artifact,
    build_llm_adjudication_artifact,
    build_selected_prototype_identity,
    report_to_plain_dict,
    validate_benchmark_artifact_privacy,
    write_benchmark_artifact,
)
from .assertion import (
    CLINICAL_ASSERTION_LICENSE,
    CLINICAL_ASSERTION_MODEL_NAME,
    ClinicalAssertionAdapter,
    clinical_assertion_config_from_manifest,
    mark_entity,
)
from .benchmark import (
    TARGET_TRANSLATION_LANGUAGES,
    TranslationBenchmarkReport,
    TranslationCandidate,
    benchmark_translation_candidates,
)
from .category_benchmark import (
    CategoryBenchmarkReport,
    CategoryRankerCandidate,
    benchmark_category_rankers,
)
from .classifier import (
    BIO_CLINICALBERT_CLASSIFIER_STATUS,
    BIO_CLINICALBERT_MODEL_NAME,
    BioClinicalBertTrainingContract,
    DisabledBioClinicalBertClassifier,
    bio_clinicalbert_config_from_manifest,
)
from .context import (
    CAREROUTE_CONTEXT_CLINICAL_REVIEW,
    CAREROUTE_CONTEXT_RULE_VERSION,
    CareRouteContextRuleAdapter,
)
from .contracts import (
    AdapterConfig,
    AdapterStatus,
    AssertionResult,
    CategoryCandidate,
    ContextResult,
    MentionSpan,
    OptionalModelAdapter,
    SafetyNlpAdapters,
    TranslationResult,
)
from .fake_adapters import fake_adapters
from .manifest import (
    SafetyModelManifestError,
    load_safety_model_manifest,
    validate_safety_model_manifest,
)
from .ner import (
    BIOMEDICAL_NER_LICENSE,
    BIOMEDICAL_NER_MODEL_NAME,
    BiomedicalNerAdapter,
    biomedical_ner_config_from_manifest,
    bridge_mentions_to_spacy_doc,
)
from .pipeline import classify
from .release_gate import (
    PrototypeGateReport,
    PrototypeGateThresholds,
    evaluate_prototype_release_gate,
)
from .runtime import (
    BENCHMARK_COMPARATOR_MODELS,
    SELECTED_PROTOTYPE_CATEGORY_MODEL,
    SafetyNlpRuntime,
    SelectedPrototypeModelSet,
    select_minimum_prototype_model_set,
)
from .similarity import (
    BIOLORD_LICENSE_REVIEW_STATUS,
    BIOLORD_MODEL_NAME,
    BIOLORD_USAGE,
    LABSE_MODEL_NAME,
    MDEBERTA_NLI_MODEL_NAME,
    SYNTHETIC_REDFLAG_EXAMPLES,
    BioLordSimilarityAdapter,
    LabseSimilarityAdapter,
    MDebertaNliAdapter,
    biolord_config_from_manifest,
    labse_config_from_manifest,
    mdeberta_nli_config_from_manifest,
)
from .telemetry import (
    SafetyNlpTelemetrySummary,
    sanitized_signal_record,
    summarize_safety_signals,
)
from .translation import (
    MADLAD_LICENSE,
    MADLAD_MODEL_NAME,
    NLLB_LICENSE,
    NLLB_MODEL_NAME,
    MadladTranslationAdapter,
    NllbTranslationAdapter,
    madlad_config_from_manifest,
    nllb_config_from_manifest,
)

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "BENCHMARK_COMPARATOR_MODELS",
    "BIOLORD_LICENSE_REVIEW_STATUS",
    "BIOLORD_MODEL_NAME",
    "BIOLORD_USAGE",
    "BIOMEDICAL_NER_LICENSE",
    "BIOMEDICAL_NER_MODEL_NAME",
    "BIO_CLINICALBERT_CLASSIFIER_STATUS",
    "BIO_CLINICALBERT_MODEL_NAME",
    "CAREROUTE_CONTEXT_CLINICAL_REVIEW",
    "CAREROUTE_CONTEXT_RULE_VERSION",
    "CLINICAL_ASSERTION_LICENSE",
    "CLINICAL_ASSERTION_MODEL_NAME",
    "FORBIDDEN_ARTIFACT_KEYS",
    "LABSE_MODEL_NAME",
    "MADLAD_LICENSE",
    "MADLAD_MODEL_NAME",
    "MDEBERTA_NLI_MODEL_NAME",
    "NLLB_LICENSE",
    "NLLB_MODEL_NAME",
    "SELECTED_PROTOTYPE_CATEGORY_MODEL",
    "SYNTHETIC_REDFLAG_EXAMPLES",
    "TARGET_TRANSLATION_LANGUAGES",
    "AdapterConfig",
    "AdapterStatus",
    "AssertionResult",
    "BioClinicalBertTrainingContract",
    "BioLordSimilarityAdapter",
    "BiomedicalNerAdapter",
    "CareRouteContextRuleAdapter",
    "CategoryBenchmarkReport",
    "CategoryCandidate",
    "CategoryRankerCandidate",
    "ClinicalAssertionAdapter",
    "ContextResult",
    "DisabledBioClinicalBertClassifier",
    "LabseSimilarityAdapter",
    "MDebertaNliAdapter",
    "MadladTranslationAdapter",
    "MentionSpan",
    "NllbTranslationAdapter",
    "OptionalModelAdapter",
    "PrototypeGateReport",
    "PrototypeGateThresholds",
    "SafetyModelManifestError",
    "SafetyNlpAdapters",
    "SafetyNlpRuntime",
    "SafetyNlpTelemetrySummary",
    "SelectedPrototypeModelSet",
    "TranslationBenchmarkReport",
    "TranslationCandidate",
    "TranslationResult",
    "artifact_to_json",
    "benchmark_artifact_hash",
    "benchmark_category_rankers",
    "benchmark_translation_candidates",
    "bio_clinicalbert_config_from_manifest",
    "biolord_config_from_manifest",
    "biomedical_ner_config_from_manifest",
    "bridge_mentions_to_spacy_doc",
    "build_benchmark_artifact",
    "build_llm_adjudication_artifact",
    "build_selected_prototype_identity",
    "classify",
    "clinical_assertion_config_from_manifest",
    "evaluate_prototype_release_gate",
    "fake_adapters",
    "labse_config_from_manifest",
    "load_safety_model_manifest",
    "madlad_config_from_manifest",
    "mark_entity",
    "mdeberta_nli_config_from_manifest",
    "nllb_config_from_manifest",
    "report_to_plain_dict",
    "sanitized_signal_record",
    "select_minimum_prototype_model_set",
    "summarize_safety_signals",
    "validate_benchmark_artifact_privacy",
    "validate_safety_model_manifest",
    "write_benchmark_artifact",
]
