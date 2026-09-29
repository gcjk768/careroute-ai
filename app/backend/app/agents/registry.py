"""[Agentic] Agent Registry — AAS Day 3 AM, slide 18 ("Agent Discovery").

OWNER: platform (James).

WHY THIS EXISTS
---------------
Slide 18 draws an **Agent Registry** and two ways an agent can be reached:

    Agent as tool    -> tool registry
    Agent as service -> service discovery

CareRoute had neither, and — worse — the canonical list of which agents exist
lived in `tests/agents/harness.py`, with a comment asking the reader to keep it
"in lock-step" with the app by hand. A list that only the test suite knows is
not a registry; it is a duplicate, and the promise to keep it in step is exactly
the kind that quietly stops being true. `AGENT_CLASSES` now lives here and the
harness imports it, so the two cannot diverge because there is only one.

WHAT IT ADDS THAT DID NOT EXIST
-------------------------------
Every worker already declares an `AgentCapability` — reasoning, action space,
memory, tools, classification, justification. That is a rich, machine-checked
self-description which, until now, could only be read by importing the class in
a test. `describe()` turns it into a discovery record and `/api/agents` serves
it, for the same reason the routing table is on `/api/health`: "we have seven
agents and here is what each may decide" should be inspectable at runtime rather
than taken from a diagram.

WHAT THIS DELIBERATELY DOES NOT DO
-----------------------------------
Both of slide 18's *exposure* modes are declined, in writing, because CareRoute's
agents are in-process pipeline stages and pretending otherwise would be the kind
of overstatement `Agent Capability Audit.md` exists to prevent:

* **Agent as tool.** No agent may call another agent. The sequence is fixed and
  owned by the orchestrator, and the contract tests enforce per-agent write
  lanes over shared state — turning a worker into a callable tool would let one
  agent re-enter another mid-case and make those lanes unenforceable. The
  agent-to-agent channel that DOES exist is the message bus (`COMMS`), which is
  announcement-only and deliberately cannot return a value.
* **Agent as service / service discovery.** Since 2026-09-19 there CAN be many
  processes: with `AGENT_TRANSPORT=http` each agent runs in its own container
  (`app/microservices/`, `docs/ARCHITECTURE.md` §5a). Addressing them is still
  not this module's job, and deliberately so — the address table is static
  configuration (`config.AGENT_URLS`, one env var per agent, resolved by Compose
  or ECS service discovery) and the dispatch client is `microservices/remote.py`.
  A dynamic registry that agents register themselves INTO would add a
  consistency problem to a set of services that is fixed at deploy time.

So this registry is for **discovery and introspection**, not for dispatch. That
distinction is the honest description of what it is, and it is why `describe()`
returns no endpoint or transport field: the address is deployment configuration,
not something an agent gets to announce about itself.

A2A AGENT CARDS (2026-09-24)
----------------------------
[Agentic][A2A] Lecture gaps "A2A concepts borrowed; protocol not implemented" and
"static registry, no runtime discovery". `agent_card()` publishes each agent as
an A2A-style Agent Card: name, description, version, skills (one per intent the
agent publishes, with input/output JSON schema), endpoint + transport, auth
requirement, and its tools (gateway-executed? reachable over MCP?). Every field
is GENERATED from declarations that already exist and are already enforced
(`CAPABILITY`, `CONTRACT`, `COMMS`, the microservice `OPS`, `config`), so there
is no second, hand-maintained description to drift. Cards are served on
`GET /api/agents/cards` and, by each agent container, on
`GET /.well-known/agent.json` (the A2A discovery path).

The card's address still comes from deployment configuration
(`config.AGENT_URLS`) — the platform writes it into the card; the agent does not
announce itself. Runtime discovery is the other direction: `fetch_card()` reads
a running container's card over HTTP, `load_card()` validates any card against
`AGENT_CARD_SCHEMA` (with the same validator the tool gateway uses) and
`resolve_skill()` finds which agent provides a skill. Dispatch itself stays
with `microservices/remote.py`; this is discovery, not a service mesh.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import config
from ..tools import registry as tool_registry
from .base import AGENT_LABELS
from .capability import AgentCapability
from .classifier import SeverityClassifierAgent
from .handoff import ClinicianHandoffAgent
from .hitl import HumanInTheLoopAgent
from .intake import SymptomIntakeAgent
from .reflection import ReflectionAgent
from .routing import CareRoutingAgent
from .safety import SafetyOverrideAgent

#: The canonical worker set, keyed by the slug that is ALSO the pytest marker and
#: the SSE event's `agent` field. One key across the app, the tests and the wire.
AGENT_CLASSES: dict[str, type] = {
    "intake": SymptomIntakeAgent,
    "classifier": SeverityClassifierAgent,
    "safety": SafetyOverrideAgent,
    "routing": CareRoutingAgent,
    "hitl": HumanInTheLoopAgent,
    "reflection": ReflectionAgent,
    "handoff": ClinicianHandoffAgent,
}

#: Who to ask when a worker misbehaves. Not on `AgentCapability` on purpose:
#: that dataclass is each member's own declaration about their agent's
#: BEHAVIOUR, and ownership is a team fact that changes without the code
#: changing. Mirrors the owners recorded in `pytest.ini`'s markers.
AGENT_OWNERS: dict[str, str] = {
    "intake": "Sham Goh",
    "classifier": "Koh Guan Chin James",
    "safety": "Aaron Liew",
    "routing": "Marcus Teh",
    "hitl": "Heriz Yusoff",
    "handoff": "Heriz Yusoff",
    "reflection": "platform / James",
}

#: The pipeline order the orchestrator actually runs. Recorded here so discovery
#: answers "when does this run?" and not only "what is it?" — a registry that
#: cannot say where a worker sits in the sequence is a glossary.
PIPELINE_ORDER: tuple[str, ...] = (
    "intake", "classifier", "safety", "routing", "hitl", "reflection", "handoff",
)


def capability_for(slug: str) -> AgentCapability:
    """The declared capability of one worker, without running it."""
    return AGENT_CLASSES[slug]().CAPABILITY


def describe(slug: str) -> dict[str, Any]:
    """One discovery record. The shape `/api/agents` serves.

    Reports `usesTrainedModel` because "which agent uses the trained model?" is
    the question the proposal review asked and the answer should not require
    grepping the source. Reports `upgradePath` because a policy node that has
    not yet earned the word "agent" should say so at runtime too, not only in a
    document a marker may never open.
    """
    capability = capability_for(slug)
    return {
        "slug": slug,
        "label": AGENT_LABELS.get(slug, slug),
        "owner": AGENT_OWNERS.get(slug, "unassigned"),
        "classification": capability.classification,
        "pipelinePosition": PIPELINE_ORDER.index(slug) + 1 if slug in PIPELINE_ORDER else None,
        "reasoning": capability.reasoning,
        # A tuple of ONE is the hallmark of a policy node — autonomy made
        # countable. Served as a list so the count is visible to a client.
        "actionSpace": list(capability.action_space),
        "memory": capability.memory,
        "tools": list(capability.tools),
        "usesTrainedModel": capability.uses_trained_model,
        "justification": capability.justification,
        "upgradePath": capability.upgrade_path,
    }


def discover() -> list[dict[str, Any]]:
    """Every worker, in the order the pipeline runs them."""
    return [describe(slug) for slug in PIPELINE_ORDER]


def summary() -> dict[str, Any]:
    """Counts worth being able to check without reading seven records."""
    records = discover()
    return {
        "agents": len(records),
        "byClassification": {
            value: sum(1 for r in records if r["classification"] == value)
            for value in sorted({r["classification"] for r in records})
        },
        "usesTrainedModel": [r["slug"] for r in records if r["usesTrainedModel"]],
        "withUpgradePath": [r["slug"] for r in records if r["upgradePath"].strip()],
    }


# --------------------------------------------------------------------------
# [Agentic][A2A] Agent Cards — see "A2A AGENT CARDS" in the module docstring.
# --------------------------------------------------------------------------
CARD_SCHEMA_VERSION = "careroute-agent-card/1"
WELL_KNOWN_PATH = "/.well-known/agent.json"
INVOKE_PATH = "/v1/invoke"

_JSON_SCHEMA_OBJECT = {"type": "object", "required": ["type"], "properties": {"type": {"type": "string", "enum": ["object"]}}}
_SKILL_SCHEMA = {
    "type": "object",
    "required": ["id", "name", "description", "inputSchema", "outputSchema"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "description": {"type": "string", "minLength": 1},
        "tags": {"type": "array", "items": {"type": "string"}},
        "inputSchema": _JSON_SCHEMA_OBJECT,
        "outputSchema": _JSON_SCHEMA_OBJECT,
    },
}
#: The card contract, checked by `load_card` with the tool gateway's validator.
AGENT_CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["schemaVersion", "name", "slug", "description", "version", "url", "transport",
                 "authentication", "skills", "tools"],
    "properties": {
        "schemaVersion": {"type": "string", "enum": [CARD_SCHEMA_VERSION]},
        "name": {"type": "string", "minLength": 1},
        "slug": {"type": "string", "minLength": 1},
        "description": {"type": "string", "minLength": 1},
        "version": {"type": "string", "minLength": 1},
        "url": {"type": "string"},
        "transport": {"type": "string", "enum": ["inprocess", "http+json"]},
        "authentication": {
            "type": "object", "required": ["schemes", "required"],
            "properties": {"schemes": {"type": "array", "items": {"type": "string"}},
                           "required": {"type": "boolean"}},
        },
        "skills": {"type": "array", "minItems": 1, "items": _SKILL_SCHEMA},
        "tools": {"type": "array", "items": {
            "type": "object", "required": ["name", "gateway"],
            "properties": {"name": {"type": "string"}, "gateway": {"type": "boolean"}},
        }},
    },
}


class CardError(ValueError):
    """An agent card that does not satisfy AGENT_CARD_SCHEMA."""


def _app_version() -> str:
    """Release version: CAREROUTE_VERSION (set in images), else the repo's VERSION file."""
    version = os.getenv("CAREROUTE_VERSION", "").strip()
    if version:
        return version
    path = Path(__file__).resolve().parents[3] / "VERSION"
    return path.read_text(encoding="utf-8").strip() if path.exists() else "0.0.0-dev"


