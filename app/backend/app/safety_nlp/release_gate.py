"""Phase 6 educational-prototype release gates.

The evaluator consumes aggregate metrics only. It is intentionally absent from
the live request path, so a missing benchmark artifact can block acceptance of
a prototype candidate but can never block or delay deterministic triage.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PrototypeGateThresholds:
    deterministic_recall: float = 1.0
    semantic_recall: float = 0.95
    context_macro_f1: float = 0.90
    semantic_specificity: float = 0.90
    hitl_specificity: float = 0.80
    nlp_p95_latency_ms: float = 2500.0
    llm_p95_latency_ms: float = 8000.0
    end_to_end_p95_latency_ms: float = 12000.0


@dataclass(frozen=True)
class PrototypeGateReport:
    passed: bool
    failures: tuple[str, ...]
    thresholds: PrototypeGateThresholds
    benchmark_hash: str = ""
    manifest_hash: str = ""
    production_eligible: bool = False
    designation: str = "educational_prototype_only"

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifactType": "safety_semantic_release_gate",
            "passed": self.passed,
            "failures": list(self.failures),
            "thresholds": {
                "deterministicRecall": self.thresholds.deterministic_recall,
                "semanticRecall": self.thresholds.semantic_recall,
                "contextMacroF1": self.thresholds.context_macro_f1,
                "semanticSpecificity": self.thresholds.semantic_specificity,
                "hitlSpecificity": self.thresholds.hitl_specificity,
                "nlpP95LatencyMs": self.thresholds.nlp_p95_latency_ms,
                "llmP95LatencyMs": self.thresholds.llm_p95_latency_ms,
                "endToEndP95LatencyMs": self.thresholds.end_to_end_p95_latency_ms,
            },
            "lineage": {
                "benchmarkHash": self.benchmark_hash,
                "manifestHash": self.manifest_hash,
            },
            "productionEligible": self.production_eligible,
            "designation": self.designation,
        }


def evaluate_prototype_release_gate(
    metrics: Mapping[str, Any],
    *,
    benchmark_hash: str = "",
    manifest_hash: str = "",
    thresholds: PrototypeGateThresholds | None = None,
) -> PrototypeGateReport:
    """Evaluate all Phase 6 prototype gates from sanitized aggregate metrics."""
    limits = thresholds or PrototypeGateThresholds()
    failures: list[str] = []

    _minimum(metrics, "deterministicRecall", limits.deterministic_recall, failures)
    _maximum(metrics, "acuityLoweringCount", 0.0, failures)
    _minimum(metrics, "semanticRecall", limits.semantic_recall, failures)
    _minimum(metrics, "contextMacroF1", limits.context_macro_f1, failures)
    _minimum(metrics, "semanticSpecificity", limits.semantic_specificity, failures)
    _minimum(metrics, "hitlSpecificity", limits.hitl_specificity, failures)
    _maximum(metrics, "nlpP95LatencyMs", limits.nlp_p95_latency_ms, failures)
    _maximum(metrics, "llmP95LatencyMs", limits.llm_p95_latency_ms, failures)
    _maximum(metrics, "endToEndP95LatencyMs", limits.end_to_end_p95_latency_ms, failures)

    per_language = metrics.get("semanticRecallByLanguage")
    if not isinstance(per_language, Mapping) or not per_language:
        failures.append("semanticRecallByLanguage is missing")
    else:
        for language, value in sorted(per_language.items()):
            _minimum_value(
                value,
                f"semanticRecallByLanguage.{language}",
                limits.semantic_recall,
                failures,
            )

    if not benchmark_hash:
        failures.append("benchmarkHash is missing")
    if not manifest_hash:
        failures.append("manifestHash is missing")

    return PrototypeGateReport(
        passed=not failures,
        failures=tuple(failures),
        thresholds=limits,
        benchmark_hash=benchmark_hash,
        manifest_hash=manifest_hash,
        # External clinical, licensing and governance approvals are deliberately
        # outside this school-project artifact, so it never authorizes production.
        production_eligible=False,
    )


def _minimum(metrics: Mapping[str, Any], key: str, minimum: float, failures: list[str]) -> None:
    if key not in metrics:
        failures.append(f"{key} is missing")
        return
    _minimum_value(metrics[key], key, minimum, failures)


def _minimum_value(value: Any, key: str, minimum: float, failures: list[str]) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        failures.append(f"{key} is not numeric")
        return
    if number < minimum:
        failures.append(f"{key}={number:.4f} is below {minimum:.4f}")


def _maximum(metrics: Mapping[str, Any], key: str, maximum: float, failures: list[str]) -> None:
    if key not in metrics:
        failures.append(f"{key} is missing")
        return
    try:
        number = float(metrics[key])
    except (TypeError, ValueError):
        failures.append(f"{key} is not numeric")
        return
    if number > maximum:
        failures.append(f"{key}={number:.4f} exceeds {maximum:.4f}")
