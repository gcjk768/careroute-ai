"""[XRAI] K-fold cross-validation — reliability evidence beyond a single split.

XRAI Day 1 (model building) asks for train/test PLUS K-fold mean ± std, so a
headline accuracy is known to be stable rather than one lucky partition. The
training audit carried only a single 75/25 split until 2026-09-16. These
tests pin that the audit reports 5-fold stratified CV for the served pipeline
(age-band oversampling + cost-sensitive RandomForest), that the numbers are
computed (folds vary, std is real), and that the API forwards them.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("sklearn")

from app.main import app  # noqa: E402
from app.ml.model import get_model  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module")
def cv() -> dict:
    return get_model().fairness()["crossValidation"]


def test_cross_validation_is_five_fold_stratified(cv):
    assert cv["folds"] == 5
    assert "StratifiedKFold" in cv["method"]
    for key in ("accuracy", "redFlagRecall", "fairnessGap"):
        assert 0.0 <= cv[key]["mean"] <= 1.0
        assert cv[key]["std"] >= 0.0
        assert len(cv[key]["perFold"]) == 5


def test_cross_validation_agrees_with_the_held_out_split(cv):
    audit = get_model().fairness()
    # A 5-fold mean that disagrees with the single-split number by more than a
    # few points would mean the headline accuracy was a partition artefact.
    assert abs(cv["accuracy"]["mean"] - audit["overallAccuracy"]) <= 0.05
    assert abs(cv["redFlagRecall"]["mean"] - audit["redFlagRecall"]) <= 0.05


def test_per_fold_numbers_are_computed_not_copied(cv):
    per_fold = cv["accuracy"]["perFold"]
    assert len(set(per_fold)) > 1, "identical fold accuracies — CV is not really running"


def test_api_forwards_cross_validation():
    body = client.get("/api/fairness").json()
    assert body["crossValidation"]["folds"] == 5
    assert "std" in body["crossValidation"]["accuracy"]
