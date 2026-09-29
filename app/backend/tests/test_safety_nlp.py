"""Safety NLP Phase 4 scaffold tests.

These tests use only fake adapters. They must never download or import NLP model
artifacts; real adapters belong under a separate marker once they exist.
"""
from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from app.safety_nlp import classify, fake_adapters
from app.safety_nlp.classifier import (
    BIO_CLINICALBERT_CLASSIFIER_STATUS,
    BIO_CLINICALBERT_MODEL_NAME,
    BioClinicalBertTrainingContract,
    DisabledBioClinicalBertClassifier,
)
from app.safety_nlp.assertion import (
    CLINICAL_ASSERTION_MODEL_NAME,
    ClinicalAssertionAdapter,
    mark_entity,
)
from app.safety_nlp.context import (
    CAREROUTE_CONTEXT_CLINICAL_REVIEW,
    CAREROUTE_CONTEXT_RULE_VERSION,
    CareRouteContextRuleAdapter,
)
from app.safety_nlp.similarity import (
    BIOLORD_LICENSE_REVIEW_STATUS,
    BIOLORD_MODEL_NAME,
    BIOLORD_USAGE,
    BioLordSimilarityAdapter,
)
from app.safety_nlp.contracts import (
    AdapterConfig,
    AssertionResult,
    CategoryCandidate,
    ContextResult,
    MentionSpan,
    OptionalModelAdapter,
    SafetyNlpAdapters,
    TranslationResult,
)
from app.safety_nlp.fake_adapters import (
    FakeAssertionAdapter,
    FakeCategoryAdapter,
    FakeContextAdapter,
    FakeNerAdapter,
    FakeTranslationAdapter,
)
from app.safety_nlp.translation import (
    MADLAD_MODEL_NAME,
    NLLB_MODEL_NAME,
    NLLB_SOURCE_LANGUAGES,
    MadladTranslationAdapter,
    NllbTranslationAdapter,
)
from app.safety_nlp.ner import (
    BIOMEDICAL_NER_MODEL_NAME,
    BiomedicalNerAdapter,
    bridge_mentions_to_spacy_doc,
)
from app.safety_nlp.runtime import (
    BENCHMARK_COMPARATOR_MODELS,
    SELECTED_PROTOTYPE_CATEGORY_MODEL,
    SafetyNlpRuntime,
    select_minimum_prototype_model_set,
)
from app.safety_nlp.manifest import SafetyModelManifestError
from app.safety_nlp.telemetry import sanitized_signal_record, summarize_safety_signals
from app.agents.safety import SafetySignal

pytestmark = [pytest.mark.safety, pytest.mark.eval]


def test_contracts_validate_statuses_and_offsets():
    MentionSpan(mention_id="m1", text="chest pain", start=0, end=10)
    with pytest.raises(ValueError, match="offsets"):
        MentionSpan(mention_id="m1", text="chest pain", start=5, end=3)
    with pytest.raises(ValueError, match="status"):
        TranslationResult(
            original_text="x",
            language="en",
            translated_text="x",
            model_name="fake",
            model_revision="test",
            status="bad",
        )


def test_optional_model_adapter_reports_common_statuses():
    disabled = OptionalModelAdapter(AdapterConfig(
        model_name="fake",
        model_revision="test",
        enabled=False,
    ))
    missing = OptionalModelAdapter(AdapterConfig(
        model_name="fake",
        model_revision="test",
        requires_artifact=True,
    ), loaded=False)
    too_long = OptionalModelAdapter(AdapterConfig(
        model_name="fake",
        model_revision="test",
        max_input_chars=3,
    ))

    assert disabled.availability_status("text") == "disabled"
    assert missing.availability_status("text") == "unavailable"
    assert too_long.availability_status("long text") == "invalid_output"
    assert OptionalModelAdapter(
        AdapterConfig(model_name="fake", model_revision="test"),
        status_override="timeout",
    ).availability_status("text") == "timeout"


def test_fake_pipeline_accepts_current_patient_red_flag():
    signals = classify("I cannot breathe and have chest pain.", "en", adapters=fake_adapters())

    categories = {signal.category for signal in signals if signal.triggered}
    assert categories == {"breathlessness", "cardiac_chest_pain"}
    assert all(signal.channel == "nlp" for signal in signals)
    assert all(signal.status == "success" for signal in signals)


