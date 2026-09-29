"""Tests for LLM token/cost accounting (app.llm_cost).

The course's efficiency gate is "cost per task <= $0.12 / p95 latency <= 8000 ms".
CareRoute measured neither: `llm.py` threw the provider's `usage` block away, so
the token cost of a triage was literally unknown. Latency arrived with the
Locust job; this is the other half.

The rule that matters most here is that an UNPRICED model must not report
$0.00. A silent zero is indistinguishable from "this triage was free", and the
one number nobody should ever trust is a cost that defaults to nothing.
"""
from __future__ import annotations

import pytest

from app import llm_cost


def test_known_model_cost_uses_separate_input_and_output_rates():
    # gpt-4o-mini: $0.15 / 1M input, $0.60 / 1M output.
    cost = llm_cost.cost_usd("gpt-4o-mini", prompt_tokens=1_000_000, completion_tokens=0)
    assert cost == pytest.approx(0.15)

    cost = llm_cost.cost_usd("gpt-4o-mini", prompt_tokens=0, completion_tokens=1_000_000)
    assert cost == pytest.approx(0.60)


def test_cost_is_the_sum_of_both_directions():
    cost = llm_cost.cost_usd("gpt-4o-mini", prompt_tokens=500_000, completion_tokens=500_000)
    assert cost == pytest.approx(0.075 + 0.30)


def test_unknown_model_is_unpriced_not_free():
    usage = llm_cost.record("some-model-we-never-priced", {"prompt_tokens": 100, "completion_tokens": 50})

    assert usage["priced"] is False
    assert usage["costUsd"] is None
    assert usage["totalTokens"] == 150


def test_known_model_is_priced():
    usage = llm_cost.record("gpt-4o-mini", {"prompt_tokens": 1000, "completion_tokens": 1000})

    assert usage["priced"] is True
    assert usage["costUsd"] == pytest.approx(0.00015 + 0.0006)


def test_missing_usage_block_does_not_raise():
    usage = llm_cost.record("gpt-4o-mini", None)

    assert usage["totalTokens"] == 0
    assert usage["costUsd"] == pytest.approx(0.0)


def test_partial_usage_block_is_tolerated():
    """Providers vary; a missing completion count must not crash a triage."""
    usage = llm_cost.record("gpt-4o-mini", {"prompt_tokens": 10})

    assert usage["promptTokens"] == 10
    assert usage["completionTokens"] == 0


def test_model_name_matching_ignores_version_suffixes():
    """`gpt-4o-mini-2024-07-18` must price as `gpt-4o-mini`, not as unknown."""
    assert llm_cost.cost_usd("gpt-4o-mini-2024-07-18", 1_000_000, 0) == pytest.approx(0.15)


def test_recording_never_raises_when_metrics_are_disabled(monkeypatch):
    monkeypatch.setattr(llm_cost.metrics, "enabled", lambda: False)

    usage = llm_cost.record("gpt-4o-mini", {"prompt_tokens": 5, "completion_tokens": 5})

    assert usage["totalTokens"] == 10
