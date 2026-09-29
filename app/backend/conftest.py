"""Test bootstrap.

1. Ensures `app/backend` (this directory) is on sys.path so tests can
   `import app...` regardless of the working directory pytest runs from.
2. Globally forces the deterministic (rule-based) path in every test — no test
   ever shells out to a local CLI provider or calls the OpenAI API. This keeps the
   suite fast, reproducible, and network-free, and directly exercises the hard
   "runs without a model" guarantee.
"""
import sys
from pathlib import Path

import pytest


_CARE_ROUTING_TEST_FILES = {"test_routing.py", "test_llm_openai.py"}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Print a concise outcome for the care-routing demonstration tests.

    Use ``pytest -s`` when running them locally so the purpose and outcome are
    visible beside pytest's standard report. Other backend tests are unchanged.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or item.path.name not in _CARE_ROUTING_TEST_FILES:
        return
    description = (getattr(item.function, "__doc__", "") or item.name).strip().splitlines()[0]
    status = "PASS" if report.passed else "FAIL"
    print(f"[CARE ROUTING {status}] {description}")

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def pytest_configure(config):
    """Import the embedding backend on the MAIN thread before any test runs.

    Only does anything where the optional `fastembed` extra is installed (not
    in CI). It is the same guard the API applies at startup: the first import
    of onnxruntime from a worker thread — which is where every retrieval runs,
    via `asyncio.to_thread` — segfaults the interpreter on Windows instead of
    raising. Without this, installing requirements-agentic.txt turns a green
    local suite into an access violation in tests/agents/test_classifier.py.
    See `app.rag_embed.warm`.
    """
    try:
        from app import rag_embed

        rag_embed.warm()
    except Exception:  # noqa: BLE001 - never let a warm-up stop the suite from running
        pass


async def _llm_disabled(*_args, **_kwargs):
    from app.llm import LLMUnavailableError

    raise LLMUnavailableError("LLM disabled in tests")


@pytest.fixture(scope="session", autouse=True)
def _disable_llm_for_the_session():
    """Make every agent LLM call raise, so the deterministic fallback runs.

    Every worker calls the LLM via `llm.complete(...)` (through the `llm`
    module, not a bare import), so patching `llm.complete` here is the single
    kill-switch that forces the deterministic path in ALL agents at once —
    regardless of how the agents package is split into per-agent files.

    SESSION-scoped on purpose. pytest builds module- and session-scoped
    fixtures before the function-scoped ones of the first test that needs them,
    so a function-scoped switch alone leaves every module-scoped eval fixture
    (test_eval_clarifying_questions.outcomes, test_eval_continuity.report, ...)
    on the REAL provider chain. In CI no provider exists and they fell through
    to the deterministic path by accident; on a machine with a local CLI
    provider on PATH they spent an hour of live model calls per run and failed
    on whatever the model answered (tests/test_conftest_llm_kill_switch.py).
    """
    import app.llm as llm

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(llm, "complete", _llm_disabled)
        # The LLM-path evals (tests/test_eval_intake.py) decide whether to run
        # by `llm.health()`, which probes the providers rather than calling
        # `complete`. On a laptop with a local CLI provider on PATH the probe says
        # "available", the module then scores a path this switch has disabled,
        # and four tests fail on the deterministic fallback's numbers. CI has no
        # provider and skips them; make the laptop do the same.
        mp.setenv("CAREROUTE_SKIP_LLM_EVAL", "1")
        # Tracing sinks OFF too: a developer's repo-root .env may hold real
        # LangSmith/Langfuse keys, and the suite must never ship spans to them.
        # tests/test_tracing.py injects fake sinks where it needs one.
        from app import config, tracing
        for key in ("LANGSMITH_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            mp.setattr(config, key, "")
        tracing._langfuse.cache_clear()
        tracing._langsmith.cache_clear()
        yield


@pytest.fixture(autouse=True)
def _disable_llm(monkeypatch):
    """Per-test re-arm of the session switch above: a test that swaps in its
    own `llm.complete` (or calls `monkeypatch.undo()`) is restored to the
    disabled one, never to the real provider chain."""
    import app.llm as llm

    monkeypatch.setattr(llm, "complete", _llm_disabled)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Clear the shared rate limiter before each test so per-test request bursts
    never collide with the fixed-window limit (and stay independent)."""
    try:
        from app.main import abuse_monitor, rate_limiter
        rate_limiter.reset()
        abuse_monitor.reset()
    except Exception:
        pass
    yield


@pytest.fixture(scope="session", autouse=True)
def _hitl_memory_off_disk(tmp_path_factory):
    """[HITL] Precedent memory is READ on every HITL run, so a developer's real
    backend/monitoring/precedents.jsonl (or a decision another test made) would
    make HITL verdicts order- and machine-dependent. Session scope so module-
    scoped eval fixtures are covered too; per-test isolation below."""
    import os

    base = tmp_path_factory.mktemp("hitl_memory")
    os.environ["CAREROUTE_PRECEDENT_LOG"] = str(base / "precedents.jsonl")
    os.environ["CAREROUTE_RETRAIN_QUEUE"] = str(base / "retrain_queue.jsonl")


@pytest.fixture(autouse=True)
def _hitl_memory_per_test(monkeypatch, tmp_path):
    monkeypatch.setenv("CAREROUTE_PRECEDENT_LOG", str(tmp_path / "precedents.jsonl"))
    monkeypatch.setenv("CAREROUTE_RETRAIN_QUEUE", str(tmp_path / "retrain_queue.jsonl"))