def test_fake_pipeline_keeps_negated_red_flag_non_triggering():
    signals = classify("I do not have chest pain.", "en", adapters=fake_adapters())

    assert len(signals) == 1
    assert signals[0].category == "cardiac_chest_pain"
    assert signals[0].assertion == "negated"
    assert signals[0].triggered is False


def test_fake_pipeline_keeps_remote_history_non_triggering():
    signals = classify(
        "I had a seizure ten years ago and feel fine today.", "en", adapters=fake_adapters()
    )

    assert len(signals) == 1
    assert signals[0].category == "seizure"
    assert signals[0].temporality == "remote"
    assert signals[0].triggered is False


def test_fake_pipeline_treats_conditional_self_harm_as_triggering():
    signals = classify("If this continues, I might kill myself.", "en", adapters=fake_adapters())

    assert len(signals) == 1
    assert signals[0].category == "suicidal_ideation"
    assert signals[0].assertion == "conditional"
    assert signals[0].triggered is True


def test_fake_pipeline_reports_translation_failure_as_typed_status():
    adapters = fake_adapters(translation=FakeTranslationAdapter(status_override="unavailable"))

    signals = classify("Saya sakit dada.", "ms", adapters=adapters)

    assert len(signals) == 1
    assert signals[0].triggered is False
    assert signals[0].stage == "translation"
    assert signals[0].status == "unavailable"


def test_fake_pipeline_disabled_mode_returns_typed_disabled_signal():
    signals = classify("I have chest pain.", "en", enabled=False)

    assert len(signals) == 1
    assert signals[0].triggered is False
    assert signals[0].status == "disabled"


def test_enabled_pipeline_without_injected_runtime_does_not_use_fake_adapters():
    signals = classify("I have chest pain.", "en")

    assert len(signals) == 1
    assert signals[0].stage == "classifier"
    assert signals[0].status == "unavailable"
    assert signals[0].category is None


def test_fake_translation_allows_non_english_shadow_path_without_real_model():
    adapters = fake_adapters(
        translation=FakeTranslationAdapter(translations={
            ("ta", "எனக்கு நெஞ்சு வலி"): "I have chest pain.",
        })
    )

    signals = classify("எனக்கு நெஞ்சு வலி", "ta", adapters=adapters)

    assert len(signals) == 1
    assert signals[0].category == "cardiac_chest_pain"
    assert signals[0].source_language == "ta"
    assert signals[0].triggered is True


class BadCategoryAdapter:
    def rank(self, text: str, mention: MentionSpan) -> list[CategoryCandidate]:
        return [
            CategoryCandidate(
                mention_id=mention.mention_id,
                category="not_a_rule",
                confidence=0.99,
                model_name="bad-fake",
                model_revision="test",
            )
        ]


def test_unknown_categories_are_dropped_before_safety_signal_validation():
    adapters = SafetyNlpAdapters(
        translation=FakeTranslationAdapter(),
        ner=FakeNerAdapter(),
        assertion=FakeAssertionAdapter(),
        context=FakeContextAdapter(),
        category=BadCategoryAdapter(),
    )

    signals = classify("I have chest pain.", "en", adapters=adapters)

    assert len(signals) == 1
    assert signals[0].category is None
    assert signals[0].triggered is False


def test_stage_failure_prevents_trigger_without_raising():
    adapters = fake_adapters(assertion=FakeAssertionAdapter(status_override="timeout"))

    signals = classify("I have chest pain.", "en", adapters=adapters)

    assert len(signals) == 1
    assert signals[0].status == "timeout"
    assert signals[0].triggered is False


def test_ner_timeout_without_mentions_is_preserved_as_typed_signal():
    adapters = fake_adapters(ner=FakeNerAdapter(status_override="timeout"))

    signals = classify("An ambiguous symptom description.", "en", adapters=adapters)

    assert len(signals) == 1
    assert signals[0].stage == "ner"
    assert signals[0].status == "timeout"


def test_category_comparator_disagreement_reaches_summary_telemetry():
    adapters = SafetyNlpAdapters(
        translation=FakeTranslationAdapter(),
        ner=FakeNerAdapter(),
        assertion=FakeAssertionAdapter(),
        context=FakeContextAdapter(),
        category=FakeCategoryAdapter(overrides={"m1": "cardiac_chest_pain"}),
        category_comparators=(FakeCategoryAdapter(overrides={"m1": "stroke_signs"}),),
    )

    signals = classify("I have chest pain.", "en", adapters=adapters)
    summary = summarize_safety_signals(signals).to_dict()

    assert [signal.category for signal in signals] == ["cardiac_chest_pain", "stroke_signs"]
    assert summary["disagreementCount"] == 1


