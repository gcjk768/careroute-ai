"""[AI-Security][MLOps] Out-of-distribution and adversarial-input detection (AIC Day 1).

AIC Day 1 lists adversarial-example DETECTION beside adversarial training and
ensembles. Two detectors, each claimed only for what it was measured to catch
(2026-09-16, against the served model):

  detector                       held-out  HopSkipJump in-budget  implausible combos
  isolation forest (novelty)     1.0 %     0 of 6                 96.3 %
  feature-vector validity check  0 %       6 of 6                 -

The isolation forest does NOT catch in-budget adversarial examples: they are
small moves inside the training distribution. The validity check does, because
the attack perturbs binary symptom flags into values a real input can never
produce. Real HopSkipJump runs took ~9 minutes for 6 examples, so these tests
use bounded perturbations of the same shape (<= EPSILON on the flags).

A flagged input is never refused and never lowers acuity. The classifier caps
its confidence below the escalation threshold, so the case goes to a person
(or a clarifying question) instead of an automated answer.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("sklearn")

from app.agents import SeverityClassifierAgent  # noqa: E402
from app.agents.base import CONFIDENCE_THRESHOLD, CaseState  # noqa: E402
from app.ml import data  # noqa: E402
from app.ml.adversarial import EPSILON  # noqa: E402
from app.ml.features import N_SYMPTOM_FEATURES, extract_features  # noqa: E402
from app.ml.model import feature_vector_valid, get_model  # noqa: E402


@pytest.fixture(scope="module")
def ood() -> dict:
    return get_model().fairness()["outOfDistribution"]


def test_every_extracted_feature_vector_is_valid():
    for text, band, sex in [("crushing chest pain", "65+", "Male"), ("", None, None),
                            ("mild cough and a runny nose for two days", "0-17", "Female")]:
        assert feature_vector_valid(extract_features(text, band, sex))


def test_validity_check_catches_bounded_flag_perturbations():
    X, _, _ = data.generate_dataset(n=200, seed=11)
    rng = np.random.default_rng(3)
    X_adv = X.copy()
    X_adv[:, :N_SYMPTOM_FEATURES] += rng.uniform(-EPSILON, EPSILON, size=(len(X), N_SYMPTOM_FEATURES))
    assert all(feature_vector_valid(row) for row in X)
    assert not any(feature_vector_valid(row) for row in X_adv)


def test_audit_reports_measured_detection_rates(ood):
    assert ood["heldOutFlagRate"] <= 0.02
    assert ood["implausibleComboFlagRate"] >= 0.80
    assert ood["invalidVectorDetectionRate"] == 1.0
    assert "isolation forest" in ood["method"].lower()


def test_prediction_reports_the_detector_verdict():
    m = get_model()
    normal = m.predict("sore throat and a cough", "18-39", "Female")["outOfDistribution"]
    assert normal["validVector"] is True and normal["flagged"] is False
    assert isinstance(normal["noveltyScore"], float)


def test_an_implausible_presentation_is_flagged_and_sent_for_review(monkeypatch):
    """Eight unrelated organ systems in one sentence is not a presentation the
    model has seen; the classifier must not answer it confidently."""
    text = ("chest pain, sore throat, rash, bleeding, seizure, fever, abdominal pain, "
            "slurred speech, back pain, vomiting, dizziness and a sprained ankle")
    pred = get_model().predict(text, "40-64", "Male")
    assert pred["outOfDistribution"]["flagged"] is True

    state = CaseState(raw_text=text, normalised_symptoms=text, age_band="40-64", sex="Male")
    candidate = SeverityClassifierAgent()._try_model(state)
    assert candidate is not None
    assert candidate["confidence"] < CONFIDENCE_THRESHOLD
    assert candidate["acuity_code"] == pred["acuity_code"]       # never lowered, only doubted
