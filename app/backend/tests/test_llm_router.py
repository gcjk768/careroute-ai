"""[Agentic][MLOps] LLM ROUTER + response CACHE (ArchAAS Day 3 AM).

The course's LLM router does more than failover: it picks a model per task
(small and cheap where that is enough, stronger where it matters) and caches.
Until 2026-09-16 `llm.complete` only walked a fixed provider order with one
model each and cached nothing.

Pinned here:
  * a named task resolves to a model TIER, and each provider receives the
    tier's model; an unnamed call is byte-for-byte the old behaviour (no model
    argument at all, so existing four-argument provider fakes keep working);
  * the cache is EXACT-MATCH, opt-in per task, TTL- and size-bounded, never
    stores failures and never outranks the kill switch;
  * semantic caching is OFF by default — two different symptom descriptions
    must never share a triage answer. The opt-in semantic tier and its guards
    are pinned in tests/test_llm_gateway_controls.py.
"""
from __future__ import annotations

import asyncio

import pytest

from app import config, llm

# The root conftest autouse-patches `llm.complete` to raise. These tests are ABOUT
# `complete`, so the real coroutine is captured at import and restored per test.
_REAL_COMPLETE = llm.complete


class _Recorder:
    """A provider that records what it was asked, and can be told to fail."""

    def __init__(self, name: str = "openai", fail: bool = False):
        self.name = name
        self.fail = fail
        self.calls: list[dict] = []

    def label(self) -> str:
        return f"{self.name}:fake"

    async def available(self) -> bool:
        return True

    async def complete(self, system, prompt, json_mode, json_schema=None, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.fail:
            raise llm.LLMUnavailableError("down")
        return f"answer:{prompt}:{kwargs.get('model')}"


class _LegacyFourArg:
    """A provider fake written before the router existed: no **kwargs."""

    name = "openai"

    def label(self):
        return "legacy"

    async def available(self):
        return True

    async def complete(self, system, prompt, json_mode, json_schema=None):
        return "legacy-ok"


@pytest.fixture
def provider(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(llm, "complete", _REAL_COMPLETE)
    monkeypatch.setattr(llm, "_PROVIDERS", {"openai": rec})
    monkeypatch.setattr(config, "LLM_PROVIDER_ORDER", ["openai"])
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    monkeypatch.setenv("OPENAI_MODEL_DEEP", "deep-model")
    llm.reset_breakers()
    llm.reset_cache()
    yield rec
    llm.reset_cache()
    llm.reset_breakers()


def _run(coro):
    return asyncio.run(coro)


# --- routing -------------------------------------------------------------------------
def test_named_tasks_route_to_their_tier_model(provider):
    _run(llm.complete("s", "a", task="intake.normalise"))
    _run(llm.complete("s", "b", task="reflection.critic"))
    assert provider.calls[0]["model"] == "fast-model"
    assert provider.calls[1]["model"] == "deep-model"


def test_an_unnamed_call_passes_no_model_argument(provider):
    _run(llm.complete("s", "plain"))
    assert "model" not in provider.calls[0]


def test_legacy_provider_fakes_without_kwargs_still_work(monkeypatch):
    monkeypatch.setattr(llm, "complete", _REAL_COMPLETE)
    monkeypatch.setattr(llm, "_PROVIDERS", {"openai": _LegacyFourArg()})
    monkeypatch.setattr(config, "LLM_PROVIDER_ORDER", ["openai"])
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    llm.reset_breakers()
    assert _run(llm.complete("s", "p")) == "legacy-ok"


def test_unknown_task_falls_back_to_the_default_tier_uncached(provider):
    _run(llm.complete("s", "x", task="no.such.task"))
    _run(llm.complete("s", "x", task="no.such.task"))
    assert len(provider.calls) == 2
    assert "model" not in provider.calls[0]


def test_router_table_is_described_for_every_agent_task():
    table = llm.route_table()
    for task in ("intake.normalise", "classifier.classify", "safety.semantic",
                 "routing.select", "handoff.summary", "reflection.critic"):
        assert task in table
        assert table[task]["tier"] in {"fast", "deep"}
        assert isinstance(table[task]["cacheable"], bool)
    # Safety-critical and state-dependent tasks are never cached.
    assert table["safety.semantic"]["cacheable"] is False
    assert table["reflection.critic"]["cacheable"] is False
    assert table["routing.select"]["cacheable"] is False


def test_every_llm_call_in_the_app_is_routed():
    """The router can only pick a model by task weight if each call names its task.
    handoff, routing, the semantic red-flag layer and the interview question used to
    call llm.complete with no task, so they all ran on the default model whatever
    tier they needed (found 2026-09-27). Each call must name a task in ROUTES or pin
    its own model (the safety adjudicator's model_override)."""
    import ast
    import pathlib

    app_dir = pathlib.Path(llm.__file__).parent
    unrouted = []
    for path in app_dir.rglob("*.py"):
        if path.name == "llm.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "complete" and getattr(node.func.value, "id", "") == "llm"):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            if "model_override" in kw:
                continue
            task = kw.get("task")
            if isinstance(task, ast.Constant):
                ok = task.value in llm.ROUTES
            else:   # task=self.TASK / req.task: resolved at runtime, checked below
                ok = task is not None
            if not ok:
                unrouted.append(f"{path.relative_to(app_dir)}:{node.lineno}")
    assert not unrouted, f"llm.complete without a routed task: {unrouted}"


def test_runtime_tasks_are_routed():
    from app.agents.safety import SemanticRedFlagLayer
    assert SemanticRedFlagLayer.TASK in llm.ROUTES
    assert llm.ROUTES["classifier.question"].tier == "fast"


def test_code_defaults_apply_when_no_tier_env_is_set(monkeypatch):
    """AWS tasks set only OPENAI_MODEL; the tiers must still be the chosen GPT-5 models."""
    for tier in ("FAST", "DEEP", "MAX"):
        monkeypatch.delenv(f"OPENAI_MODEL_{tier}", raising=False)
    assert [llm._model_for("openai", t) for t in ("fast", "deep", "max")] == ["gpt-5.4-mini", "gpt-5.4", "gpt-5.5"]


def test_tier_models_come_from_the_environment(provider, monkeypatch):
    """Heavy tasks on the stronger model, light ones on the cheap one, hard cases on the top one."""
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    monkeypatch.setenv("OPENAI_MODEL_DEEP", "deep-model")
    monkeypatch.setenv("OPENAI_MODEL_MAX", "max-model")
    assert llm._model_for("openai", llm.ROUTES["intake.normalise"].tier) == "fast-model"
    assert llm._model_for("openai", llm.ROUTES["reflection.critic"].tier) == "deep-model"
    tier, reason = llm.choose_tier("deep", {"confidence": 0.3})
    assert (llm._model_for("openai", tier), reason) == ("max-model", "low_confidence")


def test_health_reports_the_model_per_tier(monkeypatch):
    """/api/health's `model` is only the base model; `tiers` is what routed calls use."""
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    for tier in ("DEEP", "MAX"):
        monkeypatch.delenv(f"OPENAI_MODEL_{tier}", raising=False)
    assert llm.tier_models("openai:gpt-4o-mini") == {"fast": "fast-model", "deep": "gpt-5.4", "max": "gpt-5.5"}
    assert llm.tier_models(None) == {}


# --- the per-request call log behind the result card's "AI models used" panel -------------
def test_each_call_is_logged_with_its_tier_model_and_reason(provider, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL_MAX", "max-model")
    log = llm.begin_call_log()
    _run(llm.complete("s", "a", task="intake.normalise"))
    _run(llm.complete("s", "b", task="reflection.critic", difficulty={"confidence": 0.3}))
    assert [(c["task"], c["tier"], c["model"], c["reason"]) for c in log] == [
        ("intake.normalise", "fast", "fast-model", "base"),
        ("reflection.critic", "max", "max-model", "low_confidence"),
    ]
    assert all(isinstance(c["ms"], int) and c["cached"] is False for c in log)


def test_a_cache_hit_is_logged_as_cached(provider):
    log = llm.begin_call_log()
    _run(llm.complete("s", "same", task="intake.normalise"))
    _run(llm.complete("s", "same", task="intake.normalise"))
    assert [c["cached"] for c in log] == [False, True]
    assert log[1]["model"] == "fast-model"


def test_nothing_is_logged_outside_a_request(provider):
    llm._CALL_LOG.set(None)
    _run(llm.complete("s", "a", task="intake.normalise"))   # must not raise or leak
    assert llm._CALL_LOG.get() is None


def test_the_final_event_carries_the_call_log(monkeypatch):
    """With the LLM down the list is empty — but always present, so the UI can rely on it."""
    import json as _json

    import app.main as main_mod
    from app.models import TriageRequest

    async def _no_delay(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "_step_delay", _no_delay)

    async def _collect():
        finals = []
        async for chunk in main_mod._triage_event_stream(TriageRequest(text="sore throat and runny nose for two days")):
            if chunk.startswith("data:"):
                e = _json.loads(chunk[5:])
                if e.get("event") == "final":
                    finals.append(e)
        return finals

    [final] = _run(_collect())
    assert final["llm"] == []


# --- cache ---------------------------------------------------------------------------------
def test_cacheable_task_is_served_from_cache_on_an_identical_prompt(provider):
    first = _run(llm.complete("s", "same", task="classifier.classify"))
    second = _run(llm.complete("s", "same", task="classifier.classify"))
    assert first == second
    assert len(provider.calls) == 1
    assert llm.cache_stats()["hits"] == 1


def test_a_different_prompt_is_a_miss(provider):
    _run(llm.complete("s", "one", task="classifier.classify"))
    _run(llm.complete("s", "two", task="classifier.classify"))
    assert len(provider.calls) == 2


def test_non_cacheable_task_always_reaches_the_provider(provider):
    _run(llm.complete("s", "same", task="reflection.critic"))
    _run(llm.complete("s", "same", task="reflection.critic"))
    assert len(provider.calls) == 2


def test_failures_are_never_cached(provider):
    provider.fail = True
    for _ in range(2):
        with pytest.raises(llm.LLMUnavailableError):
            _run(llm.complete("s", "boom", task="classifier.classify"))
    provider.fail = False
    llm.reset_breakers()
    assert _run(llm.complete("s", "boom", task="classifier.classify")).startswith("answer:")


def test_entries_expire_after_the_ttl(provider, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(llm, "_cache_ttl", lambda: 60.0)
    _run(llm.complete("s", "ttl", task="classifier.classify"))
    clock[0] += 61.0
    _run(llm.complete("s", "ttl", task="classifier.classify"))
    assert len(provider.calls) == 2


def test_cache_is_size_bounded_least_recently_used_first(provider, monkeypatch):
    monkeypatch.setattr(llm, "_cache_max", lambda: 2)
    for prompt in ("a", "b", "c"):
        _run(llm.complete("s", prompt, task="classifier.classify"))
    _run(llm.complete("s", "a", task="classifier.classify"))   # evicted -> miss
    assert [c["prompt"] for c in provider.calls] == ["a", "b", "c", "a"]


def test_kill_switch_beats_the_cache(provider, monkeypatch):
    _run(llm.complete("s", "cached", task="classifier.classify"))
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    with pytest.raises(llm.LLMUnavailableError):
        _run(llm.complete("s", "cached", task="classifier.classify"))


def test_cache_can_be_disabled(provider, monkeypatch):
    monkeypatch.setattr(llm, "_cache_ttl", lambda: 0.0)
    _run(llm.complete("s", "same", task="classifier.classify"))
    _run(llm.complete("s", "same", task="classifier.classify"))
    assert len(provider.calls) == 2
