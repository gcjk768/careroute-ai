"""[Microservices] What each agent container runs, and how a worker is built.

Stateless per request: every `/v1/invoke` gets a FRESH worker, so two cases in
flight can never see each other's inbox or Safety result (the bug
PipelineOrchestrator.new_session exists to prevent in-process). The expensive,
case-independent parts — the clinic dataset, the hours snapshot, the OneMap
client, the route cache, Safety's semantic layer — are built ONCE per
container and handed to each worker, exactly as new_session does.
"""
from __future__ import annotations

from collections.abc import Callable

from ..agents.classifier import SeverityClassifierAgent
from ..agents.handoff import ClinicianHandoffAgent
from ..agents.hitl import HumanInTheLoopAgent
from ..agents.reflection import ReflectionAgent
from ..agents.routing import CareRoutingAgent
from ..agents.safety import SafetyOverrideAgent

AGENT_CLASSES: dict[str, type] = {
    "classifier": SeverityClassifierAgent,
    "safety": SafetyOverrideAgent,
    "routing": CareRoutingAgent,
    "reflection": ReflectionAgent,
    "hitl": HumanInTheLoopAgent,
    "handoff": ClinicianHandoffAgent,
}

#: The worker methods the orchestrator calls on each agent. `emit` is on every
#: agent because an announcement is built from the CURRENT state (Reflection's
#: reads `state.reflection`, which the orchestrator sets after the loop).
OPS: dict[str, frozenset[str]] = {
    "classifier": frozenset({"run", "emit"}),
    "safety": frozenset({"prescreen", "shadow_nlp", "areason", "run", "emit"}),
    "routing": frozenset({"run", "emit"}),
    "reflection": frozenset({"areason", "run", "emit"}),
    "hitl": frozenset({"run", "emit"}),
    "handoff": frozenset({"run", "emit"}),
}

#: Instance attributes that a worker's emit() reads and its run() wrote.
#: Everything else emit() needs is on CaseState. Safety alone builds its
#: `safety.override` message from its own last result, so that result makes a
#: round trip through the gateway as `carry` instead of living in a process.
CARRY: dict[str, tuple[str, ...]] = {"safety": ("_last_result", "_last_protocol_issues")}


def worker_factory(slug: str) -> Callable[[], object]:
    if slug == "safety":
        template = SafetyOverrideAgent()
        return lambda: SafetyOverrideAgent(semantic=template.semantic)
    if slug == "routing":
        template = CareRoutingAgent()
        return lambda: CareRoutingAgent(
            lookup=template.lookup, hours=template.hours, maps=template.maps,
            use_onemap=False, route_cache=template._route_cache,
        )
    if slug in AGENT_CLASSES:
        return AGENT_CLASSES[slug]
    raise KeyError(f"unknown agent {slug!r}")
