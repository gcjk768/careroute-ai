"""The test bootstrap's LLM kill switch must cover EVERY fixture scope.

conftest.py promises that no test ever shells out to a local CLI provider or calls
a hosted model. Until 2026-09-22 that promise was kept by a function-scoped
autouse fixture only, and pytest builds module-scoped fixtures BEFORE the
function-scoped ones of the first test that needs them - so the eval suites
(test_eval_clarifying_questions.outcomes, test_eval_continuity.report, ...)
ran the real provider chain. In CI no provider exists, so they fell through to
the deterministic path by accident; on a developer machine with a local CLI
provider on PATH they spent an hour of live model calls per run and failed on
whatever the model happened to answer.
"""
from __future__ import annotations

import asyncio

import pytest

from app import llm


def _probe() -> str:
    try:
        asyncio.run(llm.complete("system", "prompt"))
    except llm.LLMUnavailableError as exc:
        return str(exc)
    return "completed"


@pytest.fixture(scope="module")
def module_scoped_probe() -> str:
    return _probe()


@pytest.fixture(scope="session")
def session_scoped_probe() -> str:
    return _probe()


def test_function_scope_sees_the_kill_switch():
    assert _probe() == "LLM disabled in tests"


def test_module_scoped_fixtures_see_the_kill_switch(module_scoped_probe):
    assert module_scoped_probe == "LLM disabled in tests"


def test_session_scoped_fixtures_see_the_kill_switch(session_scoped_probe):
    assert session_scoped_probe == "LLM disabled in tests"


def test_the_llm_path_evals_are_told_to_skip():
    """`llm.health()` is not patched (the breaker tests need the real one), so
    the eval modules that gate on it are told to skip through the env switch
    they already honour; otherwise a laptop with a provider on PATH scores the
    LLM path against the disabled `complete` and fails on the fallback."""
    import os

    assert os.environ.get("CAREROUTE_SKIP_LLM_EVAL") == "1"

