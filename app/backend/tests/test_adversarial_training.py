"""[AI-Security] Adversarial re-training (AIC Day 1's model-side defence).

`app/ml/adversarial.py` is a measurable experiment rather than part of the served
training path -- see its module docstring for why. These tests pin the properties
that decide whether the experiment is worth believing, and keep the expensive
black-box attack to the one test that genuinely needs it.
"""
from __future__ import annotations

import numpy as np
import pytest
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split

from app.ml import adversarial, data


@pytest.fixture(scope="module")
def split():
    X, y, _ = data.generate_dataset(n=1200, seed=42)
    return train_test_split(X, y, test_size=0.25, stratify=y, random_state=42)


@pytest.fixture(scope="module")
def base_model(split):
    X_tr, _, y_tr, _ = split
    return RandomForestClassifier(n_estimators=60, random_state=42, n_jobs=-1).fit(X_tr, y_tr)


def test_adversarial_rows_are_labelled_from_the_row_they_were_perturbed_from(
    monkeypatch, split, base_model
):
    """The deck says "with CORRECT labels", and that detail is the whole defence.

    This is also the test that catches the subtle version of getting it wrong. The
    in-budget filter DROPS rows, so the survivors no longer line up positionally
    with the sample that produced them: a caller that re-derived the sample and
    truncated it would hand row 5's label to row 2's attack. Silently mislabelled
    training data is the poisoning failure this module exists to avoid, so the
    generator returns the source indices and they are asserted here.

    The attack is stubbed, so this tests the labelling contract deterministically.
    """
    X_tr, _, y_tr, _ = split
    # Survivors are NON-CONTIGUOUS on purpose -- rows 7, 2 and 9, in that order.
    source_idx = np.array([7, 2, 9])
    fake_adv = X_tr[source_idx].astype(np.float32).copy()
    monkeypatch.setattr(
        adversarial, "generate_adversarial_examples", lambda *a, **k: (fake_adv, source_idx)
    )
    captured: dict = {}

    class _Spy(RandomForestClassifier):
        def fit(self, X, y, **kw):
            captured["y_aug"] = np.asarray(y)
            return super().fit(X, y, **kw)

    monkeypatch.setattr(adversarial, "RandomForestClassifier", _Spy)

    _, n_adv = adversarial.adversarially_retrain(base_model, X_tr, y_tr, n=3, seed=42)

    assert n_adv == 3
    appended = captured["y_aug"][len(y_tr):]
    # Exactly y_tr[7], y_tr[2], y_tr[9] -- not y_tr[0:3], and not a prediction.
    assert list(appended) == list(y_tr[source_idx])


def test_no_adversarial_examples_returns_the_model_unchanged(monkeypatch, split, base_model):
    """An attack that finds nothing in budget must not silently retrain on noise."""
    X_tr, _, y_tr, _ = split
    monkeypatch.setattr(
        adversarial,
        "generate_adversarial_examples",
        lambda *a, **k: (np.empty((0, X_tr.shape[1])), np.empty(0, dtype=int)),
    )

    returned, n_adv = adversarial.adversarially_retrain(base_model, X_tr, y_tr)

    assert returned is base_model and n_adv == 0


def test_generated_examples_stay_inside_the_perturbation_budget(split, base_model):
    """The expensive one. An out-of-budget "adversarial" row is just a different
    patient, and training on it as if it were an attack proves nothing."""
    pytest.importorskip("art", reason="adversarial-robustness-toolbox (art) not installed")
    X_tr, _, _, _ = split

    x_adv, source_idx = adversarial.generate_adversarial_examples(
        base_model, X_tr, n=6, max_iter=4
    )

    assert x_adv.shape[1] == X_tr.shape[1]
    assert len(source_idx) == len(x_adv)
    # Each survivor is within EPSILON of THE ROW IT CLAIMS TO COME FROM -- not
    # merely of some row somewhere, which a mis-mapped index would also satisfy.
    if len(x_adv):
        drift = np.max(np.abs(x_adv - X_tr[source_idx]), axis=1)
        assert np.all(drift <= adversarial.EPSILON + 1e-6)
