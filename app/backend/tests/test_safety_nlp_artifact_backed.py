"""Opt-in Safety NLP tests that load local model artifacts.

Normal test runs must not download or load heavy model artifacts. Run these only
after the local cache has been prepared:

    RUN_SAFETY_NLP_ARTIFACT_TESTS=1 python -m pytest -m safety_nlp_artifact
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from app import config, llm
import app.main as main_module
from app.models import TriageRequest
from app.safety_nlp import SafetyNlpRuntime
from app.safety_nlp.context import CareRouteContextRuleAdapter
from app.safety_nlp.contracts import AssertionResult, MentionSpan, SafetyNlpAdapters

pytestmark = [
    pytest.mark.safety,
    pytest.mark.eval,
    pytest.mark.safety_nlp_artifact,
    pytest.mark.skipif(
        os.environ.get("RUN_SAFETY_NLP_ARTIFACT_TESTS") != "1",
        reason="set RUN_SAFETY_NLP_ARTIFACT_TESTS=1 to load local Safety NLP artifacts",
    ),
]

BACKEND_ROOT = Path(__file__).parents[1]
MANIFEST = BACKEND_ROOT / "models" / "safety" / "manifest.json"


def test_selected_biolord_artifact_backed_smoke():
    runtime = SafetyNlpRuntime.from_manifest_file(
        MANIFEST,
        enabled=True,
        project_root=BACKEND_ROOT,
        cache_dir=str(BACKEND_ROOT / "models" / "safety" / "cache"),
    )
    runtime.warmup()

    mention = MentionSpan(mention_id="m1", text="crushing chest pain", start=0, end=19)
    ranked = runtime.category_adapter.rank("crushing chest pain", mention)

    assert runtime.loaded_model_keys == ["ner", "assertion", "biolord"]
    assert ranked
    assert ranked[0].status == "success"
    assert ranked[0].category == "cardiac_chest_pain"


def test_live_artifact_disagreement_invokes_bounded_llm_through_sse(monkeypatch):
    """Pinned BioLORD/mDeBERTa disagreement reaches the forcing-free LLM gate."""
    phrase = "a heavy feeling spreading from my chest toward my arm"

    class WholePhraseNer:
        model_name = "artifact-test-span-source"
        model_revision = "v1"

        def extract(self, text: str, language: str):
            start = text.index(phrase)
            return [MentionSpan(
                mention_id="artifact-m1", text=phrase, start=start, end=start + len(phrase),
                source_language=language, model_name=self.model_name,
                model_revision=self.model_revision,
            )]

    class PresentAssertion:
        def classify(self, text: str, mention: MentionSpan):
            return AssertionResult(
                mention_id=mention.mention_id, assertion="present", confidence=1.0,
                model_name="artifact-test-assertion", model_revision="v1",
            )

    runtime = SafetyNlpRuntime.from_manifest_file(
        MANIFEST,
        enabled=True,
        project_root=BACKEND_ROOT,
        cache_dir=str(BACKEND_ROOT / "models" / "safety" / "cache"),
        direct_nli_enabled=True,
        timeout_ms=10_000,
    )
    runtime.warmup()
    adapters = SafetyNlpAdapters(
        translation=runtime.adapters.translation,
        ner=WholePhraseNer(),
        assertion=PresentAssertion(),
        context=CareRouteContextRuleAdapter(),
        category=runtime.adapters.category,
        category_comparators=runtime.adapters.category_comparators,
    )

    async def bounded_llm(*_args, **_kwargs):
        return json.dumps({
            "triggered": True,
            "category": "cardiac_chest_pain",
            "assertion": "present",
            "temporality": "current",
            "subject": "patient",
            "confidence": 0.85,
            "evidenceSpan": phrase,
            "rationale": "Bounded adjudication of model disagreement.",
        })

    async def no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main_module.orchestrator.safety, "nlp_adapters", adapters)
    monkeypatch.setattr(main_module, "_step_delay", no_delay)
    monkeypatch.setattr(config, "SAFETY_NLP_ENABLED", True)
    monkeypatch.setattr(config, "SAFETY_NLP_TEST_FORCE_UNCERTAINTY", False)
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    monkeypatch.setenv("CAREROUTE_SAFETY_LLM", "1")
    monkeypatch.setattr(llm, "complete", bounded_llm)

    async def collect_events():
        events = []
        request = TriageRequest(text=f"Since this morning I have {phrase}.", language="en")
        async for chunk in main_module._triage_event_stream(request):
            events.append(json.loads(chunk.removeprefix("data:").strip()))
        return events

    events = asyncio.run(collect_events())
    safety = next(
        event for event in events
        if event.get("event") == "agent_result" and event.get("agent") == "safety"
    )

    assert safety["data"]["triggered"] is False
    assert safety["data"]["semantic"][0]["category"] == "cardiac_chest_pain"
    assert "testOnlyForcedUncertainty" not in repr(safety)
