"""[Responsible-AI] POST-PROCESSING fairness mitigation + gated Equal Opportunity.

XRAI Day 2 teaches mitigation at three stages — pre-processing (this project
already oversamples age bands), in-processing, and post-processing (adjusting
decisions after the model). Until 2026-09-16 the named fairness metrics were
reported but nothing gated them and no post-processing step existed. The
served model now carries per-group severe thresholds derived on an unseen
sample: for a group whose severe-class true-positive rate trails the best
group, a P3–P5 argmax is RAISED to P2 when the calibrated P(severe) clears
that group's threshold. Raise-only by design — it can only catch more
emergencies, never dismiss one — and deterministic, unlike a randomised
threshold optimiser. The Equal Opportunity gap after mitigation is gated.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("sklearn")

from app.ml import data, fairness  # noqa: E402
from app.ml.features import extract_features  # noqa: E402
from app.ml.model import MAX_EQUAL_OPPORTUNITY_GAP, get_model  # noqa: E402
from app.ml.train import ReleaseGateError, validate  # noqa: E402


@pytest.fixture(scope="module")
def pp() -> dict:
    return get_model().fairness()["postProcessing"]


def test_thresholds_are_per_group_raise_only_and_bounded():
    m = get_model()
    assert m.severe_thresholds, "served model must carry per-group severe thresholds"
    for group, t in m.severe_thresholds.items():
        assert " · " in group
        assert 0.0 < t <= 1.0
    # Deriving thresholds directly: a group already at the target TPR keeps the
    # "never raise" threshold (1.0); a trailing group gets a lower one.
    # 40 rows: groups alternate A/B, every group has severe (0) and routine (3) rows.
    groups = ["A" if i % 2 == 0 else "B" for i in range(40)]
    y = np.array([0 if (i // 2) % 2 == 0 else 3 for i in range(40)])
    # Group A's severe cases score 0.9 (caught); group B's only 0.45 (missed by
    # the argmax rule); routine rows score 0.1 everywhere.
    p_severe = np.array([
        (0.9 if g == "A" else 0.45) if y[i] == 0 else 0.1 for i, g in enumerate(groups)
    ], dtype=float)
    pred_severe = p_severe >= 0.5
    th = fairness.equal_opportunity_thresholds(p_severe, pred_severe, y, groups)
    assert th["A"] == 1.0
    assert th["B"] < 0.5


def test_post_processing_never_lowers_an_acuity():
    m = get_model()
    X, _, groups = data.generate_dataset(n=600, seed=321)
    proba = m.calibrated.predict_proba(X)
    base = proba.argmax(axis=1)
    adjusted = m.apply_post_processing(proba, groups)
    assert np.all(adjusted <= base)                       # lower index == more urgent
    assert np.all((adjusted == base) | (adjusted == 1))   # a raise lands exactly on P2


def test_audit_reports_before_and_after_on_the_calibrated_pipeline(pp):
    for phase in ("before", "after"):
        blk = pp[phase]
        for key in ("equalOpportunityGap", "equalizedOddsGap", "disparateImpactRatio", "redFlagRecall", "overallAccuracy"):
            assert 0.0 <= blk[key] <= 1.0
    assert pp["after"]["redFlagRecall"] >= pp["before"]["redFlagRecall"] - 1e-9
    assert pp["after"]["equalOpportunityGap"] <= pp["before"]["equalOpportunityGap"] + 1e-9
    assert "raise-only" in pp["method"]
    assert pp["thresholds"] == get_model().severe_thresholds


def test_equal_opportunity_gap_after_mitigation_is_gated(pp):
    assert pp["after"]["equalOpportunityGap"] <= MAX_EQUAL_OPPORTUNITY_GAP


def test_release_gate_rejects_a_wide_equal_opportunity_gap():
    audit = dict(get_model().fairness())
    pp = dict(audit["postProcessing"])
    pp["after"] = {**pp["after"], "equalOpportunityGap": MAX_EQUAL_OPPORTUNITY_GAP + 0.05}
    audit["postProcessing"] = pp
    with pytest.raises(ReleaseGateError, match="equal opportunity"):
        validate(audit)


def test_served_prediction_reports_the_post_processing_decision():
    m = get_model()
    pred = m.predict("mild runny nose and sneezing", "18-39", "Female")
    blk = pred["postProcessing"]
    assert blk["group"] == "18-39 · Female"
    assert isinstance(blk["applied"], bool)
    assert 0.0 < blk["threshold"] <= 1.0
    # A raise, when it happens, lands on P2 and the reported confidence is P(severe).
    x = extract_features("mild runny nose and sneezing", "18-39", "Female")
    proba = m.calibrated.predict_proba(x.reshape(1, -1))[0]
    if blk["applied"]:
        assert pred["acuity_code"] == "P2_EMERGENT"
        assert abs(pred["confidence"] - round(float(proba[:2].sum()), 3)) < 1e-6
    else:
        assert pred["acuity_code"] == data.ACUITY_INDEX_TO_CODE[int(proba.argmax())]


def test_served_probability_does_not_depend_on_sex():
    """[Responsible-AI] The calibrated model is scored sex-blind, so sex can act
    only through the explicit per-group thresholds. Model 2fccc97c3aa3 put
    "dizzy and lightheaded when standing" (18-39) at P(severe) 0.288 for a
    woman and 0.328 for a man, either side of the 0.30 raise threshold: P4
    versus P2 for the same text."""
    import numpy as np

    from app.ml.features import extract_features
    from app.ml.model import get_model

    model = get_model()
    for text in ("dizzy and lightheaded when standing", "chest pain", "mild sore throat"):
        for band in ("0-17", "18-39", "40-64", "65+"):
            female = model.calibrated.predict_proba(extract_features(text, band, "Female"))
            male = model.calibrated.predict_proba(extract_features(text, band, "Male"))
            assert np.allclose(female, male), (text, band)
