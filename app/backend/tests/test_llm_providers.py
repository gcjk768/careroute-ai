"""Production provider surface: the hosted OpenAI API is the only LLM provider.

The local CLI provider shelled out to a command-line tool on a developer's
machine and the Ollama provider called a self-hosted server on localhost.
Neither exists in the deployed service, and a provider that resolves to a
laptop tool is a way for a dev machine's credentials to answer production
traffic. Both were removed on 2026-09-23; this pins that they stay out.
"""
from __future__ import annotations

import pytest

from app import config, llm


def test_openai_is_the_only_registered_provider():
    assert set(llm._PROVIDERS) == {"openai"}


def test_the_local_tool_providers_are_gone():
    for name in ("ClaudeCLIProvider", "OllamaProvider", "OLLAMA_BASE_URL", "_resolve_claude_bin"):
        assert not hasattr(llm, name), name
    for name in ("CLAUDE_CLI_BIN", "CLAUDE_MODEL", "CLAUDE_CLI_TIMEOUT",
                 "OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_TIMEOUT"):
        assert not hasattr(config, name), name


def test_the_configured_orders_name_only_registered_providers():
    """A name that is not registered is silently skipped by `_ordered`, so a
    stale LLM_PROVIDER_ORDER would leave the chain empty without a word."""
    assert set(config.LLM_PROVIDER_ORDER) <= set(llm._PROVIDERS)
    assert set(config.SAFETY_LLM_PROVIDER_ORDER) <= set(llm._PROVIDERS)
    assert llm._ordered(["claude_cli", "ollama", "openai"]) == [llm._PROVIDERS["openai"]]


@pytest.mark.asyncio
async def test_health_reports_only_the_openai_provider(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    status = await llm.health()
    assert set(status["providers"]) == {"openai"}
    assert status["available"] is False and status["active"] is None
