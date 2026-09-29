"""Care-Routing — the bounded ReAct tool loop.   Run with: pytest -m routing

Added 2026-09-15 (James, courseware audit) alongside Marcus's own suite in
test_routing.py, which is untouched and must keep passing: a model that never
asks for a tool takes exactly the old single-shot path.

What these pin, in the Agentic module's vocabulary:
  * agent loop      — reason → act (tool call) → observe → reason again
  * tool registry   — only registered tools, only registered arguments
  * function calling— tool requests arrive as structured JSON, not free text
  * loop safety     — a hard turn budget, and repeats are not re-executed
  * least privilege — a tool request still passes `enforce_tool_access`
"""
import asyncio
import json

import pytest

from app.agents import CareRoutingAgent, CaseState
from app.agents.routing import _MAX_TOOL_TURNS, ROUTING_TOOL_REGISTRY, routing_schema_for
from app.tools.clinic_lookup import Clinic
from app.tools.gpgowhere import HoursProfile

pytestmark = pytest.mark.routing

OPEN_ID = "chas-100001-open-clinic"
FAR_ID = "chas-100005-far-clinic"


class _Lookup:
    def find_candidate_clinics(self, *_args, **_kwargs):
        return [
            Clinic("Open Clinic", "1 Test Road", "100001", "60000001", 1.301, 103.801, ["CHAS"]),
            Clinic("Far Clinic", "5 Test Road", "100005", "60000005", 1.320, 103.820, ["CHAS"]),
        ]


class _Hours:
    def match(self, *, postal, name):
        return HoursProfile(postal=postal, name=name, hours={}, twenty_four_hours=True, verified_at="test")


def _state():
    return CaseState(raw_text="mild rash", acuity_code="P4_NON_URGENT", latitude=1.3, longitude=103.8)


def _agent():
    return CareRoutingAgent(lookup=_Lookup(), hours=_Hours(), use_onemap=False)


def _selection(clinic_id=OPEN_ID, **extra):
    body = {"selected_clinic_id": clinic_id, "confidence": 0.8, "rationale": "Open and near.", "tradeoffs": [],
            "tool_call": None}
    body.update(extra)
    return json.dumps(body)


def _scripted(responses):
    """An LLM stub that returns the scripted responses in order and records
    every prompt it was shown."""
    calls = []

    async def complete(_system, prompt, **kwargs):
        calls.append({"prompt": json.loads(prompt), "schema": kwargs.get("json_schema")})
        index = min(len(calls) - 1, len(responses) - 1)
        return responses[index]

    return complete, calls


def test_a_tool_call_is_executed_observed_and_then_the_model_decides(monkeypatch):
    """Turn 1: the model asks for a walking estimate of the far clinic.
    Turn 2: it sees the observation and commits. The trace is on the plan."""
    ask = _selection(FAR_ID, tool_call={"name": "travel.estimate", "arguments": {"clinic_id": FAR_ID, "transport": "walk"}})
    decide = _selection(OPEN_ID)
    complete, calls = _scripted([ask, decide])
    import app.llm as llm
    monkeypatch.setattr(llm, "complete", complete)

    state = _state()
    result = asyncio.run(_agent().run(state))

    assert len(calls) == 2, "one tool turn then one decision turn"
    assert "observations" not in calls[0]["prompt"], "turn 1 is the original prompt"
    observation = calls[1]["prompt"]["observations"][0]
    assert observation["tool"] == "travel.estimate"
    assert observation["clinic_id"] == FAR_ID and observation["transport"] == "walk"
    assert observation["travel_time_min"] >= 1

    assert result["source"] == "llm"
    assert result["selected_clinic_id"] == OPEN_ID
    assert state.routing_plan["tool_calls"][0]["tool"] == "travel.estimate"
    assert result["tool_trace"] == state.routing_plan["tool_calls"]


def test_hours_lookup_tool_returns_a_logistics_only_observation(monkeypatch):
    ask = _selection(OPEN_ID, tool_call={"name": "facility.hours.lookup", "arguments": {"clinic_id": OPEN_ID, "transport": None}})
    complete, calls = _scripted([ask, _selection(OPEN_ID)])
    import app.llm as llm
    monkeypatch.setattr(llm, "complete", complete)

    state = CaseState(raw_text="PRIVATE_RAW_MARKER", normalised_symptoms="PRIVATE_NORMALISED_MARKER",
                      acuity_code="P4_NON_URGENT", latitude=1.3, longitude=103.8)
    asyncio.run(_agent().run(state))

    observation = calls[1]["prompt"]["observations"][0]
    assert observation["tool"] == "facility.hours.lookup"
    assert observation["open_now"] is True
    assert "PRIVATE" not in repr(calls[1]["prompt"])


