"""[Responsible-AI][Safety] Per-subgroup red-flag recall — measured, gated, and
checked on the pipeline a patient actually receives.

Model `careroute-triage-rf-4e409ad346f3` shipped with population red-flag recall
0.9747 while 65+ Male sat at 0.8966 (26 of 29 emergencies caught): the release
gate only looked at the population figure, so a subgroup could miss one
emergency in ten and still ship. These tests pin:

  * the metric (`fairness.subgroup_red_flag_recall`) and its support counts,
  * the release gate (`train.validate`) failing on exactly that old audit, on the
    raw forest AND on the served (calibrated + post-processed) pipeline,
  * the minimum-support rule (a subgroup with too few emergencies to estimate
    a 0.95 floor is reported, not gated),
  * the sex-flip counterfactual run on the served pipeline, cross-checked
    end-to-end through `TriageModel.predict`,
  * the sidecar SHA-256 check on artifact load.
"""
from __future__ import annotations

import hashlib
import os

import joblib
import numpy as np
import pytest

from app.ml import fairness, train
from app.ml import model as model_module
from app.ml.model import MIN_RED_FLAG_RECALL, MIN_SUBGROUP_SEVERE_SUPPORT

# The live per-subgroup numbers of the model this gate exists to stop
# (held-out split, raw forest): 65+ Male 26/29 = 0.8966.
_OLD_RAW = {
    "0-17 · Female": {"recall": 1.0, "nSevere": 47},
    "0-17 · Male": {"recall": 0.9737, "nSevere": 38},
    "18-39 · Female": {"recall": 0.9792, "nSevere": 48},
    "18-39 · Male": {"recall": 1.0, "nSevere": 37},
    "40-64 · Female": {"recall": 1.0, "nSevere": 35},
    "40-64 · Male": {"recall": 0.9762, "nSevere": 42},
    "65+ · Female": {"recall": 0.95, "nSevere": 40},
    "65+ · Male": {"recall": 0.8966, "nSevere": 29},
}


def _healthy(**overrides) -> dict:
    ok = {g: {"recall": 1.0, "nSevere": v["nSevere"]} for g, v in _OLD_RAW.items()}
    block = {"floor": MIN_RED_FLAG_RECALL, "minSevereSupport": MIN_SUBGROUP_SEVERE_SUPPORT,
             "raw": ok, "served": dict(ok)}
    block.update(overrides)
    return {
        "overallAccuracy": 0.9, "redFlagRecall": 0.99,
        "fairnessGapBefore": 0.5, "fairnessGapAfter": 0.1,
        "calibration": {"method": "isotonic", "ece": 0.02, "brier": 0.3},
        "explanationAgreement": {"reportedTop1Agreement": 0.95},
        "postProcessing": {"after": {"equalOpportunityGap": 0.05}},
        "subgroupRedFlagRecall": block,
    }


# ---------------------------------------------------------------- the metric
def test_subgroup_red_flag_recall_reports_recall_and_support():
    y_true = np.array([0, 1, 1, 3, 0, 1, 4])
    y_pred = np.array([0, 2, 1, 3, 1, 1, 4])
    groups = ["A", "A", "A", "A", "B", "B", "B"]
    out = fairness.subgroup_red_flag_recall(y_true, y_pred, groups)
    assert out["A"] == {"recall": 0.6667, "nSevere": 3}
    assert out["B"] == {"recall": 1.0, "nSevere": 2}


def test_a_subgroup_with_no_emergencies_is_listed_with_zero_support():
    out = fairness.subgroup_red_flag_recall(np.array([3, 4]), np.array([3, 4]), ["A", "A"])
    assert out["A"] == {"recall": None, "nSevere": 0}


# ------------------------------------------------------------------ the gate
def test_gate_accepts_a_healthy_audit():
    train.validate(_healthy())


def test_gate_fails_on_the_audit_that_shipped():
    """The old model passed every existing gate; this is the one that stops it."""
    audit = _healthy(raw=_OLD_RAW)
    with pytest.raises(train.ReleaseGateError, match=r"65\+ · Male.*0\.897"):
        train.validate(audit)