def agent_card(slug: str) -> dict[str, Any]:
    """One agent's A2A-style card, generated from its declarations."""
    from ..microservices.workers import OPS  # lazy: workers imports the agent classes

    cls = AGENT_CLASSES[slug]
    capability: AgentCapability = cls.CAPABILITY
    ops = sorted(OPS.get(slug, ()))
    served = slug in config.AGENT_URLS
    input_schema: dict[str, Any] = {
        "type": "object",
        "required": ["op", "state"],
        "properties": {
            "op": {"type": "string", "enum": ops or ["run"]},
            "case_id": {"type": "string"},
            "state": {"type": "object", "description": "CaseState, wire format (microservices/wire.py)"},
            "inbox": {"type": "array", "items": {"type": "object"},
                      "description": f"A2A messages it subscribes to: {sorted(cls.COMMS.subscribes)}"},
        },
    }
    mcp_names = {spec.name: name for name, spec in tool_registry.mcp_tools().items()}

    def _output(intent: str) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "result": {"type": "object", "required": sorted(cls.CONTRACT.returns),
                           "properties": {key: {} for key in sorted(cls.CONTRACT.returns)}},
                # The agent's write lane IS the patch schema: a key outside it is
                # rejected by wire.apply_patch at the gateway.
                "state_patch": {"type": "object", "additionalProperties": False,
                                "properties": {key: {} for key in sorted(cls.CONTRACT.writes)}},
                "messages": {"type": "array", "items": {
                    "type": "object", "properties": {"intent": {"type": "string", "enum": [intent]}}}},
            },
        }

    return {
        "schemaVersion": CARD_SCHEMA_VERSION,
        "name": AGENT_LABELS.get(slug, slug),
        "slug": slug,
        "description": capability.reasoning or capability.justification,
        "version": _app_version(),
        "provider": {"organization": "CareRoute AI (NUS-ISS Team 3)", "owner": AGENT_OWNERS.get(slug, "unassigned")},
        "url": config.AGENT_URLS.get(slug, ""),
        "transport": "http+json" if served and config.AGENT_TRANSPORT == "http" else "inprocess",
        "endpoints": {"invoke": INVOKE_PATH, "card": WELL_KNOWN_PATH} if served else {},
        "authentication": {"schemes": ["internalToken"] if served else [], "header": "X-Internal-Token",
                           "required": bool(served and config.INTERNAL_TOKEN)},
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "classification": capability.classification,
        "skills": [
            {"id": intent, "name": intent,
             "description": f"Produces '{intent}'. Chooses between: {'; '.join(capability.action_space)}.",
             "tags": [capability.classification, slug],
             "inputSchema": input_schema, "outputSchema": _output(intent)}
            for intent in sorted(cls.COMMS.publishes)
        ],
        "tools": [{"name": tool, "gateway": tool in tool_registry.REGISTRY, "mcpName": mcp_names.get(tool)}
                  for tool in capability.tools],
    }