def test_the_loop_is_bounded_and_falls_back_when_the_model_never_decides(monkeypatch):
    """A model that only ever asks for tools exhausts the budget and the
    deterministic order wins — with the trace preserved for the audit."""
    asks = [
        _selection(OPEN_ID, tool_call={"name": "travel.estimate", "arguments": {"clinic_id": OPEN_ID, "transport": mode}})
        for mode in ("walk", "cycle", "drive", "taxi", "public")
    ]
    complete, calls = _scripted(asks)
    import app.llm as llm
    monkeypatch.setattr(llm, "complete", complete)

    state = _state()
    result = asyncio.run(_agent().run(state))

    assert len(calls) == _MAX_TOOL_TURNS + 1
    assert len(result["tool_trace"]) == _MAX_TOOL_TURNS
    # The final turn's tool request is refused (budget spent) and, because the
    # accompanying "decision" is only provisional, the safe fallback is used.
    assert result["source"] == "deterministic_fallback"
    assert result["clinic"] == "Open Clinic"


def test_an_unregistered_tool_or_unverified_clinic_becomes_an_error_observation(monkeypatch):
    bad_tool = _selection(OPEN_ID, tool_call={"name": "shell.exec", "arguments": {"clinic_id": OPEN_ID, "transport": None}})
    bad_clinic = _selection(OPEN_ID, tool_call={"name": "travel.estimate", "arguments": {"clinic_id": "chas-999-invented", "transport": "walk"}})
    complete, calls = _scripted([bad_tool, bad_clinic, _selection(OPEN_ID)])
    import app.llm as llm
    monkeypatch.setattr(llm, "complete", complete)

    result = asyncio.run(_agent().run(_state()))

    observations = calls[2]["prompt"]["observations"]
    assert "unregistered" in observations[0]["error"]
    assert "not a verified candidate" in observations[1]["error"]
    assert result["source"] == "llm" and result["selected_clinic_id"] == OPEN_ID


def test_a_repeated_identical_request_is_not_re_executed(monkeypatch):
    executed = []
    agent = _agent()
    original = agent._run_tool

    async def counting(state, request, candidates):
        executed.append(request)
        return await original(state, request, candidates)

    agent._run_tool = counting
    same = _selection(OPEN_ID, tool_call={"name": "travel.estimate", "arguments": {"clinic_id": OPEN_ID, "transport": "walk"}})
    complete, calls = _scripted([same, same, _selection(OPEN_ID)])
    import app.llm as llm
    monkeypatch.setattr(llm, "complete", complete)

    asyncio.run(agent.run(_state()))

    assert len(executed) == 1
    assert "already answered" in calls[2]["prompt"]["observations"][1]["error"]


def test_tool_requests_go_through_the_least_privilege_gate(monkeypatch):
    """Revoking the tool from the allow-list turns the request into an error
    observation — the case still completes."""
    ask = _selection(OPEN_ID, tool_call={"name": "travel.estimate", "arguments": {"clinic_id": OPEN_ID, "transport": "walk"}})
    complete, calls = _scripted([ask, _selection(OPEN_ID)])
    import app.llm as llm
    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(CareRoutingAgent, "TOOL_ALLOWLIST", ["clinic.lookup", "facility.hours.lookup", "llm.complete"])

    agent = _agent()
    # The pre-computed travel estimate in `_candidates` also needs the tool, so
    # candidates would be empty; test the loop helper directly instead.
    candidates = [{"clinic_id": OPEN_ID, "name": "Open Clinic", "address": "1 Test Road", "postal": "100001",
                   "phone": "60000001", "latitude": 1.301, "longitude": 103.801, "programmes": ["CHAS"]}]
    request = agent._tool_call_request(json.loads(ask), candidates)
    with pytest.raises(Exception, match="not in its least-privilege allow-list"):
        asyncio.run(agent._run_tool(_state(), request, candidates))
    assert len(calls) == 0


def test_schema_binds_tool_arguments_to_verified_candidates_and_supported_modes():
    candidates = [{"clinic_id": OPEN_ID}, {"clinic_id": FAR_ID}]
    schema = routing_schema_for(candidates)

    assert schema["properties"]["selected_clinic_id"]["enum"] == [OPEN_ID, FAR_ID]
    assert "tool_call" in schema["required"]
    tool = schema["properties"]["tool_call"]["anyOf"][1]
    assert tool["properties"]["name"]["enum"] == list(ROUTING_TOOL_REGISTRY)
    assert tool["properties"]["arguments"]["properties"]["clinic_id"]["enum"] == [OPEN_ID, FAR_ID]
    assert set(tool["properties"]["arguments"]["properties"]["transport"]["anyOf"][0]["enum"]) == {
        "walk", "cycle", "public", "drive", "taxi",
    }


def test_a_model_that_never_calls_a_tool_takes_the_single_shot_path(monkeypatch):
    """Marcus's contract: no tool call, no behaviour change, one LLM call."""
    complete, calls = _scripted([_selection(OPEN_ID)])
    import app.llm as llm
    monkeypatch.setattr(llm, "complete", complete)

    state = _state()
    result = asyncio.run(_agent().run(state))

    assert len(calls) == 1
    assert result["source"] == "llm" and result["tool_trace"] == []
    assert "tool_calls" not in state.routing_plan