def test_context_and_assertion_contracts_are_adapter_outputs():
    assertion = AssertionResult(mention_id="m1", assertion="possible", confidence=0.5)
    context = ContextResult(mention_id="m1", subject="care_subject", temporality="recent")

    assert assertion.assertion == "possible"
    assert context.subject == "care_subject"


def test_future_bio_clinicalbert_contract_keeps_base_encoder_disabled_as_classifier():
    contract = BioClinicalBertTrainingContract()
    classifier = DisabledBioClinicalBertClassifier()
    mention = MentionSpan(mention_id="m1", text="chest pain", start=0, end=10)

    candidates = classifier.rank("I have chest pain.", mention)

    assert contract.base_model == BIO_CLINICALBERT_MODEL_NAME
    assert "macro_f1" in contract.required_metrics
    assert "threshold_report" in contract.required_artifacts
    assert candidates[0].status == "disabled"
    assert candidates[0].category is None
    assert candidates[0].metadata["classifier_status"] == BIO_CLINICALBERT_CLASSIFIER_STATUS


class RecordingCategoryAdapter:
    loaded_keys: list[str] = []

    def __init__(self, config: AdapterConfig, cache_dir: str | None = None):
        self.config = config
        self.cache_dir = cache_dir
        self.rank_calls: list[tuple[str, str]] = []

    def load(self) -> None:
        self.loaded_keys.append(self.config.model_name)

    def rank(self, text: str, mention: MentionSpan) -> list[CategoryCandidate]:
        self.rank_calls.append((text, mention.mention_id))
        return [
            CategoryCandidate(
                mention_id=mention.mention_id,
                category="cardiac_chest_pain",
                confidence=0.9,
                model_name=self.config.model_name,
                model_revision=self.config.model_revision,
            )
        ]


def test_selected_runtime_chooses_biolord_and_keeps_comparators_out_of_warmup(monkeypatch):
    manifest = {
        "schemaVersion": 1,
        "activationMode": "shadow",
        "runtimeDownloadsAllowed": False,
        "similarity": {
            "biolord": {
                "model": BIOLORD_MODEL_NAME,
                "revision": "rev-biolord",
                "artifact_path": "models/safety/biolord-2023",
                "artifactSha256": "hash-biolord",
            },
            "labse": {
                "model": "sentence-transformers/LaBSE",
                "revision": "rev-labse",
                "artifact_path": "models/safety/labse",
                "artifactSha256": "hash-labse",
            },
            "mdebertaNli": {
                "model": "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli",
                "revision": "rev-nli",
                "artifact_path": "models/safety/mdeberta-v3-base-mnli-xnli",
                "artifactSha256": "hash-nli",
            },
        },
    }
    RecordingCategoryAdapter.loaded_keys = []
    import app.safety_nlp.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "load_safety_model_manifest", lambda path: manifest)

    selected = select_minimum_prototype_model_set(manifest)
    runtime = SafetyNlpRuntime.from_manifest_file(
        "manifest.json",
        enabled=True,
        project_root=Path("backend"),
        category_factory=RecordingCategoryAdapter,
    )
    runtime.warmup()

    assert selected.category_model_key == SELECTED_PROTOTYPE_CATEGORY_MODEL
    assert selected.benchmark_comparators == BENCHMARK_COMPARATOR_MODELS
    assert runtime.loaded_model_keys == ["biolord"]
    assert RecordingCategoryAdapter.loaded_keys == [BIOLORD_MODEL_NAME]
    assert Path(runtime.category_adapter.config.artifact_path).parts[-3:] == ("models", "safety", "biolord-2023")


def test_selected_runtime_rejects_manifests_that_allow_runtime_downloads(monkeypatch):
    manifest = {
        "schemaVersion": 1,
        "activationMode": "shadow",
        "runtimeDownloadsAllowed": True,
        "similarity": {
            "biolord": {
                "model": BIOLORD_MODEL_NAME,
                "revision": "rev-biolord",
                "artifact_path": "models/safety/biolord-2023",
                "artifactSha256": "hash-biolord",
            },
        },
    }
    import app.safety_nlp.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "load_safety_model_manifest", lambda path: manifest)

    with pytest.raises(SafetyModelManifestError, match="runtimeDownloadsAllowed"):
        SafetyNlpRuntime.from_manifest_file("manifest.json", category_factory=RecordingCategoryAdapter)


