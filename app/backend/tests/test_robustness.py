"""[AI-Security] ADVERSARIAL-ROBUSTNESS gate (IBM ART evasion attack).

Course aspect "AI-Security": beyond LLM red-teaming, the *classical* severity
model is itself an attack surface — an adversary can perturb the input feature
vector to force a mis-triage (evade a P1 into a P5). This test runs a real ART
black-box evasion attack (HopSkipJump) against the deployed RandomForest and
asserts a robustness FLOOR: the model must not be evadable by SMALL (<= epsilon)
input perturbations. A large perturbation is not an "attack" — it's a genuinely
different presentation — so we only count in-budget flips as successful evasions.

`adversarial-robustness-toolbox` (import name `art`) is a DEV-only dependency
(requirements-dev.txt). The test SKIPS cleanly if it isn't installed, so the
core suite still runs everywhere.
"""
from __future__ import annotations

import numpy as np
import pytest

# Skip the whole module cleanly when IBM ART isn't installed.
art = pytest.importorskip("art", reason="adversarial-robustness-toolbox (art) not installed")

from art.attacks.evasion import HopSkipJump  # noqa: E402
from art.estimators.classification import SklearnClassifier  # noqa: E402

from app.ml import data  # noqa: E402
from app.ml.model import get_model  # noqa: E402

# L-infinity perturbation budget (features live in [0,1]; a one-hot / symptom
# flag needs a >=0.5 change to flip, so a small budget is a meaningful test).
EPSILON = 0.25
# The model must survive at least this fraction of in-budget evasion attempts.
MIN_ROBUST_ACCURACY = 0.5
# Keep the black-box query budget small so the gate runs in CI time.
N_SAMPLES = 12


def test_random_forest_resists_small_evasion_perturbations():
    rf = get_model().model  # the raw served RandomForest (SHAP/fairness estimator)

    X, y, _ = data.generate_dataset(n=400, seed=2024)
    # Attack only samples the model classifies correctly, so a flip is a true evasion.
    pred = rf.predict(X)
    correct = np.where(pred == y)[0]
    idx = correct[:N_SAMPLES]
    x0 = X[idx].astype(np.float32)

    classifier = SklearnClassifier(model=rf, clip_values=(0.0, 1.0))
    attack = HopSkipJump(
        classifier=classifier, targeted=False,
        max_iter=8, max_eval=200, init_eval=10, init_size=10,
    )
    x_adv = attack.generate(x=x0)

    # In-budget evasion = the label flipped AND the perturbation stayed <= EPSILON.
    linf = np.max(np.abs(x_adv - x0), axis=1)
    flipped = rf.predict(x_adv) != rf.predict(x0)
    in_budget_evasion = flipped & (linf <= EPSILON)
    robust_accuracy = 1.0 - float(np.mean(in_budget_evasion))

    assert robust_accuracy >= MIN_ROBUST_ACCURACY, (
        f"robust accuracy {robust_accuracy:.3f} below floor {MIN_ROBUST_ACCURACY} "
        f"under an L-inf<={EPSILON} evasion budget"
    )
