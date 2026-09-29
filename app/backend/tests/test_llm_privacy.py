"""[AI-Security] LLM02 — llm.py must never leak prompt/response CONTENT.

WHY THIS EXISTS
---------------
The module docstring of ``app/llm.py`` promises: "Prompt/response CONTENT is
never logged either — this is healthcare data, so only provider names and
outcomes are emitted to the structured log."

Three places broke that promise:
  * ``_finalise`` raised ``LLMUnavailableError(f"...: {text[:160]}")`` — 160
    characters of a model response about a patient, embedded in an exception
    that ``complete()`` then logs at WARNING and re-raises into the caller.
  * ``OpenAIProvider.complete`` put ``resp.text[:160]`` (the upstream body,
    which echoes the request on some errors) into its exception.
  * ``complete()`` logged the raw exception ``exc`` for BOTH the LLM and the
    generic branch — and a generic exception such as ``json.JSONDecodeError``
    carries a slice of the offending document in its own message.

A log aggregator is a different trust boundary from the LLM provider: PHI that
was masked on the way OUT must not reappear in the logs on the way BACK.
"""
from __future__ import annotations

import json
import logging

import pytest

from app import config, llm

SENTINEL = "PATIENT-NRIC-S1234567D-CHEST-PAIN"

# conftest's autouse `_disable_llm` replaces `llm.complete` with a stub for the
# whole suite; grab the genuine coroutine at import time (before any fixture
# runs) because these tests are specifically about the real one's behaviour.
_REAL_COMPLETE = llm.complete


class _EchoProvider:
    """A provider that returns non-JSON prose containing the sentinel."""

    name = "openai"  # a name in the default order, so `complete()` tries it

    def label(self) -> str:
        return "echo"

    async def available(self) -> bool:
        return True

    async def complete(self, system, prompt, json_mode, json_schema=None) -> str:
        # Real model chatter: prose wrapped around the patient's own words. Every
        # real provider hands its raw text to `_finalise`, which is the code
        # under test here, so this double does the same.
        return llm._finalise(
            f"Sure! Here is my assessment of {SENTINEL}. No JSON for you.", json_mode
        )


class _RaisingProvider:
    """A provider whose *generic* (non-LLMUnavailableError) failure carries content."""

    name = "openai"

    def label(self) -> str:
        return "raiser"

    async def available(self) -> bool:
        return True

    async def complete(self, system, prompt, json_mode, json_schema=None) -> str:
        # json.JSONDecodeError embeds a slice of the document in str(exc).
        json.loads(f'{{"note": "{SENTINEL}" ')


@pytest.fixture(autouse=True)
def _enable_llm(monkeypatch):
    """conftest patches llm.complete globally; these tests exercise the real one."""
    monkeypatch.setattr(llm, "complete", _REAL_COMPLETE)
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    monkeypatch.setattr(config, "DEBUG_LLM", False)


@pytest.mark.asyncio
async def test_unparseable_response_content_never_reaches_logs_or_exception(monkeypatch, caplog):
    monkeypatch.setattr(llm, "_PROVIDERS", {"openai": _EchoProvider()})
    monkeypatch.setattr(config, "LLM_PROVIDER_ORDER", ["openai"])

    caplog.set_level(logging.DEBUG, logger="careroute.llm")
    with pytest.raises(llm.LLMUnavailableError) as excinfo:
        await llm.complete("system", "prompt", json_mode=True)

    assert SENTINEL not in str(excinfo.value), str(excinfo.value)
    assert SENTINEL not in caplog.text, caplog.text
    # The operator still gets something actionable: provider + outcome + size.
    assert "openai" in caplog.text


@pytest.mark.asyncio
async def test_generic_provider_exception_content_never_reaches_logs(monkeypatch, caplog):
    monkeypatch.setattr(llm, "_PROVIDERS", {"openai": _RaisingProvider()})
    monkeypatch.setattr(config, "LLM_PROVIDER_ORDER", ["openai"])

    caplog.set_level(logging.DEBUG, logger="careroute.llm")
    with pytest.raises(llm.LLMUnavailableError) as excinfo:
        await llm.complete("system", "prompt", json_mode=True)

    assert SENTINEL not in str(excinfo.value), str(excinfo.value)
    assert SENTINEL not in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_openai_error_body_is_not_put_into_the_exception(monkeypatch):
    """A 4xx/5xx body from OpenAI often echoes the prompt back; never re-raise it."""
    class _Resp:
        status_code = 400
        text = f'{{"error": {{"message": "bad request for {SENTINEL}"}}}}'

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: _Client())

    with pytest.raises(llm.LLMUnavailableError) as excinfo:
        await llm.OpenAIProvider().complete("system", "prompt", json_mode=False)

    assert SENTINEL not in str(excinfo.value), str(excinfo.value)
    assert "400" in str(excinfo.value)


@pytest.mark.asyncio
async def test_debug_flag_opts_an_operator_back_into_the_excerpt(monkeypatch):
    """CAREROUTE_DEBUG_LLM=1 is a deliberate, documented local-debug escape hatch."""
    monkeypatch.setattr(llm, "_PROVIDERS", {"openai": _EchoProvider()})
    monkeypatch.setattr(config, "LLM_PROVIDER_ORDER", ["openai"])
    monkeypatch.setattr(config, "DEBUG_LLM", True)

    with pytest.raises(llm.LLMUnavailableError) as excinfo:
        await llm.complete("system", "prompt", json_mode=True)

    assert SENTINEL in str(excinfo.value)