@pytest.mark.asyncio
async def test_selected_runtime_async_methods_move_warmup_and_rank_off_event_loop(monkeypatch):
    manifest = {
        "schemaVersion": 1,
        "activationMode": "shadow",
        "runtimeDownloadsAllowed": False,
        "similarity": {
            "biolord": {
                "model": BIOLORD_MODEL_NAME,
                "revision": "rev-biolord",
                "artifact_path": "models/safety/biolord-2023",
                "artifactSha256": "hash-biolord",
            },
        },
    }
    import app.safety_nlp.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "load_safety_model_manifest", lambda path: manifest)
    runtime = SafetyNlpRuntime.from_manifest_file(
        "manifest.json",
        enabled=True,
        project_root=Path("backend"),
        category_factory=RecordingCategoryAdapter,
    )
    mention = MentionSpan(mention_id="m1", text="chest pain", start=0, end=10)

    await runtime.awarmup()
    ranked = await runtime.arank_category("chest pain", mention)

    assert runtime.warmed is True
    assert runtime.loaded_model_keys == ["biolord"]
    assert ranked[0].category == "cardiac_chest_pain"
    assert runtime.category_adapter.rank_calls == [("chest pain", "m1")]


def test_signal_telemetry_summarizes_success_uncertainty_disagreement_and_failures():
    signals = [
        SafetySignal(
            channel="deterministic",
            triggered=True,
            category="cardiac_chest_pain",
            assertion="present",
            temporality="current",
            subject="patient",
            confidence=1.0,
            stage="deterministic",
            latency_ms=1,
        ),
        SafetySignal(
            channel="nlp",
            triggered=False,
            category="cardiac_chest_pain",
            assertion="possible",
            temporality="unknown",
            subject="patient",
            confidence=0.4,
            stage="classifier",
            latency_ms=12,
        ),
        SafetySignal(
            channel="nlp",
            triggered=False,
            category=None,
            status="timeout",
            stage="similarity",
            latency_ms=1000,
        ),
    ]

    summary = summarize_safety_signals(signals).to_dict()

    assert summary["totalSignals"] == 3
    assert summary["triggeredSignals"] == 1
    assert summary["uncertaintyCount"] == 2
    assert summary["abstentions"] == 2
    assert summary["disagreementCount"] == 1
    assert summary["statusCounts"] == {"success": 2, "timeout": 1}
    assert summary["stageLatencyMs"]["similarity"] == 1000
    assert summary["stageAvailabilityCounts"]["similarity"] == {"timeout": 1}


def test_sanitized_signal_record_excludes_patient_text_and_evidence():
    signal = SafetySignal(
        channel="nlp",
        triggered=True,
        category="breathlessness",
        assertion="present",
        temporality="current",
        subject="patient",
        confidence=0.9,
        evidence="PRIVATE_PATIENT_TEXT cannot breathe",
        stage="similarity",
    )

    record = sanitized_signal_record(signal)
    payload = repr(record)

    assert "PRIVATE_PATIENT_TEXT" not in payload
    assert "evidence" not in record
    assert record["category"] == "breathlessness"


def test_nllb_adapter_disabled_without_importing_model():
    adapter = NllbTranslationAdapter(AdapterConfig(
        model_name=NLLB_MODEL_NAME,
        model_revision="test-revision",
        enabled=False,
        requires_artifact=True,
        artifact_path="missing",
    ))

    result = adapter.translate("Saya sakit dada.", "ms")

    assert result.status == "disabled"
    assert result.original_text == "Saya sakit dada."
    assert result.translated_text == ""


def test_nllb_adapter_reports_unavailable_when_artifact_is_missing():
    adapter = NllbTranslationAdapter(AdapterConfig(
        model_name=NLLB_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models/safety/missing-model",
    ))

    result = adapter.translate("Saya sakit dada.", "ms")

    assert result.status == "unavailable"
    assert result.working_text == result.original_text


class StubNllbAdapter(NllbTranslationAdapter):
    def load(self) -> None:
        self.loaded = True

    def _translate_loaded(self, text: str, source_lang: str) -> str:
        assert source_lang == NLLB_SOURCE_LANGUAGES["ms"]
        assert text == "Saya sakit dada."
        return "I have chest pain."


def test_nllb_adapter_returns_shadow_working_copy_without_replacing_original():
    adapter = StubNllbAdapter(AdapterConfig(
        model_name=NLLB_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))

    result = adapter.translate("Saya sakit dada.", "ms")

    assert result.status == "success"
    assert result.original_text == "Saya sakit dada."
    assert result.translated_text == "I have chest pain."
    assert result.working_text == "I have chest pain."


