import pytest

from app.safety_nlp.release_gate import evaluate_prototype_release_gate

pytestmark = [pytest.mark.safety, pytest.mark.eval]


def passing_metrics():
    return {
        "deterministicRecall": 1.0,
        "acuityLoweringCount": 0,
        "semanticRecall": 0.97,
        "semanticRecallByLanguage": {"en": 0.98, "zh": 0.96, "ms": 0.95, "ta": 0.96},
        "contextMacroF1": 0.92,
        "semanticSpecificity": 0.93,
        "hitlSpecificity": 0.82,
        "nlpP95LatencyMs": 1800,
        "llmP95LatencyMs": 6500,
        "endToEndP95LatencyMs": 9000,
    }


def test_phase6_release_gate_accepts_metrics_and_labels_prototype_only():
    report = evaluate_prototype_release_gate(
        passing_metrics(), benchmark_hash="benchmark-sha256", manifest_hash="manifest-sha256"
    )

    assert report.passed is True
    assert report.production_eligible is False
    assert report.to_dict()["designation"] == "educational_prototype_only"
    assert report.to_dict()["lineage"]["benchmarkHash"] == "benchmark-sha256"


def test_phase6_release_gate_blocks_recall_regression_and_acuity_lowering():
    metrics = passing_metrics()
    metrics["deterministicRecall"] = 0.99
    metrics["acuityLoweringCount"] = 1
    metrics["semanticRecallByLanguage"]["ta"] = 0.90

    report = evaluate_prototype_release_gate(
        metrics, benchmark_hash="benchmark-sha256", manifest_hash="manifest-sha256"
    )

    assert report.passed is False
    assert any("deterministicRecall" in failure for failure in report.failures)
    assert any("acuityLoweringCount" in failure for failure in report.failures)
    assert any("semanticRecallByLanguage.ta" in failure for failure in report.failures)


def test_phase6_release_gate_requires_lineage_and_all_metrics():
    report = evaluate_prototype_release_gate({}, benchmark_hash="", manifest_hash="")

    assert report.passed is False
    assert "benchmarkHash is missing" in report.failures
    assert "manifestHash is missing" in report.failures
    assert "semanticRecallByLanguage is missing" in report.failures
