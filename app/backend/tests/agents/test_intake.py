"""Symptom-Intake agent — owner: Sham Goh.   Run with: pytest -m intake"""
import pytest

from app.agents import SymptomIntakeAgent
from harness import emit_and_check, make_case, run_and_check

pytestmark = pytest.mark.intake


def test_intake_emits_normalised_symptoms_message():
    msg = emit_and_check(SymptomIntakeAgent(), make_case())
    assert msg.intent == "symptoms.normalised"
    assert msg.recipient == "classifier"
    assert "normalised_symptoms" in msg.payload


def test_intake_falls_back_deterministically_when_llm_is_down():
    result, state = run_and_check(
        SymptomIntakeAgent(),
        make_case(raw_text="I have had a bad cough and a fever for two days."),
    )
    # The LLM is disabled globally (root conftest), so intake must fall back.
    assert result["source"] == "fallback"
    assert state.normalised_symptoms
    assert isinstance(state.intake_keywords, list)


def test_intake_detects_declared_language():
    _result, state = run_and_check(
        SymptomIntakeAgent(),
        make_case(raw_text="Tengo mucho dolor y fiebre", language="es"),
    )
    assert state.detected_language == "es"


def test_the_orchestration_pseudo_tools_are_enforced_not_just_declared(monkeypatch):
    """[AI-Security] FR-12. `route` and `aggregate` came with the orchestrator
    role and sat in the allow-list with no call site checking them — declared
    privilege that nothing enforced, which is the state the allow-list exists to
    prevent (base.py: "the allow-list is an enforced runtime control rather than
    documentation"). Sequencing the workers is the use of `route`; building the
    rationale and the citation set is the use of `aggregate`."""
    import asyncio

    from app.agents import CaseState, ToolAccessError

    agent = SymptomIntakeAgent()
    monkeypatch.setattr(agent, "TOOL_ALLOWLIST", ["llm.complete"])  # both revoked

    async def _first_step() -> None:
        steps = agent.orchestrate(
            CaseState(raw_text="mild sore throat"),
            audit=lambda **_kw: None, log=lambda *_a, **_kw: None, delay=_noop,
        )
        await steps.__anext__()

    with pytest.raises(ToolAccessError):
        asyncio.run(_first_step())
    with pytest.raises(ToolAccessError):
        agent.build_rationale(make_case())
    with pytest.raises(ToolAccessError):
        agent.build_citations(make_case())


async def _noop() -> None:
    """The orchestrator's inter-step UI delay, with no delay."""


from app.agents import intake as _intake


def test_cjk_terminal_punctuation_ends_the_sentence():
    assert _intake._clean_text("我头痛。", False) == "我头痛。"
    assert _intake._clean_text("我头痛！", False) in {"我头痛！", "我头痛!"}  # NFKC may fold the mark


def test_keyword_extraction_needs_a_right_word_boundary():
    assert "seizure" not in _intake._extract_keywords("in the fitting room and strained to reach")


def test_keyword_extraction_drops_a_denied_symptom():
    found = _intake._extract_keywords("no chest pain, just a cough")
    assert "chest pain" not in found
    assert "cough" in found
    assert "chest pain" in _intake._extract_keywords("no fever but severe chest pain")
