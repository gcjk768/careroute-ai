"""[MLOps] Token and cost accounting for LLM calls — the efficiency signal.

The course's agentic efficiency gate is "cost per task <= $0.12, p95 latency
<= 8000 ms". CareRoute measured neither. Latency arrived with the Locust job;
this module is the other half: `llm.py` parsed the provider response for its
content and discarded the `usage` block, so the token cost of a triage was
simply unknown — and an agent pipeline whose per-request cost is unknown is one
prompt change away from a bill nobody predicted.

An UNPRICED MODEL REPORTS `None`, NEVER `0.0`. A silent zero is
indistinguishable from "this triage was free", which is precisely the reading
that would let an unpriced model run up a bill while the dashboard stays flat.
Tokens are still counted for an unpriced model; only the money is withheld.

Prices are USD per 1,000,000 tokens and are a point-in-time snapshot — they are
published rates, they move, and nothing here can detect that they have. Treat
the cost as an order-of-magnitude operating signal, not an invoice.
"""
from __future__ import annotations

import contextlib

from app import metrics

# USD per 1M tokens: (input, output). Keys are matched as a longest prefix, so a
# dated release ("gpt-4o-mini-2024-07-18") prices as its base model.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    # GPT-5 family, standard tier, from developers.openai.com/api/docs/pricing (read 2026-09-27).
    # Longest-prefix match keeps "gpt-5.4-mini" from pricing as "gpt-5.4".
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.5": (5.00, 30.00),
}

_PER_TOKEN = 1_000_000


def _rates(model: str) -> tuple[float, float] | None:
    """Longest-prefix match so version suffixes price as their base model."""
    if not model:
        return None
    matches = [key for key in PRICING if model.startswith(key)]
    if not matches:
        return None
    return PRICING[max(matches, key=len)]


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """USD for one call, or None when the model has no published rate here."""
    rates = _rates(model)
    if rates is None:
        return None
    in_rate, out_rate = rates
    return (prompt_tokens * in_rate + completion_tokens * out_rate) / _PER_TOKEN


def record(model: str, usage: dict | None) -> dict:
    """Normalise a provider `usage` block, record the metrics, return the row.

    Never raises: a triage must not fail because accounting did. A provider that
    omits `usage` yields zeros, which is honest — no tokens were observed.
    """
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    cost = cost_usd(model, prompt, completion)

    row = {
        "model": model,
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": total,
        "costUsd": round(cost, 6) if cost is not None else None,
        "priced": cost is not None,
    }

    # Accounting must never break a triage.
    with contextlib.suppress(Exception):
        if metrics.enabled():
            metrics.observe_llm_usage(model, prompt, completion, cost)
    return row


def estimate_tokens(text: str) -> int:
    """~4 characters per token (the usual English rule of thumb for BPE models)."""
    return (len(text) + 3) // 4 if text else 0


def record_estimated(model: str, prompt_text: str, completion_text: str) -> dict:
    """Meter a call whose provider returned no `usage` block.

    `llm.complete` calls this for every provider that does not account its own
    tokens (only OpenAIProvider does), so no provider path is ever unmetered.
    Marked `estimated` because it is a character-count proxy, not a bill line.
    # ponytail: chars/4 heuristic; swap in the provider's tokenizer if a
    # non-OpenAI provider ever carries real traffic.
    """
    row = record(model, {"prompt_tokens": estimate_tokens(prompt_text),
                         "completion_tokens": estimate_tokens(completion_text)})
    row["estimated"] = True
    return row
