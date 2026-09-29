"""Privacy-safe Safety NLP benchmark artifact generation.

This module is intentionally separate from `pipeline.py`; live inference should
not import benchmark artifact code.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .benchmark import TranslationBenchmarkReport
from .category_benchmark import CategoryBenchmarkReport

ARTIFACT_SCHEMA_VERSION = 1
FORBIDDEN_ARTIFACT_KEYS = frozenset({
    "id",
    "caseId",
    "sessionId",
    "text",
    "original_text",
    "translated_text",
    "working_text",
    "referenceTranslation",
    "mentions",
    "mention_id",
    "evidence",
    "evidenceSpan",
    "prompt",
    "response",
    "rationale",
})


def build_benchmark_artifact(
    *,
    translation_report: TranslationBenchmarkReport | None = None,
    category_report: CategoryBenchmarkReport | None = None,
    manifest_hash: str,
    dataset_hash: str,
    commit_sha: str,
    run_id: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build one aggregate artifact for a Safety NLP benchmark run."""
    artifact = {
        "schemaVersion": ARTIFACT_SCHEMA_VERSION,
        "artifactType": "safety_nlp_benchmark_summary",
        "createdAt": created_at or datetime.now(UTC).isoformat(),
        "run": {
            "runId": run_id,
            "commitSha": commit_sha,
            "manifestHash": manifest_hash,
            "datasetHash": dataset_hash,
        },
        "aggregate": {
            "translationCandidates": list(translation_report.candidates) if translation_report else [],
            "categoryCandidates": list(category_report.candidates) if category_report else [],
            "translationBucketCount": len(translation_report.buckets) if translation_report else 0,
            "categoryBucketCount": len(category_report.buckets) if category_report else 0,
            "categoryDisagreementBucketCount": len(category_report.disagreements) if category_report else 0,
        },
        "summaries": {
            "translation": _translation_summaries(translation_report),
            "category": _category_summaries(category_report),
            "categoryDisagreements": _category_disagreement_summaries(category_report),
        },
        "notes": [
            "Synthetic benchmark labels are not clinically validated.",
            "Artifact is aggregate-only and excludes patient text, translations, prompts, responses, evidence spans, case IDs and session IDs.",
        ],
    }
    validate_benchmark_artifact_privacy(artifact)
    return artifact


