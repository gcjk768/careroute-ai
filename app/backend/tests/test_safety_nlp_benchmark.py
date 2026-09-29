"""Safety NLP Phase 4 translation benchmark scaffold tests."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from app.safety_nlp.benchmark import (
    TARGET_TRANSLATION_LANGUAGES,
    TranslationCandidate,
    benchmark_translation_candidates,
)
from app.safety_nlp.contracts import AdapterConfig, OptionalModelAdapter, TranslationResult

pytestmark = [pytest.mark.safety, pytest.mark.eval]

FIXTURE = Path(__file__).parent / "fixtures" / "safety_context_cases.json"
SENSITIVE_REPORT_KEYS = {
    "id",
    "text",
    "referenceTranslation",
    "mentions",
    "rationale",
    "original_text",
    "translated_text",
    "working_text",
    "caseId",
    "sessionId",
    "evidenceSpan",
    "prompt",
    "response",
}


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    with FIXTURE.open(encoding="utf-8") as fh:
        return json.load(fh)["cases"]


class RecordingTranslationAdapter(OptionalModelAdapter):
    def __init__(
        self,
        model_name: str,
        *,
        status: str = "success",
        latency_ms: int = 0,
        translations: dict[tuple[str, str], str] | None = None,
    ) -> None:
        super().__init__(AdapterConfig(model_name=model_name, model_revision="test-revision"))
        self.status = status
        self.latency_ms = latency_ms
        self.translations = translations or {}
        self.calls: list[tuple[str, str]] = []

    def translate(self, text: str, language: str) -> TranslationResult:
        self.calls.append((language, text))
        return TranslationResult(
            original_text=text,
            language=language,
            translated_text=self.translations.get(
                (language, text),
                "synthetic translation omitted from benchmark report",
            ),
            model_name=self.model_name,
            model_revision=self.model_revision,
            status=self.status,
            latency_ms=self.latency_ms,
        )


def test_translation_benchmark_runs_nllb_and_madlad_separately(cases):
    nllb = RecordingTranslationAdapter("facebook/nllb-200-distilled-600M", latency_ms=12)
    madlad = RecordingTranslationAdapter("google/madlad400-3b-mt", latency_ms=34)

    report = benchmark_translation_candidates(
        cases,
        (
            TranslationCandidate("nllb", nllb),
            TranslationCandidate("madlad", madlad),
        ),
    )

    assert report.schema_version == 1
    assert report.candidates == ("nllb", "madlad")
    assert report.languages == TARGET_TRANSLATION_LANGUAGES
    assert len(nllb.calls) == len(madlad.calls)
    assert len(nllb.calls) > 0
    assert {bucket.candidate for bucket in report.buckets} == {"nllb", "madlad"}
    assert all(bucket.status_counts["success"] == bucket.rows for bucket in report.buckets)
    assert any(bucket.model_name == "facebook/nllb-200-distilled-600M" for bucket in report.buckets)
    assert any(bucket.model_name == "google/madlad400-3b-mt" for bucket in report.buckets)


def test_translation_benchmark_covers_target_languages_and_code_switched_cases(cases):
    adapter = RecordingTranslationAdapter("fake-translation")

    report = benchmark_translation_candidates(cases, (TranslationCandidate("fake", adapter),))
    buckets = {(bucket.language, bucket.code_switched): bucket for bucket in report.buckets}

    assert ("zh", False) in buckets
    assert ("ms", False) in buckets
    assert ("ta", False) in buckets
    assert ("ms", True) in buckets
    assert {language for language, _ in buckets} == set(TARGET_TRANSLATION_LANGUAGES)
    assert buckets[("ms", True)].rows > 0
    assert all(bucket.preservation_label_rows == bucket.rows for bucket in report.buckets)
    assert all(
        set(bucket.expected_preservation_true_counts) == {"symptom", "polarity", "subject", "temporality"}
        for bucket in report.buckets
    )


def test_translation_benchmark_records_typed_failure_statuses(cases):
    unavailable = RecordingTranslationAdapter("missing-madlad", status="unavailable")

    report = benchmark_translation_candidates(cases, (TranslationCandidate("madlad", unavailable),))

    assert report.buckets
    assert all(bucket.status_counts["unavailable"] == bucket.rows for bucket in report.buckets)
    assert all(bucket.success_rate == 0.0 for bucket in report.buckets)


def test_translation_benchmark_measures_concept_and_context_preservation(cases):
    reference_translations = {
        (row["language"], row["text"]): row["referenceTranslation"]
        for row in cases
        if row["language"] != "en"
    }
    adapter = RecordingTranslationAdapter("reference-fake", translations=reference_translations)

    report = benchmark_translation_candidates(cases, (TranslationCandidate("reference", adapter),))

    assert report.buckets
    assert all(bucket.critical_concept_total > 0 for bucket in report.buckets)
    assert all(0.0 <= bucket.critical_concept_recall <= 1.0 for bucket in report.buckets)
    assert any(bucket.critical_concept_recall == 1.0 for bucket in report.buckets)
    assert all(
        set(bucket.measured_preservation_total_counts) == {"symptom", "polarity", "subject", "temporality"}
        for bucket in report.buckets
    )
    assert any(
        bucket.measured_preservation_hit_counts["polarity"] > 0
        for bucket in report.buckets
    )
    assert any(
        bucket.measured_preservation_hit_counts["temporality"] > 0
        for bucket in report.buckets
    )


def test_translation_benchmark_measures_madlad_unquantized_and_quantized_variants(cases):
    reference_translations = {
        (row["language"], row["text"]): row["referenceTranslation"]
        for row in cases
        if row["language"] != "en"
    }
    unquantized = RecordingTranslationAdapter(
        "google/madlad400-3b-mt",
        latency_ms=1200,
        translations=reference_translations,
    )
    quantized = RecordingTranslationAdapter(
        "google/madlad400-3b-mt-8bit",
        latency_ms=480,
        translations=reference_translations,
    )

    report = benchmark_translation_candidates(
        cases,
        (
            TranslationCandidate(
                "madlad",
                unquantized,
                variant="unquantized",
                quantized=False,
                declared_model_size_gb=11.8,
                measured_peak_memory_mb=12100.0,
            ),
            TranslationCandidate(
                "madlad",
                quantized,
                variant="int8-candidate",
                quantized=True,
                declared_model_size_gb=4.2,
                measured_peak_memory_mb=4700.0,
            ),
        ),
    )

    variants = {(bucket.variant, bucket.language): bucket for bucket in report.buckets}

    assert ("unquantized", "zh") in variants
    assert ("int8-candidate", "zh") in variants
    assert variants[("unquantized", "zh")].quantized is False
    assert variants[("int8-candidate", "zh")].quantized is True
    assert variants[("unquantized", "zh")].declared_model_size_gb == 11.8
    assert variants[("int8-candidate", "zh")].declared_model_size_gb == 4.2
    assert variants[("unquantized", "zh")].peak_memory_mb == 12100.0
    assert variants[("int8-candidate", "zh")].peak_memory_mb == 4700.0
    assert variants[("unquantized", "zh")].average_latency_ms == 1200.0
    assert variants[("int8-candidate", "zh")].average_latency_ms == 480.0
    assert variants[("unquantized", "zh")].critical_concept_recall >= variants[
        ("int8-candidate", "zh")
    ].critical_concept_recall


def test_translation_benchmark_summary_is_privacy_safe(cases):
    adapter = RecordingTranslationAdapter("fake-translation")

    report = benchmark_translation_candidates(cases, (TranslationCandidate("fake", adapter),))
    payload = asdict(report)
    payload_json = json.dumps(payload, ensure_ascii=False)

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert key not in SENSITIVE_REPORT_KEYS
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(payload)
    for row in cases:
        assert row["text"] not in payload_json
        assert row["id"] not in payload_json
        if row.get("referenceTranslation"):
            assert row["referenceTranslation"] not in payload_json
        for mention in row.get("mentions", []):
            assert mention["text"] not in payload_json
