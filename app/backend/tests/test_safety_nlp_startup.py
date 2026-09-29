"""FastAPI lifecycle coverage for the live Safety NLP runtime."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app import config
import app.main as main_module
import app.safety_nlp as safety_nlp


pytestmark = pytest.mark.safety


@pytest.fixture(autouse=True)
def restore_live_adapters():
    previous = main_module.orchestrator.safety.nlp_adapters
    yield
    main_module.orchestrator.safety.set_nlp_adapters(previous)


class RecordingRuntime:
    adapters = SimpleNamespace(name="live-adapters")
    stage_statuses = {"ner": "success", "assertion": "success", "category": "success"}
    warmed = False

    @classmethod
    def from_manifest_file(cls, *_args, **_kwargs):
        return cls()

    async def awarmup(self):
        self.warmed = True


@pytest.mark.asyncio
async def test_startup_builds_warms_and_injects_live_runtime(monkeypatch):
    monkeypatch.setattr(config, "SAFETY_NLP_ENABLED", True)
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    monkeypatch.setattr(safety_nlp, "SafetyNlpRuntime", RecordingRuntime)
    app = FastAPI()

    await main_module._configure_safety_nlp_runtime(app)

    assert app.state.safety_nlp_runtime.warmed is True
    assert main_module.orchestrator.safety.nlp_adapters is RecordingRuntime.adapters
    assert app.state.safety_nlp_status["category"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [FileNotFoundError("manifest"), TimeoutError("warmup")])
async def test_startup_failure_degrades_to_unavailable(monkeypatch, failure):
    class FailingRuntime(RecordingRuntime):
        @classmethod
        def from_manifest_file(cls, *_args, **_kwargs):
            if isinstance(failure, FileNotFoundError):
                raise failure
            return cls()

        async def awarmup(self):
            raise failure

    monkeypatch.setattr(config, "SAFETY_NLP_ENABLED", True)
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    monkeypatch.setattr(safety_nlp, "SafetyNlpRuntime", FailingRuntime)
    app = FastAPI()

    await main_module._configure_safety_nlp_runtime(app)

    assert app.state.safety_nlp_runtime is None
    assert app.state.safety_nlp_status == "unavailable"
    assert main_module.orchestrator.safety.nlp_adapters is None


@pytest.mark.asyncio
async def test_kill_switch_skips_live_runtime_construction(monkeypatch):
    class UnexpectedRuntime(RecordingRuntime):
        @classmethod
        def from_manifest_file(cls, *_args, **_kwargs):
            raise AssertionError("runtime must not be constructed under the kill switch")

    monkeypatch.setattr(config, "SAFETY_NLP_ENABLED", True)
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    monkeypatch.setattr(safety_nlp, "SafetyNlpRuntime", UnexpectedRuntime)
    app = FastAPI()

    await main_module._configure_safety_nlp_runtime(app)

    assert app.state.safety_nlp_status == "disabled"
    assert main_module.orchestrator.safety.nlp_adapters is None