def build_llm_adjudication_artifact(
    *,
    prompt_version: str,
    provider_order: list[str],
    model_name: str,
    model_revision: str,
    schema_version: str,
    metrics: dict[str, Any],
    dataset_hash: str,
    commit_sha: str,
    run_id: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build an aggregate-only Safety LLM adjudication artifact."""
    artifact = {
        "schemaVersion": ARTIFACT_SCHEMA_VERSION,
        "artifactType": "safety_llm_adjudication_summary",
        "createdAt": created_at or datetime.now(UTC).isoformat(),
        "run": {
            "runId": run_id,
            "commitSha": commit_sha,
            "datasetHash": dataset_hash,
        },
        "promptVersion": prompt_version,
        "providerOrder": list(provider_order),
        "model": {
            "name": model_name,
            "revision": model_revision,
        },
        "llmSchemaVersion": schema_version,
        "aggregateMetrics": dict(metrics),
        "notes": [
            "Aggregate-only Safety LLM adjudication artifact.",
            "Prompts, raw responses, evidence spans, patient text, case IDs and session IDs are excluded.",
        ],
    }
    validate_benchmark_artifact_privacy(artifact)
    return artifact


def artifact_to_json(artifact: dict[str, Any]) -> str:
    """Serialize after re-validating the artifact privacy shape."""
    validate_benchmark_artifact_privacy(artifact)
    return json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2)


def write_benchmark_artifact(
    output_dir: str | Path,
    artifact: dict[str, Any],
    *,
    filename: str | None = None,
) -> Path:
    """Write a sanitized benchmark artifact JSON file."""
    validate_benchmark_artifact_privacy(artifact)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(artifact.get("run", {}).get("runId") or "safety-nlp-run")
    safe_name = filename or f"{_safe_filename(run_id)}.safety-nlp-benchmark.json"
    target = target_dir / safe_name
    target.write_text(artifact_to_json(artifact) + "\n", encoding="utf-8")
    return target


def benchmark_artifact_hash(artifact: dict[str, Any]) -> str:
    """Hash the canonical sanitized benchmark artifact JSON."""
    payload = artifact_to_json(artifact).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_selected_prototype_identity(
    *,
    runtime_wrapper: str,
    selected_category_model: str,
    manifest_hash: str,
    benchmark_artifact: dict[str, Any],
    release_gate_artifact: str,
    commit_sha: str,
) -> dict[str, str]:
    """Identify the selected prototype wrapper/manifest for release checks."""
    if not release_gate_artifact:
        raise ValueError("release_gate_artifact is required")
    identity = {
        "runtimeWrapper": runtime_wrapper,
        "selectedCategoryModel": selected_category_model,
        "manifestHash": manifest_hash,
        "benchmarkArtifactHash": benchmark_artifact_hash(benchmark_artifact),
        "releaseGateArtifact": release_gate_artifact,
        "commitSha": commit_sha,
    }
    validate_benchmark_artifact_privacy(identity)
    return identity


def validate_benchmark_artifact_privacy(payload: Any) -> None:
    """Reject sensitive field names before an artifact is written or uploaded."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_ARTIFACT_KEYS:
                raise ValueError(f"benchmark artifact contains forbidden key {key!r}")
            validate_benchmark_artifact_privacy(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            validate_benchmark_artifact_privacy(item)


def _translation_summaries(report: TranslationBenchmarkReport | None) -> list[dict[str, Any]]:
    if report is None:
        return []
    return [
        {
            "candidate": bucket.candidate,
            "stage": "translation",
            "variant": bucket.variant,
            "quantized": bucket.quantized,
            "language": bucket.language,
            "codeSwitched": bucket.code_switched,
            "rows": bucket.rows,
            "statusCounts": dict(bucket.status_counts),
            "successRate": bucket.success_rate,
            "criticalConceptRecall": bucket.critical_concept_recall,
            "measuredPreservationRates": bucket.measured_preservation_rates,
            "averageLatencyMs": bucket.average_latency_ms,
            "maxLatencyMs": bucket.latency_ms_max,
            "peakMemoryMb": bucket.peak_memory_mb,
            "declaredModelSizeGb": bucket.declared_model_size_gb,
            "modelName": bucket.model_name,
            "modelRevision": bucket.model_revision,
        }
        for bucket in report.buckets
    ]


def _category_summaries(report: CategoryBenchmarkReport | None) -> list[dict[str, Any]]:
    if report is None:
        return []
    return [
        {
            "candidate": bucket.candidate,
            "stage": "similarity",
            "language": bucket.language,
            "rows": bucket.rows,
            "statusCounts": dict(bucket.status_counts),
            "recallAtK": dict(bucket.recall_at_k),
            "threshold": bucket.threshold,
            "positives": bucket.positives,
            "negatives": bucket.negatives,
            "truePositives": bucket.true_positives,
            "falsePositives": bucket.false_positives,
            "trueNegatives": bucket.true_negatives,
            "falseNegatives": bucket.false_negatives,
            "abstentions": bucket.abstentions,
            "thresholdRecall": bucket.threshold_recall,
            "specificity": bucket.specificity,
            "abstentionRate": bucket.abstention_rate,
            "calibrationError": bucket.calibration_error,
            "modelName": bucket.model_name,
            "modelRevision": bucket.model_revision,
        }
        for bucket in report.buckets
    ]


def _category_disagreement_summaries(report: CategoryBenchmarkReport | None) -> list[dict[str, Any]]:
    if report is None:
        return []
    return [
        {
            "primaryCandidate": bucket.primary_candidate,
            "comparatorCandidate": bucket.comparator_candidate,
            "stage": "similarity",
            "language": bucket.language,
            "rows": bucket.rows,
            "disagreements": bucket.disagreements,
            "triggerDisagreements": bucket.trigger_disagreements,
            "categoryDisagreements": bucket.category_disagreements,
            "reviewTelemetryCount": bucket.review_telemetry_count,
            "disagreementRate": bucket.disagreement_rate,
        }
        for bucket in report.disagreements
    ]


def report_to_plain_dict(report: TranslationBenchmarkReport | CategoryBenchmarkReport) -> dict[str, Any]:
    """Expose a safe plain-dict conversion for tests and local scripts."""
    payload = asdict(report)
    validate_benchmark_artifact_privacy(payload)
    return payload


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return safe or "safety-nlp-run"
