"""[Agentic] LangGraph adapter over the EXISTING agents — a demonstration surface.

The production path is the hand-rolled supervisor (orchestration.py): it also
runs the A2A bus, the safety fail-safe protocol, the bounded Reflection loop,
the LLM critic and ordered SSE streaming, and ~60 tests pin that behaviour. A
rewrite would change nothing a patient sees, so this module does not replace it
(docs/design/specs/2026-09-26-llm-observability-and-langgraph-idea.md).

What it does: express the SAME pipeline as a LangGraph `StateGraph` —

  nodes  = the agents (each calls the agent's own `run()`), plus `plan`
  state  = the one CaseState object every agent already shares
  edges  = intake -> classifier -> safety -> plan, then planner.ALLOWED_TRANSITIONS,
           with the conditional router following planner.make_plan's step list
           and stopping after HITL when HITL asked the patient a question

so the graph can be drawn for the report (`scripts/show_graph.py`) and run on a
case. tests/agents/test_graph.py fails if the graph drifts from the planner.

Not wired into main.py. Skipped relative to the supervisor: announcement
verification, the safety fail-safe protocol, audit/metrics, the Reflection re-run loop and the LLM critic's
`areason` layer — the deterministic `run()` of every agent is what executes.
"""
from __future__ import annotations

import inspect
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import planner
from .base import CaseState
from .messaging import MessageBus
from .routing import reroute

PREFIX = ("intake", "classifier", "safety")


class GraphState(TypedDict, total=False):
    case: CaseState
    bus: MessageBus     # the A2A bus: HITL reads the classifier's question proposal from it
    intake_result: dict  # intake's run() result; HITL's unreadable-input floor reads it
    steps: list[str]    # planner.make_plan(...).names, set by the `plan` node
    visited: list[str]


async def _maybe(value):
    return await value if inspect.isawaitable(value) else value


async def _run(agent, state: CaseState, bus: MessageBus) -> dict:
    """Deliver the agent's inbox, run it, publish what it asserts — the
    supervisor's deliver_inbox / _run_worker / _announce, minus audit and SSE."""
    consume = getattr(agent, "consume", None)
    if callable(consume):
        consume(bus.inbox(agent))
    result = await _maybe(agent.run(state))
    emit = getattr(agent, "emit", None)
    if callable(emit):
        message = await _maybe(emit(state))
        if message is not None:
            bus.publish(message)
    return result or {}


def _agent_node(name: str, agent, session):
    async def node(gs: GraphState) -> GraphState:
        state, update = gs["case"], {}
        if name == planner.HITL:
            # The supervisor's two pre-HITL floors (orchestration._step_hitl), called
            # rather than copied. Without the unreadable-input floor the graph asked a
            # Malay complaint an English question the supervisor never asks
            # (scripts/graph_soak.py, 2026-09-26).
            session._apply_cross_visit_rule(state)
            session._apply_unreadable_input_floor(state, gs.get("intake_result", {}))
        if name != "handoff" or state.escalated:   # handoff acts only on escalated cases
            result = await _run(agent, state, gs["bus"])
            if name == "intake":
                update["intake_result"] = result
        return {**update, "visited": [*gs.get("visited", []), name]}
    node.__name__ = name
    return node


async def _emergency_guidance(gs: GraphState) -> GraphState:
    """P1: 995 guidance without the clinic search (orchestration._step_emergency_guidance)."""
    state = gs["case"]
    reroute(state)
    state.clinic = "Call 995 for SCDF emergency dispatch"
    return {"visited": [*gs.get("visited", []), planner.EMERGENCY]}


def _plan(gs: GraphState) -> GraphState:
    return {"steps": list(planner.make_plan(gs["case"]).names),
            "visited": [*gs.get("visited", []), "plan"]}


def _next(current: str):
    """Router: the step after `current` in the case's validated plan."""
    def route(gs: GraphState) -> str:
        if current == planner.HITL and gs["case"].clarification_asked:
            return END   # the interview: an ask ends the turn
        steps = gs["steps"]
        i = -1 if current == "plan" else steps.index(current)
        return steps[i + 1] if i + 1 < len(steps) else END
    return route


def edges() -> dict[str, set[str]]:
    """Every edge the graph may take, derived from the planner's transition table."""
    table = {START: {PREFIX[0]}, PREFIX[0]: {PREFIX[1]}, PREFIX[1]: {PREFIX[2]}, PREFIX[2]: {"plan"},
             "plan": set(planner.ALLOWED_TRANSITIONS[planner.START])}
    for step, nxt in planner.ALLOWED_TRANSITIONS.items():
        if step != planner.START:
            table[step] = {END if n == planner.END else n for n in nxt}
    table[planner.HITL] = table[planner.HITL] | {END}
    return table


def build(session):
    """Compile the graph over `session`'s agents (orchestrator.new_session())."""
    agents = {"intake": session.intake, "classifier": session.classifier, "safety": session.safety,
              planner.ROUTING: session.routing, planner.HITL: session.hitl,
              planner.REFLECTION: session.reflection, planner.HANDOFF: session.handoff}
    g = StateGraph(GraphState)
    for name, agent in agents.items():
        g.add_node(name, _agent_node(name, agent, session))
    g.add_node(planner.EMERGENCY, _emergency_guidance)
    g.add_node("plan", _plan)
    for src, dsts in edges().items():
        if src in (START, *PREFIX):
            g.add_edge(src, next(iter(dsts)))
        else:
            g.add_conditional_edges(src, _next(src), {d: d for d in dsts})
    return g.compile()


async def run_case(session, state: CaseState, config: dict | None = None) -> GraphState:
    """Run one case through the graph with a fresh A2A bus. `config` is LangGraph's
    run config, e.g. {"callbacks": [langfuse.langchain.CallbackHandler()]} to trace it."""
    return await build(session).ainvoke({"case": state, "bus": MessageBus()}, config=config)