def test_nllb_adapter_rejects_too_long_input_before_model_use():
    adapter = StubNllbAdapter(AdapterConfig(
        model_name=NLLB_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
        max_input_chars=3,
    ))

    result = adapter.translate("long text", "ms")

    assert result.status == "invalid_output"
    assert result.translated_text == ""


class TimeoutNllbAdapter(StubNllbAdapter):
    def _with_timeout(self, func) -> str:
        raise concurrent.futures.TimeoutError()


def test_nllb_adapter_reports_timeout_as_typed_status():
    adapter = TimeoutNllbAdapter(AdapterConfig(
        model_name=NLLB_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))

    result = adapter.translate("Saya sakit dada.", "ms")

    assert result.status == "timeout"
    assert result.translated_text == ""


def test_madlad_adapter_disabled_without_importing_model():
    adapter = MadladTranslationAdapter(AdapterConfig(
        model_name=MADLAD_MODEL_NAME,
        model_revision="test-revision",
        enabled=False,
        requires_artifact=True,
        artifact_path="missing",
    ))

    result = adapter.translate("Saya sakit dada.", "ms")

    assert result.status == "disabled"
    assert result.original_text == "Saya sakit dada."
    assert result.translated_text == ""


def test_madlad_adapter_reports_unavailable_when_artifact_is_missing():
    adapter = MadladTranslationAdapter(AdapterConfig(
        model_name=MADLAD_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models/safety/missing-madlad-model",
    ))

    result = adapter.translate("Saya sakit dada.", "ms")

    assert result.status == "unavailable"
    assert result.working_text == result.original_text


class StubMadladAdapter(MadladTranslationAdapter):
    def load(self) -> None:
        self.loaded = True

    def _translate_loaded(self, text: str) -> str:
        assert text == "Saya sakit dada."
        return "I have chest pain."


def test_madlad_adapter_returns_shadow_working_copy_without_replacing_original():
    adapter = StubMadladAdapter(AdapterConfig(
        model_name=MADLAD_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))

    result = adapter.translate("Saya sakit dada.", "ms")

    assert result.status == "success"
    assert result.original_text == "Saya sakit dada."
    assert result.translated_text == "I have chest pain."
    assert result.working_text == "I have chest pain."


class TimeoutMadladAdapter(StubMadladAdapter):
    def _with_timeout(self, func) -> str:
        raise concurrent.futures.TimeoutError()


def test_madlad_adapter_reports_timeout_as_typed_status():
    adapter = TimeoutMadladAdapter(AdapterConfig(
        model_name=MADLAD_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))

    result = adapter.translate("Saya sakit dada.", "ms")

    assert result.status == "timeout"
    assert result.translated_text == ""


def test_biomedical_ner_adapter_disabled_without_importing_model():
    adapter = BiomedicalNerAdapter(AdapterConfig(
        model_name=BIOMEDICAL_NER_MODEL_NAME,
        model_revision="test-revision",
        enabled=False,
        requires_artifact=True,
        artifact_path="missing",
    ))

    assert adapter.extract("I have chest pain.", "en") == []


def test_biomedical_ner_adapter_reports_unavailable_as_empty_mentions_when_artifact_missing():
    adapter = BiomedicalNerAdapter(AdapterConfig(
        model_name=BIOMEDICAL_NER_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models/safety/missing-biomedical-ner",
    ))

    assert adapter.extract("I have chest pain.", "en") == []


class StubBiomedicalNerAdapter(BiomedicalNerAdapter):
    def load(self) -> None:
        self.loaded = True

    def _extract_loaded(self, text: str) -> list[dict]:
        assert text == "I have crushing chest pain and a mild rash."
        return [
            {"entity_group": "Sign_symptom", "start": 16, "end": 26, "word": "chest pain"},
            {"entity_group": "Medication", "start": 38, "end": 42, "word": "rash"},
        ]


def test_biomedical_ner_adapter_extracts_symptom_mentions_with_offsets():
    adapter = StubBiomedicalNerAdapter(AdapterConfig(
        model_name=BIOMEDICAL_NER_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))

    mentions = adapter.extract("I have crushing chest pain and a mild rash.", "en")

    assert len(mentions) == 1
    assert mentions[0].mention_id == "ner-1"
    assert mentions[0].text == "chest pain"
    assert mentions[0].start == 16
    assert mentions[0].end == 26
    assert mentions[0].entity_type == "Sign_symptom"
    assert mentions[0].source_language == "en"
    assert mentions[0].model_name == BIOMEDICAL_NER_MODEL_NAME


