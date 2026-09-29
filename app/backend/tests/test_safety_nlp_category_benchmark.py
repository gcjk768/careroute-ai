"""Safety NLP category-ranker benchmark tests."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from app.safety_nlp.category_benchmark import (
    CategoryRankerCandidate,
    benchmark_category_rankers,
)
from app.safety_nlp.contracts import CategoryCandidate, MentionSpan
from app.safety_nlp.similarity import (
    BIOLORD_MODEL_NAME,
    LABSE_MODEL_NAME,
    MDEBERTA_NLI_MODEL_NAME,
)

pytestmark = [pytest.mark.safety, pytest.mark.eval]

FIXTURE = Path(__file__).parent / "fixtures" / "safety_context_cases.json"
SENSITIVE_REPORT_KEYS = {
    "id",
    "text",
    "referenceTranslation",
    "mentions",
    "mention_id",
    "evidenceSpan",
    "prompt",
    "response",
    "caseId",
    "sessionId",
}


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    with FIXTURE.open(encoding="utf-8") as fh:
        return json.load(fh)["cases"]


class StaticRanker:
    def __init__(self, model_name: str, rankings: dict[str, list[str]]) -> None:
        self._model_name = model_name
        self._rankings = rankings

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_revision(self) -> str:
        return "test-revision"

    def rank(self, text: str, mention: MentionSpan) -> list[CategoryCandidate]:
        categories = self._rankings.get(mention.source_language, [])
        return [
            CategoryCandidate(
                mention_id=mention.mention_id,
                category=category,
                confidence=max(0.0, 1.0 - index * 0.1),
                model_name=self.model_name,
                model_revision=self.model_revision,
            )
            for index, category in enumerate(categories)
        ]


def test_category_benchmark_reports_recall_at_k_for_all_ranker_candidates(cases):
    biolord = StaticRanker(BIOLORD_MODEL_NAME, {
        "en": ["cardiac_chest_pain", "breathlessness", "seizure"],
        "zh": ["cardiac_chest_pain", "breathlessness", "stroke_signs"],
        "ms": ["stroke_signs", "cardiac_chest_pain", "severe_bleeding"],
        "ta": ["anaphylaxis", "suicidal_ideation", "cardiac_chest_pain"],
    })
    labse = StaticRanker(LABSE_MODEL_NAME, {
        "en": ["breathlessness", "cardiac_chest_pain", "seizure"],
        "zh": ["breathlessness", "cardiac_chest_pain", "stroke_signs"],
        "ms": ["cardiac_chest_pain", "stroke_signs", "severe_bleeding"],
        "ta": ["suicidal_ideation", "anaphylaxis", "cardiac_chest_pain"],
    })
    mdeberta = StaticRanker(MDEBERTA_NLI_MODEL_NAME, {
        "en": ["seizure", "cardiac_chest_pain", "breathlessness"],
        "zh": ["cardiac_chest_pain", "breathlessness", "stroke_signs"],
        "ms": ["stroke_signs", "severe_bleeding", "cardiac_chest_pain"],
        "ta": ["anaphylaxis", "suicidal_ideation", "seizure"],
    })

    report = benchmark_category_rankers(
        cases,
        (
            CategoryRankerCandidate("biolord", biolord),
            CategoryRankerCandidate("labse", labse),
            CategoryRankerCandidate("mdeberta-nli", mdeberta),
        ),
        k_values=(1, 3),
    )

    assert report.schema_version == 1
    assert report.candidates == ("biolord", "labse", "mdeberta-nli")
    assert report.k_values == (1, 3)
    assert {bucket.candidate for bucket in report.buckets} == {"biolord", "labse", "mdeberta-nli"}
    assert {bucket.language for bucket in report.buckets} >= {"en", "zh", "ms", "ta"}
    assert report.disagreements
    assert {
        bucket.comparator_candidate
        for bucket in report.disagreements
    } == {"labse", "mdeberta-nli"}
    assert all(0.0 <= bucket.disagreement_rate <= 1.0 for bucket in report.disagreements)
    assert all(bucket.review_telemetry_count == bucket.disagreements for bucket in report.disagreements)
    assert all(set(bucket.recall_at_k) == {1, 3} for bucket in report.buckets)
    assert all(0.0 <= rate <= 1.0 for bucket in report.buckets for rate in bucket.recall_at_k.values())
    assert all(0.0 <= bucket.threshold_recall <= 1.0 for bucket in report.buckets)
    assert all(0.0 <= bucket.specificity <= 1.0 for bucket in report.buckets)
    assert all(0.0 <= bucket.abstention_rate <= 1.0 for bucket in report.buckets)
    assert all(0.0 <= bucket.calibration_error <= 1.0 for bucket in report.buckets)
    assert all(bucket.positives + bucket.negatives == bucket.rows for bucket in report.buckets)
    assert any(bucket.model_name == BIOLORD_MODEL_NAME for bucket in report.buckets)
    assert any(bucket.model_name == LABSE_MODEL_NAME for bucket in report.buckets)
    assert any(bucket.model_name == MDEBERTA_NLI_MODEL_NAME for bucket in report.buckets)


def test_category_benchmark_summary_is_privacy_safe(cases):
    ranker = StaticRanker(BIOLORD_MODEL_NAME, {
        "en": ["cardiac_chest_pain"],
        "zh": ["cardiac_chest_pain"],
        "ms": ["cardiac_chest_pain"],
        "ta": ["cardiac_chest_pain"],
    })

    report = benchmark_category_rankers(cases, (CategoryRankerCandidate("biolord", ranker),))
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
        assert row["id"] not in payload_json
        assert row["text"] not in payload_json
        if row.get("referenceTranslation"):
            assert row["referenceTranslation"] not in payload_json
        for mention in row.get("mentions", []):
            assert mention["text"] not in payload_json
