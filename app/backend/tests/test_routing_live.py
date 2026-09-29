"""Opt-in smoke test for the real OpenAI and OneMap routing integrations.

This never runs in CI or normal ``pytest`` use. It uses only a synthetic
non-clinical QA string and requires an operator to explicitly set
``RUN_LIVE_ROUTING_TESTS=1``.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from app import config, llm
from app.agents import CareRoutingAgent, CaseState
from app.services.onemap import ONEMAP_EMAIL, ONEMAP_PASSWORD

pytestmark = [pytest.mark.routing, pytest.mark.integration]


def _live_enabled() -> bool:
    return os.environ.get("RUN_LIVE_ROUTING_TESTS") == "1"


def test_live_openai_and_onemap_select_a_verified_clinic(monkeypatch):
    """Calls OpenAI and OneMap with synthetic QA data; opt-in because it costs money."""
    if not _live_enabled():
        pytest.skip("set RUN_LIVE_ROUTING_TESTS=1 to call live OpenAI and OneMap")
    if not config.OPENAI_API_KEY:
        pytest.skip("OPENAI_API_KEY is not configured")
    if not ONEMAP_EMAIL or not ONEMAP_PASSWORD:
        pytest.skip("ONEMAP_EMAIL and ONEMAP_PASSWORD are not configured")

    provider = llm.OpenAIProvider()
    provider_errors: list[str] = []
    model_summaries: list[dict] = []
    llm_inputs: list[object] = []
    raw_llm_responses: list[str] = []

    async def openai_only(system: str, prompt: str, json_mode: bool = False, json_schema: dict | None = None) -> str:
        try:
            try:
                llm_inputs.append(json.loads(prompt))
            except (TypeError, ValueError):
                llm_inputs.append({"parse": "router prompt was not JSON"})
            raw = await provider.complete(system, prompt, json_mode, json_schema)
            raw_llm_responses.append(raw)
            try:
                decision = json.loads(raw)
                model_summaries.append({
                    "selected_clinic_id": decision.get("selected_clinic_id"),
                    "confidence": decision.get("confidence"),
                    "rationale": decision.get("rationale"),
                    "tradeoffs": decision.get("tradeoffs"),
                    "rationale_length": len(str(decision.get("rationale", ""))),
                    "tradeoff_count": len(decision.get("tradeoffs", [])) if isinstance(decision.get("tradeoffs"), list) else None,
                })
            except (TypeError, ValueError):
                model_summaries.append({"parse": "provider returned non-JSON"})
            return raw
        except llm.LLMUnavailableError as exc:
            # Safe to surface in an opt-in synthetic smoke test; it includes
            # provider status only, never the API key or patient text.
            provider_errors.append(str(exc))
            raise

    # Root conftest disables all LLM calls for normal tests. Restore only the
    # OpenAI provider inside this explicitly requested live smoke test.
    monkeypatch.setattr(llm, "complete", openai_only)
    state = CaseState(
        raw_text="Synthetic QA routing check: mild rash.",
        acuity_code="P4_NON_URGENT",
        latitude=1.3000,
        longitude=103.8000,
        transport_mode="walk",
        max_travel_time_min=45,
    )

    result = asyncio.run(CareRoutingAgent().run(state))

    # This is deliberately an opt-in synthetic QA trace. It makes the model's
    # bounded selection and the final validated result visible without logging
    # a real patient's free-text symptoms or any credentials.
    print("\n[LIVE CARE ROUTING TRACE]")
    print(json.dumps({
        "synthetic_input": {
            "case_text": state.raw_text,
            "acuity_code": state.acuity_code,
            "location": {"latitude": state.latitude, "longitude": state.longitude},
            "transport_mode": state.transport_mode,
            "max_travel_time_min": state.max_travel_time_min,
            "symptom_text_sent_to_llm": False,
        },
        "exact_llm_input": llm_inputs[-1] if llm_inputs else None,
        "raw_llm_response": raw_llm_responses[-1] if raw_llm_responses else None,
        "llm_decision": model_summaries[-1] if model_summaries else None,
        "validated_route": {
            key: result.get(key)
            for key in (
                "source", "care_tier", "clinic", "selected_clinic_id", "confidence",
                "rationale", "rationale_truncated", "tradeoffs", "travel_time_min",
                "travel_estimate_source", "route_available", "route_instructions",
            )
        },
    }, indent=2))

    assert result["source"] == "llm", (
        "OpenAI did not yield an accepted routing decision. "
        "Provider diagnostic: "
        f"{provider_errors[-1] if provider_errors else model_summaries[-1] if model_summaries else 'invalid structured response'}"
    )
    assert result["care_tier"] == "GP"
    assert result["selected_clinic_id"].startswith("chas-")
    assert result["route_available"] is True
    assert result["travel_estimate_source"] == "onemap_route"
