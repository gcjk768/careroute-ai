"""Offline Safety NLP translation benchmark scaffolding.

The live inference path must not import benchmark artifact code. This module is
for Phase 4 shadow evaluation only: callers provide local/fake adapters and get
privacy-safe aggregate summaries back.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .contracts import AdapterStatus, TranslationAdapter

PRESERVATION_KEYS = ("symptom", "polarity", "subject", "temporality")
TARGET_TRANSLATION_LANGUAGES = ("zh", "ms", "ta")
VALID_TRANSLATION_STATUSES = ("success", "disabled", "unavailable", "timeout", "invalid_output")
CRITICAL_CONCEPT_TERMS: dict[str, tuple[str, ...]] = {
    "cardiac_chest_pain": ("chest pain", "chest pressure", "chest tightness"),
    "breathlessness": ("difficulty breathing", "short of breath", "cannot breathe", "can't breathe"),
    "anaphylaxis": ("throat", "tongue", "swelling", "closing"),
    "stroke_signs": ("face", "drooping", "speech", "slurred", "weakness"),
    "severe_bleeding": ("bleeding", "blood", "vomiting blood"),
    "suicidal_ideation": ("kill myself", "end my life", "want to die", "suicide"),
    "seizure": ("seizure", "convulsion"),
}
POLARITY_TERMS = ("no", "not", "without", "denies", "do not", "don't", "stopped")
SUBJECT_TERMS: dict[str, tuple[str, ...]] = {
    "patient": ("i", "me", "my"),
    "care_subject": ("my child", "my daughter", "my son", "child"),
    "other_person": ("brother", "sister", "someone", "other person"),
    "unknown": (),
}
TEMPORALITY_TERMS: dict[str, tuple[str, ...]] = {
    "current": ("now", "currently", "today", "right now"),
    "recent": ("minutes ago", "five minutes ago", "just now", "recently"),
    "remote": ("years ago", "ten years ago", "previously", "history"),
    "unknown": (),
}


@dataclass(frozen=True)
class TranslationCandidate:
    name: str
    adapter: TranslationAdapter
    variant: str = "unquantized"
    quantized: bool = False
    declared_model_size_gb: float | None = None
    measured_peak_memory_mb: float | None = None


@dataclass(frozen=True)
class TranslationBenchmarkBucket:
    candidate: str
    variant: str
    quantized: bool
    language: str
    code_switched: bool
    rows: int = 0
    status_counts: dict[AdapterStatus, int] = field(default_factory=dict)
    preservation_label_rows: int = 0
    expected_preservation_true_counts: dict[str, int] = field(default_factory=dict)
    critical_concept_total: int = 0
    critical_concept_hits: int = 0
    measured_preservation_total_counts: dict[str, int] = field(default_factory=dict)
    measured_preservation_hit_counts: dict[str, int] = field(default_factory=dict)
    latency_ms_total: int = 0
    latency_ms_max: int = 0
    peak_memory_mb: float | None = None
    declared_model_size_gb: float | None = None
    model_name: str | None = None
    model_revision: str | None = None

    @property
    def success_rate(self) -> float:
        return _rate(self.status_counts.get("success", 0), self.rows)

    @property
    def average_latency_ms(self) -> float:
        return _rate(self.latency_ms_total, self.rows)

    @property
    def critical_concept_recall(self) -> float:
        return _rate(self.critical_concept_hits, self.critical_concept_total)

    @property
    def measured_preservation_rates(self) -> dict[str, float]:
        return {
            key: _rate(self.measured_preservation_hit_counts.get(key, 0), total)
            for key, total in self.measured_preservation_total_counts.items()
        }


@dataclass(frozen=True)
class TranslationBenchmarkReport:
    schema_version: int
    candidates: tuple[str, ...]
    languages: tuple[str, ...]
    buckets: tuple[TranslationBenchmarkBucket, ...]
    notes: tuple[str, ...] = (
        "Synthetic benchmark labels are not clinically validated.",
        "Summaries are aggregate-only and must not contain patient text, translations, prompts, responses, evidence spans, case IDs or session IDs.",
    )


def benchmark_translation_candidates(
    cases: Iterable[Mapping],
    candidates: Iterable[TranslationCandidate],
    *,
    languages: Iterable[str] = TARGET_TRANSLATION_LANGUAGES,
) -> TranslationBenchmarkReport:
    """Benchmark translation candidates separately over non-English rows.

    The returned report intentionally contains no row-level identifiers, source
    text, reference translations or model outputs. It is safe to serialize as a
    local/CI aggregate artifact.
    """
    language_tuple = tuple(languages)
    selected_cases = [
        row for row in cases
        if row.get("language") in language_tuple and row.get("referenceTranslation")
    ]
    candidate_tuple = tuple(candidates)
    buckets: dict[tuple[str, str, bool, str, bool], _MutableBucket] = {}

    for candidate in candidate_tuple:
        for row in selected_cases:
            language = str(row["language"])
            code_switched = _is_code_switched(row)
            result = candidate.adapter.translate(str(row["text"]), language)
            key = (candidate.name, language, code_switched, candidate.variant, candidate.quantized)
            bucket = buckets.setdefault(
                key,
                _MutableBucket(
                    candidate=candidate.name,
                    variant=candidate.variant,
                    quantized=candidate.quantized,
                    declared_model_size_gb=candidate.declared_model_size_gb,
                    measured_peak_memory_mb=candidate.measured_peak_memory_mb,
                    language=language,
                    code_switched=code_switched,
                ),
            )
            bucket.add_result(result.status, result.latency_ms, result.model_name, result.model_revision)
            bucket.add_preservation_labels(row.get("translationPreserved"))
            bucket.add_translation_measurements(row, result.translated_text if result.status == "success" else "")

    return TranslationBenchmarkReport(
        schema_version=1,
        candidates=tuple(candidate.name for candidate in candidate_tuple),
        languages=language_tuple,
        buckets=tuple(
            buckets[key].freeze()
            for key in sorted(buckets, key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
        ),
    )


@dataclass
class _MutableBucket:
    candidate: str
    variant: str
    quantized: bool
    language: str
    code_switched: bool
    declared_model_size_gb: float | None = None
    measured_peak_memory_mb: float | None = None
    rows: int = 0
    status_counts: dict[AdapterStatus, int] = field(default_factory=lambda: dict.fromkeys(VALID_TRANSLATION_STATUSES, 0))
    preservation_label_rows: int = 0
    expected_preservation_true_counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PRESERVATION_KEYS, 0))
    critical_concept_total: int = 0
    critical_concept_hits: int = 0
    measured_preservation_total_counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PRESERVATION_KEYS, 0))
    measured_preservation_hit_counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PRESERVATION_KEYS, 0))
    latency_ms_total: int = 0
    latency_ms_max: int = 0
    peak_memory_mb: float | None = None
    model_name: str | None = None
    model_revision: str | None = None

    def add_result(
        self,
        status: AdapterStatus,
        latency_ms: int,
        model_name: str,
        model_revision: str,
    ) -> None:
        if status not in self.status_counts:
            self.status_counts[status] = 0
        self.rows += 1
        self.status_counts[status] += 1
        self.latency_ms_total += latency_ms
        self.latency_ms_max = max(self.latency_ms_max, latency_ms)
        self.peak_memory_mb = _max_optional(self.peak_memory_mb, self.measured_peak_memory_mb)
        self.model_name = model_name
        self.model_revision = model_revision

    def add_preservation_labels(self, labels) -> None:
        if not isinstance(labels, Mapping):
            return
        self.preservation_label_rows += 1
        for key in PRESERVATION_KEYS:
            if labels.get(key) is True:
                self.expected_preservation_true_counts[key] += 1

    def add_translation_measurements(self, row: Mapping, translated_text: str) -> None:
        normalized = _normalize_text(translated_text)
        category = str(row.get("category", ""))
        concept_terms = CRITICAL_CONCEPT_TERMS.get(category, ())
        if concept_terms:
            self.critical_concept_total += 1
            if _contains_any(normalized, concept_terms):
                self.critical_concept_hits += 1

        checks = {
            "symptom": concept_terms,
            "polarity": _expected_polarity_terms(row),
            "subject": SUBJECT_TERMS.get(str(row.get("subject", "")), ()),
            "temporality": TEMPORALITY_TERMS.get(str(row.get("temporality", "")), ()),
        }
        for key, terms in checks.items():
            if not terms:
                continue
            self.measured_preservation_total_counts[key] += 1
            if _contains_any(normalized, terms):
                self.measured_preservation_hit_counts[key] += 1

    def freeze(self) -> TranslationBenchmarkBucket:
        return TranslationBenchmarkBucket(
            candidate=self.candidate,
            variant=self.variant,
            quantized=self.quantized,
            language=self.language,
            code_switched=self.code_switched,
            rows=self.rows,
            status_counts=dict(self.status_counts),
            preservation_label_rows=self.preservation_label_rows,
            expected_preservation_true_counts=dict(self.expected_preservation_true_counts),
            critical_concept_total=self.critical_concept_total,
            critical_concept_hits=self.critical_concept_hits,
            measured_preservation_total_counts=dict(self.measured_preservation_total_counts),
            measured_preservation_hit_counts=dict(self.measured_preservation_hit_counts),
            latency_ms_total=self.latency_ms_total,
            latency_ms_max=self.latency_ms_max,
            peak_memory_mb=self.peak_memory_mb,
            declared_model_size_gb=self.declared_model_size_gb,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )


def _is_code_switched(row: Mapping) -> bool:
    row_id = str(row.get("id", "")).lower()
    return "code-switched" in row_id or "code_switched" in row_id


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _max_optional(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _normalize_text(text: str) -> str:
    lowered = text.casefold()
    return re.sub(r"[^a-z0-9']+", " ", lowered)


def _contains_any(normalized_text: str, terms: tuple[str, ...]) -> bool:
    padded = f" {normalized_text} "
    return any(f" {_normalize_text(term).strip()} " in padded for term in terms)


def _expected_polarity_terms(row: Mapping) -> tuple[str, ...]:
    assertion = str(row.get("assertion", ""))
    if assertion == "negated":
        return POLARITY_TERMS
    if assertion == "conditional":
        return ("if", "might", "would", "could")
    return ()
