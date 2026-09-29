"""CareRoute agents package.

Split from the former single `agents.py` module so each of the five worker
agents lives in ITS OWN file (one owner per agent), while this package's
`__init__` re-exports the same public names the rest of the app and the test
suite import — so `from app.agents import CaseState, Supervisor, ...` keeps
working unchanged.

Ownership (see README "Agent ownership & isolated development"):
    intake.py        Symptom-Intake + ORCHESTRATOR — Sham Goh
    orchestration.py Pipeline orchestration engine — Sham Goh
    classifier.py    Severity-Classifier  — Koh Guan Chin James  (+ app/ml, app/rag*)
    safety.py        Safety-Override      — Aaron Liew           (+ app/redflags.py)
    routing.py       Care-Routing         — Marcus Teh
    hitl.py          Human-in-the-Loop    — Heriz Yusoff
    reflection.py    Reflection / Critic  — platform (James)
    base.py          Shared contract      — platform (James)
    supervisor.py    DEPRECATED alias     — see its docstring

Symptom-Intake holds BOTH roles: it is the first worker AND the orchestrator
that sequences the other five. `Supervisor` is re-exported below as an alias for
`SymptomIntakeAgent` so existing imports keep working; new code should use
`SymptomIntakeAgent` directly.
"""
from __future__ import annotations

from .base import (
    AGENT_LABELS,
    CONFIDENCE_THRESHOLD,
    AgentContract,
    CaseState,
    ToolAccessError,
    acuity_from_code,
    enforce_tool_access,
)
from .capability import (
    AGENT,
    ORCHESTRATOR,
    POLICY_NODE,
    AgentCapability,
    CapabilityError,
    classify,
    enforce_capability,
)
from .classifier import SeverityClassifierAgent
from .handoff import ClinicianHandoffAgent
from .hitl import HumanInTheLoopAgent
from .intake import SymptomIntakeAgent
from .messaging import (
    AgentComms,
    AgentMessage,
    CommsAccessError,
    ConsumesMessages,
    MessageBus,
    enforce_comms,
    require_subscription,
)
from .orchestration import PipelineOrchestrator
from .reasoning import ReasoningLayer, ReasoningOutcome
from .reflection import (
    REFLECTION_BUDGET_MS,
    REFLECTION_MAX_ITERS,
    ReflectionAgent,
)
from .routing import CareRoutingAgent, onemap_status
from .safety import SafetyOverrideAgent, SemanticRedFlagLayer
from .supervisor import Supervisor  # deprecated alias for SymptomIntakeAgent

__all__ = [
    "AGENT",
    "AGENT_LABELS",
    "CONFIDENCE_THRESHOLD",
    "ORCHESTRATOR",
    "POLICY_NODE",
    "REFLECTION_BUDGET_MS",
    "REFLECTION_MAX_ITERS",
    "AgentCapability",
    "AgentComms",
    "AgentContract",
    "AgentMessage",
    "CapabilityError",
    "CareRoutingAgent",
    "CaseState",
    "ClinicianHandoffAgent",
    "CommsAccessError",
    "ConsumesMessages",
    "HumanInTheLoopAgent",
    "MessageBus",
    "PipelineOrchestrator",
    "ReasoningLayer",
    "ReasoningOutcome",
    "ReflectionAgent",
    "SafetyOverrideAgent",
    "SemanticRedFlagLayer",
    "SeverityClassifierAgent",
    "Supervisor",
    "SymptomIntakeAgent",
    "ToolAccessError",
    "acuity_from_code",
    "classify",
    "enforce_capability",
    "enforce_comms",
    "enforce_tool_access",
    "onemap_status",
    "require_subscription",
]