def agent_cards() -> list[dict[str, Any]]:
    """Every agent's card, in pipeline order."""
    return [agent_card(slug) for slug in PIPELINE_ORDER]


def load_card(card: Any) -> dict[str, Any]:
    """Validate a card (local or fetched) and return it. Raises CardError."""
    try:
        tool_registry.validate_arguments(AGENT_CARD_SCHEMA, card)
    except tool_registry.ToolArgumentError as exc:
        raise CardError(f"invalid agent card: {exc}") from None
    ids = [skill["id"] for skill in card["skills"]]
    if len(ids) != len(set(ids)):
        raise CardError(f"invalid agent card: duplicate skill ids {ids}")
    return card


def resolve_skill(skill_id: str, cards: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The card of the agent that provides `skill_id`, or None. Defaults to the
    local cards; pass fetched ones for runtime discovery."""
    for card in agent_cards() if cards is None else cards:
        if any(skill["id"] == skill_id for skill in card["skills"]):
            return card
    return None


def fetch_card(base_url: str, *, client: Any = None) -> dict[str, Any]:
    """[A2A] Runtime discovery: GET a running agent's card from its well-known
    path and validate it before anything trusts it. `client` is anything with
    an httpx-style `.get()` (a TestClient in tests)."""
    if client is None:
        import httpx

        with httpx.Client(timeout=5.0) as own:
            return fetch_card(base_url, client=own)
    reply = client.get(f"{base_url.rstrip('/')}{WELL_KNOWN_PATH}")
    reply.raise_for_status()
    return load_card(reply.json())
