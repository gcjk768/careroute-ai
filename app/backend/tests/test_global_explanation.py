"""[XRAI] Global explanations — the model-level view beside the per-case SHAP.

Until 2026-09-15 CareRoute had only LOCAL explanations (one prediction's SHAP
values). The Explainable-AI syllabus treats global explanation (feature
importance, partial dependence) as the other half; these tests pin that the
audit now carries it, that it is computed rather than hard-coded, and that
the API forwards it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("sklearn")
pytest.importorskip("shap")

from app.main import app  # noqa: E402
from app.ml.features import FEATURE_NAMES  # noqa: E402
from app.ml.model import get_model  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module")
def global_explanation() -> dict:
    return get_model().fairness()["globalExplanation"]


def test_top_features_are_ranked_by_mean_abs_shap(global_explanation):
    features = global_explanation["features"]
    assert 5 <= len(features) <= 10
    shap_values = [f["meanAbsShap"] for f in features]
    assert shap_values == sorted(shap_values, reverse=True)
    assert shap_values[0] > 0
    assert {f["feature"] for f in features} <= set(FEATURE_NAMES)
    assert all(f["label"] for f in features)
    assert all(0 <= f["impurityImportance"] <= 1 for f in features)


def test_partial_dependence_is_a_probability_curve_over_the_top_features(global_explanation):
    pdp = global_explanation["partialDependence"]
    assert len(pdp) == 3
    top_three = [f["feature"] for f in global_explanation["features"][:3]]
    assert [p["feature"] for p in pdp] == top_three
    for curve in pdp:
        assert len(curve["grid"]) == len(curve["pUrgent"]) >= 2
        assert all(0.0 <= p <= 1.0 for p in curve["pUrgent"])
        # A top feature that never moves P(urgent) would not be a top feature.
        assert max(curve["pUrgent"]) - min(curve["pUrgent"]) > 0.0


def test_the_api_forwards_the_global_explanation():
    body = client.get("/api/fairness").json()
    assert body["globalExplanation"]["sampleN"] > 0
    assert body["globalExplanation"]["features"][0]["feature"] in FEATURE_NAMES
