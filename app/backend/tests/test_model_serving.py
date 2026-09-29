"""The served model must not stall the event loop or spawn worker pools."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.agents import SeverityClassifierAgent
from app.agents.base import CaseState

pytest.importorskip("sklearn")


def _forests(model):
    yield model.model
    for calibrated in getattr(model.calibrated, "calibrated_classifiers_", []):
        yield calibrated.estimator


def test_the_served_forests_run_single_threaded():
    """Trained with n_jobs=-1, every predict_proba spawned and tore down a
    worker pool: 10-26 s per prediction on a laptop, several times per case.
    Serving sets the forests to one job at load time."""
    from app.ml.model import get_model

    for forest in _forests(get_model()):
        assert getattr(forest, "n_jobs", 1) == 1


def test_classifier_inference_does_not_block_the_event_loop(monkeypatch):
    agent = SeverityClassifierAgent()

    def _slow_model(state):
        time.sleep(0.4)
        return {"source": "model", "acuity_code": "P4_NON_URGENT", "confidence": 0.9,
                "evidence": ["sore throat"], "explanation": [{"feature": "sore throat", "weight": 1.0}]}

    monkeypatch.setattr(agent, "_try_model", _slow_model)

    async def scenario() -> int:
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.02)

        task = asyncio.create_task(ticker())
        await agent.run(CaseState(raw_text="sore throat", normalised_symptoms="sore throat"))
        stop = True
        await task
        return ticks

    assert asyncio.run(scenario()) >= 5