def test_gate_fails_when_only_the_served_pipeline_misses():
    served = dict(_healthy()["subgroupRedFlagRecall"]["served"])
    served["65+ · Female"] = {"recall": 0.925, "nSevere": 40}
    with pytest.raises(train.ReleaseGateError, match="served pipeline"):
        train.validate(_healthy(served=served))


def test_under_supported_subgroup_is_reported_not_gated():
    """Below the support floor one miss moves recall by more than the gate's
    whole 5-point tolerance, so the estimate cannot tell 0.95 from 0.90."""
    raw = dict(_healthy()["subgroupRedFlagRecall"]["raw"])
    raw["65+ · Male"] = {"recall": 0.8, "nSevere": MIN_SUBGROUP_SEVERE_SUPPORT - 1}
    train.validate(_healthy(raw=raw))
    assert train.subgroup_red_flag_shortfalls(_healthy(raw=raw)) == []


def test_gate_refuses_an_audit_without_subgroup_recall_evidence():
    audit = _healthy()
    del audit["subgroupRedFlagRecall"]
    with pytest.raises(train.ReleaseGateError, match="subgroupRedFlagRecall"):
        train.validate(audit)


def test_support_floor_is_the_smallest_n_that_tolerates_one_miss():
    n = MIN_SUBGROUP_SEVERE_SUPPORT
    assert (n - 1) / n >= MIN_RED_FLAG_RECALL
    assert (n - 2) / (n - 1) < MIN_RED_FLAG_RECALL


def test_report_surfaces_the_worst_subgroup():
    from app.ml import report

    assert report._worst_subgroup_recall(_healthy(raw=_OLD_RAW)) == ("0.897 / 1.000", False)
    assert report._worst_subgroup_recall(_healthy()) == ("1.000 / 1.000", True)
    assert report._worst_subgroup_recall({}) == ("n/a", False)


# ------------------------------------------------ the deployed model's audit
@pytest.fixture(scope="module")
def served_model():
    return model_module.get_model()


def test_served_counterfactual_matches_predict_end_to_end(served_model):
    """The audit's served-pipeline sex-flip result is reproduced through the
    real serving entry point (calibrated argmax + raise-only thresholds)."""
    cf = served_model.fairness()["counterfactualServed"]
    from app.ml.features import AGE_BANDS

    probes = model_module._COUNTERFACTUAL_PROBES
    assert cf["n"] == len(probes) * len(AGE_BANDS)
    flips = 0
    for text in probes:
        for band in AGE_BANDS:
            f = served_model.predict(text, band, "Female")["acuity_code"]
            m = served_model.predict(text, band, "Male")["acuity_code"]
            flips += f != m
    assert round(flips / cf["n"], 4) == cf["sexFlipRate"]


# ------------------------------------------------------ artifact integrity
def _write_artifact(tmp_path, *, sidecar: str | None):
    path = tmp_path / "careroute-triage-rf-abc.joblib"
    joblib.dump({"marker": 1}, path)
    if sidecar == "good":
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        (tmp_path / (path.name + ".sha256")).write_text(f"{digest}  {path.name}\n")
    elif sidecar == "bad":
        (tmp_path / (path.name + ".sha256")).write_text(f"{'0' * 64}  {path.name}\n")
    return path


@pytest.fixture()
def _any_payload_compatible(monkeypatch, tmp_path):
    monkeypatch.setenv("CAREROUTE_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(model_module, "_compatibility", lambda p: (True, "ok"))


@pytest.mark.usefixtures("_any_payload_compatible")
def test_load_accepts_an_artifact_matching_its_sidecar(tmp_path):
    path = _write_artifact(tmp_path, sidecar="good")
    payload, loaded = model_module._load_artifact()
    assert payload == {"marker": 1} and os.path.samefile(loaded, path)


@pytest.mark.usefixtures("_any_payload_compatible")
def test_load_rejects_an_artifact_whose_hash_does_not_match(tmp_path):
    _write_artifact(tmp_path, sidecar="bad")
    assert model_module._load_artifact() == (None, None)


@pytest.mark.usefixtures("_any_payload_compatible")
def test_load_rejects_an_artifact_with_no_sidecar(tmp_path):
    _write_artifact(tmp_path, sidecar=None)
    assert model_module._load_artifact() == (None, None)