class TimeoutBiomedicalNerAdapter(StubBiomedicalNerAdapter):
    def _with_timeout(self, func) -> list[dict]:
        raise concurrent.futures.TimeoutError()


def test_biomedical_ner_adapter_timeout_returns_empty_mentions():
    adapter = TimeoutBiomedicalNerAdapter(AdapterConfig(
        model_name=BIOMEDICAL_NER_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))

    assert adapter.extract("I have crushing chest pain and a mild rash.", "en") == []


class MalformedBiomedicalNerAdapter(StubBiomedicalNerAdapter):
    def _extract_loaded(self, text: str):
        return "not-a-list"


def test_biomedical_ner_adapter_malformed_output_returns_empty_mentions():
    adapter = MalformedBiomedicalNerAdapter(AdapterConfig(
        model_name=BIOMEDICAL_NER_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))

    assert adapter.extract("I have crushing chest pain and a mild rash.", "en") == []


class FakeSpanExtension:
    pass


class FakeSpan:
    def __init__(self, text: str, start: int, end: int, label: str) -> None:
        self.text = text
        self.start_char = start
        self.end_char = end
        self.label_ = label
        self._ = FakeSpanExtension()


class FakeDoc:
    def __init__(self, text: str) -> None:
        self.text = text
        self.ents = ()

    def char_span(self, start: int, end: int, label: str, alignment_mode: str):
        assert alignment_mode == "expand"
        if start < 0 or end > len(self.text):
            return None
        return FakeSpan(self.text[start:end], start, end, label)


def test_bridge_mentions_to_spacy_doc_retains_original_mention_ids():
    doc = FakeDoc("I have crushing chest pain and cannot breathe.")
    existing = FakeSpan("crushing", 7, 15, "Descriptor")
    doc.ents = (existing,)
    mentions = [
        MentionSpan(
            mention_id="ner-1",
            text="chest pain",
            start=16,
            end=26,
            entity_type="Sign_symptom",
            source_language="en",
            model_name=BIOMEDICAL_NER_MODEL_NAME,
            model_revision="test-revision",
        ),
        MentionSpan(
            mention_id="ner-2",
            text="cannot breathe",
            start=31,
            end=45,
            entity_type="Sign_symptom",
            source_language="en",
            model_name=BIOMEDICAL_NER_MODEL_NAME,
            model_revision="test-revision",
        ),
    ]

    bridged_doc = bridge_mentions_to_spacy_doc(doc, mentions)

    assert bridged_doc is doc
    assert len(doc.ents) == 3
    assert doc.ents[0] is existing
    assert doc.ents[1].text == "chest pain"
    assert doc.ents[1]._.safety_mention_id == "ner-1"
    assert doc.ents[1]._.safety_mention["source_language"] == "en"
    assert doc.ents[2].text == "cannot breathe"
    assert doc.ents[2]._.safety_mention_id == "ner-2"


def test_bridge_mentions_to_spacy_doc_skips_unalignable_mentions():
    doc = FakeDoc("I have chest pain.")
    mention = MentionSpan(
        mention_id="ner-1",
        text="x",
        start=0,
        end=1,
    )

    doc.char_span = lambda start, end, label, alignment_mode: None

    bridge_mentions_to_spacy_doc(doc, [mention])

    assert doc.ents == ()


def test_mark_entity_wraps_exact_mention_offsets():
    mention = MentionSpan(mention_id="m1", text="chest pain", start=7, end=17)

    marked = mark_entity("I have chest pain today.", mention)

    assert marked == "I have [entity]chest pain[/entity] today."


def test_mark_entity_rejects_stale_offsets():
    mention = MentionSpan(mention_id="m1", text="chest pain", start=0, end=10)

    with pytest.raises(ValueError, match="offsets"):
        mark_entity("I have chest pain today.", mention)


def test_clinical_assertion_adapter_disabled_without_importing_model():
    adapter = ClinicalAssertionAdapter(AdapterConfig(
        model_name=CLINICAL_ASSERTION_MODEL_NAME,
        model_revision="test-revision",
        enabled=False,
        requires_artifact=True,
        artifact_path="missing",
    ))
    mention = MentionSpan(mention_id="m1", text="chest pain", start=7, end=17)

    result = adapter.classify("I have chest pain today.", mention)

    assert result.status == "disabled"
    assert result.assertion == "unknown"
    assert result.confidence == 0.0


