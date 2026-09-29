"""Privacy-safe Safety NLP benchmark artifact tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.safety_nlp.artifacts import (
    artifact_to_json,
    benchmark_artifact_hash,
    build_benchmark_artifact,
    build_llm_adjudication_artifact,
    build_selected_prototype_identity,
    validate_benchmark_artifact_privacy,
    write_benchmark_artifact,
)
from app.safety_nlp.benchmark import TranslationCandidate, benchmark_translation_candidates
from app.safety_nlp.category_benchmark import CategoryRankerCandidate, benchmark_category_rankers
from app.safety_nlp.contracts import AdapterConfig, CategoryCandidate, MentionSpan, OptionalModelAdapter, TranslationResult

pytestmark = [pytest.mark.safety, pytest.mark.eval]

FIXTURE = Path(__file__).parent / "fixtures" / "safety_context_cases.json"


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    with FIXTURE.open(encoding="utf-8") as fh:
        return json.load(fh)["cases"]


class ArtifactTranslationAdapter(OptionalModelAdapter):
    def __init__(self) -> None:
        super().__init__(AdapterConfig(model_name="artifact-translation", model_revision="test-revision"))

    def translate(self, text: str, language: str) -> TranslationResult:
        return TranslationResult(
            original_text=text,
            language=language,
            translated_text="synthetic aggregate translation omitted",
            model_name=self.model_name,
            model_revision=self.model_revision,
            latency_ms=7,
        )


class ArtifactCategoryRanker:
    @property
    def model_name(self) -> str:
        return "artifact-ranker"

    @property
    def model_revision(self) -> str:
        return "test-revision"

    def rank(self, text: str, mention: MentionSpan) -> list[CategoryCandidate]:
        return [
            CategoryCandidate(
                mention_id=mention.mention_id,
                category="cardiac_chest_pain",
                confidence=0.8,
                model_name=self.model_name,
                model_revision=self.model_revision,
            )
        ]


def test_build_benchmark_artifact_records_required_lineage_and_summaries(cases):
    translation = benchmark_translation_candidates(
        cases,
        (TranslationCandidate("translation", ArtifactTranslationAdapter()),),
    )
    category = benchmark_category_rankers(
        cases,
        (CategoryRankerCandidate("category", ArtifactCategoryRanker()),),
    )

    artifact = build_benchmark_artifact(
        translation_report=translation,
        category_report=category,
        manifest_hash="manifest-hash",
        dataset_hash="dataset-hash",
        commit_sha="commit-sha",
        run_id="run-001",
        created_at="2026-08-23T00:00:00+00:00",
    )

    assert artifact["schemaVersion"] == 1
    assert artifact["artifactType"] == "safety_nlp_benchmark_summary"
    assert artifact["run"] == {
        "runId": "run-001",
        "commitSha": "commit-sha",
        "manifestHash": "manifest-hash",
        "datasetHash": "dataset-hash",
    }
    assert artifact["aggregate"]["translationBucketCount"] == len(translation.buckets)
    assert artifact["aggregate"]["categoryBucketCount"] == len(category.buckets)
    assert artifact["aggregate"]["categoryDisagreementBucketCount"] == len(category.disagreements)
    assert artifact["summaries"]["translation"]
    assert artifact["summaries"]["category"]
    assert artifact["summaries"]["categoryDisagreements"] == []


def test_benchmark_artifact_is_privacy_safe(cases):
    translation = benchmark_translation_candidates(
        cases,
        (TranslationCandidate("translation", ArtifactTranslationAdapter()),),
    )
    artifact = build_benchmark_artifact(
        translation_report=translation,
        manifest_hash="manifest-hash",
        dataset_hash="dataset-hash",
        commit_sha="commit-sha",
        run_id="run-privacy",
        created_at="2026-08-23T00:00:00+00:00",
    )

    payload = artifact_to_json(artifact)

    for row in cases:
        assert row["id"] not in payload
        assert row["text"] not in payload
        if row.get("referenceTranslation"):
            assert row["referenceTranslation"] not in payload
        for mention in row.get("mentions", []):
            assert mention["text"] not in payload


def test_benchmark_artifact_validator_rejects_sensitive_keys():
    with pytest.raises(ValueError, match="forbidden key"):
        validate_benchmark_artifact_privacy({"prompt": "patient text"})


def test_llm_adjudication_artifact_is_aggregate_only_and_privacy_safe():
    artifact = build_llm_adjudication_artifact(
        prompt_version="safety-llm-schema-v1",
        provider_order=["openai"],
        model_name="gpt-4o-mini",
        model_revision="provider-configured",
        schema_version="1",
        metrics={
            "rows": 8,
            "triggered": 3,
            "notTriggered": 5,
            "statusCounts": {"success": 6, "provider_failure": 2},
            "averageLatencyMs": 220,
        },
        dataset_hash="dataset-hash",
        commit_sha="commit-sha",
        run_id="llm-run",
        created_at="2026-08-31T00:00:00+00:00",
    )

    payload = artifact_to_json(artifact)

    assert artifact["artifactType"] == "safety_llm_adjudication_summary"
    assert artifact["promptVersion"] == "safety-llm-schema-v1"
    assert artifact["providerOrder"] == ["openai"]
    assert "PRIVATE_PATIENT_MARKER" not in payload
    assert '"prompt"' not in payload
    assert '"response"' not in payload
    assert "evidenceSpan" not in payload


def test_benchmark_artifact_generation_does_not_require_tracking_server(monkeypatch, cases):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:1/unreachable")

    translation = benchmark_translation_candidates(
        cases,
        (TranslationCandidate("translation", ArtifactTranslationAdapter()),),
    )
    artifact = build_benchmark_artifact(
        translation_report=translation,
        manifest_hash="manifest-hash",
        dataset_hash="dataset-hash",
        commit_sha="commit-sha",
        run_id="run-no-tracking",
        created_at="2026-08-23T00:00:00+00:00",
    )

    assert artifact["run"]["runId"] == "run-no-tracking"
    assert artifact["aggregate"]["translationBucketCount"] == len(translation.buckets)


def test_write_benchmark_artifact_creates_required_json_file(cases):
    translation = benchmark_translation_candidates(
        cases,
        (TranslationCandidate("translation", ArtifactTranslationAdapter()),),
    )
    artifact = build_benchmark_artifact(
        translation_report=translation,
        manifest_hash="manifest-hash",
        dataset_hash="dataset-hash",
        commit_sha="commit-sha",
        run_id="run file test",
        created_at="2026-08-23T00:00:00+00:00",
    )
    output_dir = Path(__file__).parents[1] / ".pytest-safety-nlp-artifacts"
    written = output_dir / "run-file-test.safety-nlp-benchmark.json"

    try:
        path = write_benchmark_artifact(output_dir, artifact)
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert path == written
        assert payload["artifactType"] == "safety_nlp_benchmark_summary"
        assert payload["run"]["manifestHash"] == "manifest-hash"
        assert payload["run"]["datasetHash"] == "dataset-hash"
        assert payload["run"]["commitSha"] == "commit-sha"
    finally:
        if written.exists():
            written.unlink()
        if output_dir.exists():
            output_dir.rmdir()


def test_selected_prototype_identity_links_wrapper_manifest_benchmark_gate_and_commit(cases):
    translation = benchmark_translation_candidates(
        cases,
        (TranslationCandidate("translation", ArtifactTranslationAdapter()),),
    )
    artifact = build_benchmark_artifact(
        translation_report=translation,
        manifest_hash="manifest-hash",
        dataset_hash="dataset-hash",
        commit_sha="commit-sha",
        run_id="run-lineage",
        created_at="2026-08-23T00:00:00+00:00",
    )

    identity = build_selected_prototype_identity(
        runtime_wrapper="SafetyNlpRuntime",
        selected_category_model="similarity.biolord",
        manifest_hash="manifest-hash",
        benchmark_artifact=artifact,
        release_gate_artifact="release-gate-local-json",
        commit_sha="commit-sha",
    )

    assert identity == {
        "runtimeWrapper": "SafetyNlpRuntime",
        "selectedCategoryModel": "similarity.biolord",
        "manifestHash": "manifest-hash",
        "benchmarkArtifactHash": benchmark_artifact_hash(artifact),
        "releaseGateArtifact": "release-gate-local-json",
        "commitSha": "commit-sha",
    }
