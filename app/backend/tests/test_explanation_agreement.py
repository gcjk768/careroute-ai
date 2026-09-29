"""[XRAI] Explanation AGREEMENT — SHAP vs a LIME-style local surrogate.

XRAI Day 2 contrasts LIME and SHAP and the question bank presses on
faithfulness/stability: an explanation a second method contradicts is not
evidence. Until 2026-09-16 the code advertised "SHAP + LIME" but the `lime`
package was never installed, so every LIME explanation was an empty list that
nothing read. That dead path is gone; instead the training audit fits an
in-repo LIME-style local linear surrogate (perturbed neighbourhood, distance-
weighted ridge) on held-out rows and reports how often it agrees with SHAP —
which reported symptom leads, top-3 overlap over all flags, Spearman rank
correlation — and the release gate floors the reported-symptom agreement.

The gate moved off top-3 overlap on 2026-09-22: over ALL flags the surrogate
ranks what ADDING an absent symptom would do against SHAP scoring its absence,
so at 51 categories the number fell to 0.31 with the served explanations
unchanged. See model._explanation_agreement.
"""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("sklearn")
pytest.importorskip("shap")

from app.main import app  # noqa: E402
from app.ml import model as model_mod  # noqa: E402
from app.ml.features import FEATURE_NAMES, extract_features  # noqa: E402
from app.ml.model import MIN_EXPLANATION_AGREEMENT, get_model  # noqa: E402
from app.ml.train import ReleaseGateError, validate  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module")
def agreement() -> dict:
    return get_model().fairness()["explanationAgreement"]


def test_agreement_block_is_computed_over_held_out_rows(agreement):
    assert agreement["n"] >= 40
    assert 0.0 <= agreement["top3Overlap"] <= 1.0
    assert -1.0 <= agreement["spearman"] <= 1.0
    assert "surrogate" in agreement["method"].lower()
    assert agreement["perturbations"] >= 100


def test_gated_metric_is_measured_on_rows_with_something_to_rank(agreement):
    # One reported symptom leaves nothing to rank, so those rows are excluded;
    # the gate must still rest on a real sample of multi-symptom rows.
    assert agreement["nMultiSymptomRows"] >= 30
    assert 0.0 <= agreement["reportedTop1Agreement"] <= 1.0


def test_agreement_clears_the_release_floor(agreement):
    assert agreement["reportedTop1Agreement"] >= MIN_EXPLANATION_AGREEMENT


def test_local_surrogate_ranks_the_driving_symptom_first():
    m = get_model()
    x = extract_features("crushing chest pain radiating to the arm", "40-64", "Male")
    idx = int(np.argmax(m.model.predict_proba(x.reshape(1, -1))[0]))
    weights = model_mod._local_surrogate_weights(m.model.predict_proba, x, idx, np.random.default_rng(0))
    assert weights.shape == (len(FEATURE_NAMES),)
    present = [j for j in range(len(FEATURE_NAMES)) if x[j] > 0.5 and FEATURE_NAMES[j] == "chest_pain"]
    assert present, "fixture must light the chest_pain flag"
    # The reported symptom should be among the strongest surrogate weights for
    # the predicted (urgent) class — the surrogate is local and faithful.
    top3 = np.argsort(np.abs(weights))[::-1][:3]
    assert present[0] in top3


def test_release_gate_rejects_a_disagreeing_explanation():
    audit = dict(get_model().fairness())
    audit["explanationAgreement"] = {**audit["explanationAgreement"],
                                     "reportedTop1Agreement": MIN_EXPLANATION_AGREEMENT - 0.05}
    with pytest.raises(ReleaseGateError, match="explanation agreement"):
        validate(audit)


def test_release_gate_requires_the_agreement_block():
    audit = dict(get_model().fairness())
    audit.pop("explanationAgreement")
    with pytest.raises(ReleaseGateError, match="explanation agreement"):
        validate(audit)


def test_dead_lime_path_is_gone():
    pred = get_model().predict("sore throat and a cough")
    assert "lime_explanation" not in pred
    assert not hasattr(model_mod, "_build_lime_explainer")


def test_api_forwards_explanation_agreement():
    body = client.get("/api/fairness").json()
    assert "reportedTop1Agreement" in body["explanationAgreement"]
    assert "top3Overlap" in body["explanationAgreement"]


def test_surrogate_neighbourhood_does_not_scale_with_the_feature_space():
    # Two expected flips per perturbed row whatever the category count. The
    # regression that failed the 51-category build was a per-flag rate that
    # flipped eleven flags per "neighbour" (see model._local_surrogate_weights).
    m = get_model()
    x = extract_features("crushing chest pain radiating to the arm", "40-64", "Male")
    idx = int(np.argmax(m.model.predict_proba(x.reshape(1, -1))[0]))
    flags = [j for j, n in enumerate(FEATURE_NAMES) if n not in ("symptom_count", "text_length")]
    mean_flips = []

    def spy(Z):
        mean_flips.append(float(np.abs(Z[:, flags] - x[flags]).sum(axis=1).mean()))
        return m.model.predict_proba(Z)

    model_mod._local_surrogate_weights(spy, x, idx, np.random.default_rng(0), n_samples=1000)
    assert 1.5 <= mean_flips[0] <= 2.5