def test_clinical_assertion_adapter_missing_artifact_returns_unavailable():
    adapter = ClinicalAssertionAdapter(AdapterConfig(
        model_name=CLINICAL_ASSERTION_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models/safety/missing-assertion-model",
    ))
    mention = MentionSpan(mention_id="m1", text="chest pain", start=7, end=17)

    result = adapter.classify("I have chest pain today.", mention)

    assert result.status == "unavailable"
    assert result.assertion == "unknown"


class StubClinicalAssertionAdapter(ClinicalAssertionAdapter):
    output = {"label": "PRESENT", "score": 0.87}

    def load(self) -> None:
        self.loaded = True

    def _classify_loaded(self, marked_text: str) -> dict:
        assert marked_text == "I have [entity]chest pain[/entity] today."
        return self.output


def test_clinical_assertion_adapter_maps_present_output():
    adapter = StubClinicalAssertionAdapter(AdapterConfig(
        model_name=CLINICAL_ASSERTION_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))
    mention = MentionSpan(mention_id="m1", text="chest pain", start=7, end=17)

    result = adapter.classify("I have chest pain today.", mention)

    assert result.status == "success"
    assert result.assertion == "present"
    assert result.confidence == 0.87
    assert result.model_name == CLINICAL_ASSERTION_MODEL_NAME


class NegatedClinicalAssertionAdapter(StubClinicalAssertionAdapter):
    output = {"label": "ABSENT", "score": 0.91}


def test_clinical_assertion_adapter_maps_absent_output_to_negated():
    adapter = NegatedClinicalAssertionAdapter(AdapterConfig(
        model_name=CLINICAL_ASSERTION_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))
    mention = MentionSpan(mention_id="m1", text="chest pain", start=7, end=17)

    result = adapter.classify("I have chest pain today.", mention)

    assert result.status == "success"
    assert result.assertion == "negated"
    assert result.confidence == 0.91


class TimeoutClinicalAssertionAdapter(StubClinicalAssertionAdapter):
    def _with_timeout(self, func) -> dict:
        raise concurrent.futures.TimeoutError()


def test_clinical_assertion_adapter_timeout_returns_typed_status():
    adapter = TimeoutClinicalAssertionAdapter(AdapterConfig(
        model_name=CLINICAL_ASSERTION_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))
    mention = MentionSpan(mention_id="m1", text="chest pain", start=7, end=17)

    result = adapter.classify("I have chest pain today.", mention)

    assert result.status == "timeout"
    assert result.assertion == "unknown"


class MalformedClinicalAssertionAdapter(StubClinicalAssertionAdapter):
    output = {"label": "ALIEN_LABEL", "score": 0.5}


def test_clinical_assertion_adapter_malformed_output_returns_invalid_output():
    adapter = MalformedClinicalAssertionAdapter(AdapterConfig(
        model_name=CLINICAL_ASSERTION_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))
    mention = MentionSpan(mention_id="m1", text="chest pain", start=7, end=17)

    result = adapter.classify("I have chest pain today.", mention)

    assert result.status == "invalid_output"
    assert result.assertion == "unknown"


def test_careroute_context_rules_detect_care_subject_and_recent_temporality():
    adapter = CareRouteContextRuleAdapter()
    text = "My child had a seizure five minutes ago."
    mention = MentionSpan(mention_id="m1", text="seizure", start=15, end=22)

    result = adapter.classify(text, mention)

    assert result.status == "success"
    assert result.subject == "care_subject"
    assert result.temporality == "recent"
    assert result.conditional is False
    assert result.model_revision == CAREROUTE_CONTEXT_RULE_VERSION


def test_careroute_context_rules_detect_other_person_and_current_temporality():
    adapter = CareRouteContextRuleAdapter()
    text = "My brother cannot breathe, but I am calling for myself."
    mention = MentionSpan(mention_id="m1", text="cannot breathe", start=11, end=25)

    result = adapter.classify(text, mention)

    assert result.subject == "other_person"
    assert result.temporality == "current"


def test_careroute_context_rules_detect_remote_and_unknown_temporality():
    adapter = CareRouteContextRuleAdapter()
    remote_text = "I had a seizure ten years ago."
    unknown_text = "I sometimes get one sided weakness."

    remote = adapter.classify(remote_text, MentionSpan("m1", "seizure", 8, 15))
    unknown = adapter.classify(unknown_text, MentionSpan("m2", "one sided weakness", 16, 34))

    assert remote.temporality == "remote"
    assert unknown.temporality == "unknown"


