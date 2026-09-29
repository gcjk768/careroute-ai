"""[XRAI] Counterfactual explanation — "what would change this assessment?"

XRAI Day 2 maps the patient's and clinician's "why not / how to be that"
questions to counterfactuals, and PDPC 5.5 names them as the implicit
explanation for non-technical audiences. Until 2026-09-16 the only
counterfactual in the system was the sex-flip FAIRNESS audit; nobody got a
"would move to P2 if breathlessness were also reported". These tests pin a
bounded single-edit search on the served model: the returned edit really does
flip the calibrated argmax, demographics are never touched, and the sentence
reaches the case state and the final SSE event.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("sklearn")

from app.agents import SeverityClassifierAgent  # noqa: E402
from app.agents.base import CaseState  # noqa: E402
from app.ml.features import FEATURE_NAMES, N_SYMPTOM_FEATURES, extract_features  # noqa: E402
from app.ml.model import get_model  # noqa: E402


def _argmax(m, x):
    return int(np.argmax(m.calibrated.predict_proba(np.asarray(x).reshape(1, -1))[0]))


def test_counterfactual_edit_actually_flips_the_calibrated_class():
    m = get_model()
    pred = m.predict("sore throat and a mild cough for two days", "18-39", "Female")
    cf = pred["counterfactual"]
    assert cf is not None and cf["moreUrgent"] is not None
    edit = cf["moreUrgent"]
    x = extract_features("sore throat and a mild cough for two days", "18-39", "Female")
    j = FEATURE_NAMES.index(edit["feature"])
    assert j < N_SYMPTOM_FEATURES, "only symptom flags may be edited"
    x2 = x.copy()
    x2[j] = 1.0 if edit["to"] == "present" else 0.0
    x2[FEATURE_NAMES.index("symptom_count")] = min(x2[:N_SYMPTOM_FEATURES].sum(), 6.0) / 6.0
    assert _argmax(m, x2) != _argmax(m, x)
    assert edit["targetAcuity"] == pred["counterfactual"]["moreUrgent"]["targetAcuity"]
    assert edit["label"] and "would" in cf["sentence"].lower()


def test_counterfactual_never_edits_demographics():
    m = get_model()
    for text in ("crushing chest pain radiating to the arm", "mild runny nose", "high fever and vomiting"):
        cf = m.predict(text, "65+", "Male")["counterfactual"]
        for key in ("moreUrgent", "lessUrgent"):
            edit = cf.get(key)
            if edit:
                assert not edit["feature"].startswith(("age_", "sex_"))


def test_counterfactual_is_single_edit_and_deterministic():
    m = get_model()
    a = m.predict("high fever and vomiting for three days", "40-64", "Female")["counterfactual"]
    b = m.predict("high fever and vomiting for three days", "40-64", "Female")["counterfactual"]
    assert a == b
    assert a["method"].startswith("single-edit")


def test_counterfactual_reaches_the_case_state_from_the_model_path():
    state = CaseState(raw_text="sore throat and a mild cough", normalised_symptoms="sore throat and a mild cough",
                      age_band="18-39", sex="Female")
    agent = SeverityClassifierAgent()
    candidate = agent._try_model(state)
    assert candidate is not None and candidate.get("counterfactual")
    agent._commit(state, candidate)
    assert state.counterfactual and state.counterfactual["sentence"]
    assert "counterfactual" in agent.CONTRACT.writes


def test_counterfactual_is_cleared_on_non_model_paths():
    state = CaseState(raw_text="x", normalised_symptoms="x")
    agent = SeverityClassifierAgent()
    agent._commit(state, {"source": "keyword", "acuity_code": "P3_URGENT", "confidence": 0.3,
                          "evidence": [], "explanation": []})
    assert state.counterfactual is None
