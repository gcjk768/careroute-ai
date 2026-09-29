"""[MLOps] Train/serve skew gate — the cheap half of a feature store.

The test that carries the weight is `test_the_probe_set_covers_every_symptom_feature`:
a behavioural fingerprint only sees what its probes exercise, so an uncovered
category is a hole in the gate that nothing else would report.
"""
import numpy as np
import pytest

from app.ml import feature_contract as fc
from app.ml import model as model_module
from app.ml.features import FEATURE_NAMES, N_SYMPTOM_FEATURES

pytestmark = pytest.mark.eval


def test_the_probe_set_covers_every_symptom_feature():
    """An unprobed category could change meaning without moving the hash."""
    vectors = np.asarray(fc.vectors())
    fired = vectors[:, :N_SYMPTOM_FEATURES].sum(axis=0)
    uncovered = [FEATURE_NAMES[i] for i, n in enumerate(fired) if n == 0]
    assert not uncovered, f"no probe lights up: {uncovered}"


def test_the_fingerprint_is_stable_across_calls():
    assert fc.fingerprint() == fc.fingerprint()


def test_a_keyword_added_to_an_existing_category_moves_the_fingerprint():
    """The edit this repository actually makes. Names and dimension unchanged,
    meaning changed — invisible to the `featureNames` check that came before."""
    from app.ml import features

    before = fc.fingerprint()
    original = features.FEATURE_KEYWORDS["cough"]
    try:
        features.FEATURE_KEYWORDS["cough"] = [*original, "hacking"]
        assert fc.fingerprint() != before
    finally:
        features.FEATURE_KEYWORDS["cough"] = original
    assert fc.fingerprint() == before


def test_a_matcher_change_that_moves_a_probe_vector_moves_the_fingerprint(monkeypatch):
    """The other half: the tables are untouched, the logic is not."""
    from app.ml import features

    before = fc.fingerprint()
    monkeypatch.setattr(features, "_DEFAULT_BAND", "18-39")
    assert fc.fingerprint() != before


def test_a_missing_contract_fails_closed():
    verdict = fc.check(None)
    assert verdict["ok"] is False
    assert "no feature contract" in verdict["reason"]


def test_a_matching_contract_passes():
    assert fc.check(fc.contract())["ok"] is True


def test_layout_and_meaning_are_reported_differently():
    """An operator reading the log needs to know which one happened."""
    renamed = {**fc.contract(), "featureNames": ["something_else"]}
    assert "layout" in fc.check(renamed)["reason"]

    moved = {**fc.contract(), "fingerprint": "0" * 64}
    assert "MEANING" in fc.check(moved)["reason"]


def test_an_artifact_whose_features_changed_meaning_is_not_served():
    """The gate's actual job: `_compatibility` refuses it, so `_load_artifact`
    rebuilds instead of scoring on features the model never saw."""
    payload = {
        "schemaVersion": model_module._ARTIFACT_SCHEMA,
        "featureNames": list(FEATURE_NAMES),
        "featureContract": {**fc.contract(), "fingerprint": "0" * 64},
        "served": object(),
        "calibrated": object(),
    }
    usable, reason = model_module._compatibility(payload)
    assert usable is False and "MEANING" in reason


def test_an_artifact_from_before_the_contract_existed_is_not_served():
    payload = {
        "schemaVersion": model_module._ARTIFACT_SCHEMA,
        "featureNames": list(FEATURE_NAMES),
        "served": object(),
        "calibrated": object(),
    }
    usable, reason = model_module._compatibility(payload)
    assert usable is False and "no feature contract" in reason


def test_a_current_artifact_is_served():
    payload = {
        "schemaVersion": model_module._ARTIFACT_SCHEMA,
        "featureNames": list(FEATURE_NAMES),
        "featureContract": fc.contract(),
        "served": object(),
        "calibrated": object(),
    }
    assert model_module._compatibility(payload)[0] is True


def test_the_gate_says_SKIPPED_rather_than_passing_when_there_is_no_artifact(monkeypatch, capsys):
    """Silent success would read as 'checked and fine'."""
    monkeypatch.setattr(model_module, "_load_artifact", lambda: (None, None))
    assert fc.main(["--check"]) == 0
    assert "SKIPPED" in capsys.readouterr().out


def test_the_gate_exits_non_zero_on_skew(monkeypatch, capsys):
    payload = {"featureContract": {**fc.contract(), "fingerprint": "0" * 64}}
    monkeypatch.setattr(model_module, "_load_artifact", lambda: (payload, "models/fake.joblib"))
    assert fc.main(["--check"]) == 1
    assert "FAILED" in capsys.readouterr().out