def test_careroute_context_rules_detect_conditional_context():
    adapter = CareRouteContextRuleAdapter()
    text = "If I had chest pain, would it be serious?"
    mention = MentionSpan(mention_id="m1", text="chest pain", start=9, end=19)

    result = adapter.classify(text, mention)

    assert result.subject == "patient"
    assert result.temporality == "current"
    assert result.conditional is True


def test_careroute_context_rules_disabled_returns_unknown_status():
    adapter = CareRouteContextRuleAdapter(AdapterConfig(
        model_name="careroute-medspacy-context-rules",
        model_revision=CAREROUTE_CONTEXT_RULE_VERSION,
        enabled=False,
    ))
    mention = MentionSpan(mention_id="m1", text="chest pain", start=7, end=17)

    result = adapter.classify("I have chest pain.", mention)

    assert result.status == "disabled"
    assert result.subject == "unknown"
    assert result.temporality == "unknown"


def test_careroute_context_rules_are_marked_not_clinically_reviewed():
    assert CAREROUTE_CONTEXT_CLINICAL_REVIEW == "not_clinically_reviewed"


def test_biolord_similarity_adapter_disabled_returns_typed_empty_candidate():
    adapter = BioLordSimilarityAdapter(AdapterConfig(
        model_name=BIOLORD_MODEL_NAME,
        model_revision="test-revision",
        enabled=False,
        requires_artifact=True,
        artifact_path="missing",
    ))
    mention = MentionSpan(mention_id="m1", text="chest pain", start=0, end=10)

    candidates = adapter.rank("chest pain", mention)

    assert len(candidates) == 1
    assert candidates[0].status == "disabled"
    assert candidates[0].category is None


def test_biolord_similarity_adapter_missing_artifact_returns_unavailable():
    adapter = BioLordSimilarityAdapter(AdapterConfig(
        model_name=BIOLORD_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models/safety/missing-biolord",
    ))
    mention = MentionSpan(mention_id="m1", text="chest pain", start=0, end=10)

    candidates = adapter.rank("chest pain", mention)

    assert candidates[0].status == "unavailable"
    assert candidates[0].category is None


class StubBioLordSimilarityAdapter(BioLordSimilarityAdapter):
    def load(self) -> None:
        self._example_vectors = {
            "cardiac_chest_pain": [[1.0, 0.0]],
            "breathlessness": [[0.0, 1.0]],
        }
        self.loaded = True

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if texts == ["chest pain"]:
            return [[1.0, 0.0]]
        return super()._encode(texts)


def test_biolord_similarity_adapter_ranks_synthetic_redflag_examples():
    adapter = StubBioLordSimilarityAdapter(AdapterConfig(
        model_name=BIOLORD_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))
    mention = MentionSpan(mention_id="m1", text="chest pain", start=0, end=10)

    candidates = adapter.rank("chest pain", mention)

    assert candidates[0].status == "success"
    assert candidates[0].category == "cardiac_chest_pain"
    assert candidates[0].confidence == 1.0
    assert candidates[0].metadata["usage"] == BIOLORD_USAGE
    assert candidates[0].metadata["license_review_status"] == BIOLORD_LICENSE_REVIEW_STATUS


class TimeoutBioLordSimilarityAdapter(StubBioLordSimilarityAdapter):
    def _with_timeout(self, func) -> list[float]:
        raise concurrent.futures.TimeoutError()


def test_biolord_similarity_adapter_timeout_returns_typed_status():
    adapter = TimeoutBioLordSimilarityAdapter(AdapterConfig(
        model_name=BIOLORD_MODEL_NAME,
        model_revision="test-revision",
        enabled=True,
        requires_artifact=True,
        artifact_path="models",
    ))
    mention = MentionSpan(mention_id="m1", text="chest pain", start=0, end=10)

    candidates = adapter.rank("chest pain", mention)

    assert candidates[0].status == "timeout"
    assert candidates[0].category is None


def test_biolord_similarity_metadata_blocks_activation_claims():
    assert BIOLORD_USAGE == "shadow_only_synthetic_examples_not_clinically_reviewed"
    assert BIOLORD_LICENSE_REVIEW_STATUS == "pending_umls_snomed_ihtsdo_nlm_review"
