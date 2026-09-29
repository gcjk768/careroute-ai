"""The LangGraph adapter (app/agents/graph.py) must not drift from the planner
or from the supervisor it mirrors."""
from __future__ import annotations

import asyncio

import pytest

from app.agents import graph, planner
from app.agents.base import CaseState
from app.main import orchestrator


def test_compiled_edges_are_exactly_the_planner_transitions():
    compiled = graph.build(orchestrator.new_session()).get_graph()
    got: dict[str, set[str]] = {}
    for e in compiled.edges:
        got.setdefault(e.source, set()).add(e.target)
    want = {src: set(dsts) for src, dsts in graph.edges().items()}
    assert got == want


def _supervisor(text: str) -> tuple[list[str], CaseState]:
    session = orchestrator.new_session()
    state = CaseState(case_id="case_sup", raw_text=text)

    async def _go():
        async def _delay():
            return None
        async for _ in session.orchestrate(state, audit=lambda *a, **k: None, log=lambda *a, **k: None, delay=_delay):
            pass

    asyncio.run(_go())
    steps = list(session.plan["steps"][i]["step"] for i in range(len(session.plan["steps"])))
    if state.clarification_asked:
        steps = steps[:steps.index(planner.HITL) + 1]
    return steps, state


@pytest.mark.parametrize("text", [
    "crushing chest pain radiating to my left arm",          # P1 -> emergency shape
    "I am vomiting blood",                                   # red flag -> red_flag shape
    "sore throat and runny nose for two days, no fever",     # routine -> full
    "I feel a bit unwell and off today, nothing specific.",  # vague -> interview stop after hitl
    "saya sakit perut sejak semalam",                        # unreadable (LLM off) -> floor: P3, escalate, never ask
])
def test_the_graph_takes_the_supervisors_path(text):
    want_steps, sup = _supervisor(text)
    state = CaseState(case_id="case_graph", raw_text=text)
    out = asyncio.run(graph.run_case(orchestrator.new_session(), state))
    assert out["visited"] == [*graph.PREFIX, "plan", *want_steps]
    assert state.acuity_code == sup.acuity_code
    assert state.escalated == sup.escalated
