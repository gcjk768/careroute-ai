"""[Agentic][AI-Security] Central TOOL REGISTRY + GATEWAY (platform-owned).

ArchAAS Day 3 (AM) separates two things an agentic system needs once models
start choosing tools:

  * a REGISTRY — one catalogue of every tool, with a description the model reads,
    a JSON schema for its arguments, and metadata (who may call it, whether it
    is read-only, who owns it);
  * a GATEWAY in front of it — the single choke point a MODEL-CHOSEN call passes
    through: validate the arguments against the schema, authorise the caller,
    execute, time it, count it, and hand back an OBSERVATION. A failure becomes
    an error observation the agent loop can read, never an exception that
    escapes into it.

WHAT GOES THROUGH THE GATEWAY
-----------------------------
Calls whose name and arguments a language model produced. That is where schema
validation earns its keep: a model can emit any JSON at all. Deterministic code
paths (the classifier fetching its own citations, say) keep calling their tool
directly under `enforce_tool_access`, because there is no untrusted argument to
validate.

Care-Routing's tools (clinic.lookup, facility.hours.lookup, travel.estimate) now
pass through this gateway too — both the deterministic shortlist calls and the
calls the model chooses in its ReAct loop. [Agentic] Until 2026-09-24 they were
only catalogued here and executed natively inside the agent, so none of the
policy below applied to them. They are CONTEXTUAL tools: they need the case
(the patient's location, the verified facility list, the agent's OneMap client
and per-case OneMap budget), which is handed to the gateway as a `context`
object and never placed in model-visible arguments. The gateway enforces a
per-case call quota over that context. Contextual tools are never published
over MCP — the patient's location must not leave the process.

TRANSPORT
---------
Default: in-process. With CAREROUTE_TOOL_TRANSPORT=mcp, a non-contextual tool
that is published over MCP is executed through the MCP CLIENT
(`tools/mcp_client.py`) against `python -m app.mcp_server`, AFTER the local
authorisation and schema checks — so an agent consumes the tool over the
protocol while the gateway's policy still applies on this side.

AUTHORISATION IS TWO-KEYED
--------------------------
A call succeeds only if BOTH the tool lists the caller in `allowed_agents` AND
the caller lists the tool in its own `TOOL_ALLOWLIST` (FR-12, checked with the
same `enforce_tool_access` every agent already uses). Either side can revoke.
"""
from __future__ import annotations

import logging
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("careroute.tools")


class ToolArgumentError(ValueError):
    """The arguments a caller supplied do not satisfy the tool's schema."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    allowed_agents: frozenset[str]
    handler: Callable[[dict[str, Any]], Any] | None
    executor: str = "gateway"
    read_only: bool = True
    owner: str = "platform (James)"
    #: [Agentic] Handler is `handler(args, context)`; the call must carry a
    #: per-case context. See the module docstring.
    contextual: bool = False
    #: [Agentic] Gateway-enforced ceiling on calls per case (needs a context).
    per_case_limit: int | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "allowedAgents": sorted(self.allowed_agents),
            "executor": self.executor,
            "readOnly": self.read_only,
            "owner": self.owner,
            "contextual": self.contextual,
            "perCaseLimit": self.per_case_limit,
            "mcpName": mcp_name(self) if self.name in mcp_tools_by_registry_name() else None,
        }


# --------------------------------------------------------------------------
# Argument validation — the JSON-schema subset tool schemas actually use.
# Written here rather than importing `jsonschema`, which is not a core
# dependency: the gateway must work on the minimal install CI runs.
# --------------------------------------------------------------------------
_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    # Nested shapes: used by the A2A agent-card schema (agents/registry.py),
    # which is validated with this same validator rather than a second one.
    "object": (dict,),
    "array": (list,),
}


def _check_value(key: str, rule: dict[str, Any], value: Any) -> None:
    expected = rule.get("type")
    if expected:
        allowed = _TYPES.get(expected)
        if allowed is None:
            raise ToolArgumentError(f"schema for {key!r} uses unsupported type {expected!r}")
        # bool is a subclass of int in Python; a model answering `true` for a
        # count must not pass as 1.
        if not isinstance(value, allowed) or (expected in ("integer", "number") and isinstance(value, bool)):
            raise ToolArgumentError(f"argument {key!r} must be {expected}")
    if "enum" in rule and value not in rule["enum"]:
        raise ToolArgumentError(f"argument {key!r} must be one of {rule['enum']}")
    if isinstance(value, str):
        if len(value) < int(rule.get("minLength", 0)):
            raise ToolArgumentError(f"argument {key!r} is shorter than {rule['minLength']}")
        if "maxLength" in rule and len(value) > int(rule["maxLength"]):
            raise ToolArgumentError(f"argument {key!r} is longer than {rule['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in rule and value < rule["minimum"]:
            raise ToolArgumentError(f"argument {key!r} is below {rule['minimum']}")
        if "maximum" in rule and value > rule["maximum"]:
            raise ToolArgumentError(f"argument {key!r} is above {rule['maximum']}")
    if isinstance(value, dict) and ("properties" in rule or "required" in rule):
        validate_arguments(rule, value)
    if isinstance(value, list):
        if len(value) < int(rule.get("minItems", 0)):
            raise ToolArgumentError(f"argument {key!r} needs at least {rule['minItems']} item(s)")
        if "items" in rule:
            for index, item in enumerate(value):
                _check_value(f"{key}[{index}]", rule["items"], item)


def validate_arguments(schema: dict[str, Any], arguments: Any) -> dict[str, Any]:
    """Validate `arguments` against an object schema and return a COPY with
    declared defaults filled in. Raises ToolArgumentError on any mismatch."""
    if not isinstance(arguments, dict):
        raise ToolArgumentError("arguments must be a JSON object")
    properties: dict[str, dict[str, Any]] = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ToolArgumentError(f"unknown argument(s): {unknown}")
    for key in schema.get("required", []):
        if key not in arguments:
            raise ToolArgumentError(f"missing required argument {key!r}")
    clean: dict[str, Any] = {}
    for key, rule in properties.items():
        if key in arguments:
            _check_value(key, rule, arguments[key])
            clean[key] = arguments[key]
        elif "default" in rule:
            clean[key] = rule["default"]
    return clean


# --------------------------------------------------------------------------
# Handlers — read-only, no patient identifiers in, none out.
# --------------------------------------------------------------------------
def _rag_retrieve(args: dict[str, Any]) -> list[dict[str, str]]:
    from .. import rag

    # rag.retrieve already sanitises and screens externally retrieved hits.
    return rag.retrieve(args["query"], top_k=int(args["top_k"]))


def _redflags_lookup(args: dict[str, Any]) -> list[dict[str, str]]:
    from .. import redflags

    term = args["term"].strip().lower()
    hits = []
    for rule in redflags.RED_FLAG_RULES:
        haystack = " ".join([rule.name.replace("_", " "), rule.reason, *rule.patterns]).lower()
        if term in haystack:
            hits.append({"name": rule.name, "forcedAcuity": rule.forced_acuity, "reason": rule.reason})
    return hits[:5]


# [Agentic] Care-Routing's tools. Each handler reads the CASE CONTEXT the
# routing agent hands the gateway: {"agent", "state", "facilities"}. The
# implementation (directory, hours snapshot, OneMap + its per-case budget and
# circuit breaker) stays on the agent; the gateway is the choke point in front.
def _facility(args: dict[str, Any], context: dict[str, Any]) -> Any:
    # KeyError -> an error observation: only a facility the agent has already
    # verified for THIS case can be asked about, whatever id the model sends.
    return context["facilities"][args["clinic_id"]]


def _clinic_lookup(args: dict[str, Any], context: dict[str, Any]) -> Any:
    state = context["state"]
    return context["agent"].lookup.find_candidate_clinics(
        state.latitude, state.longitude, programme=args["programme"], limit=args["limit"],
    )


def _hours_lookup(args: dict[str, Any], context: dict[str, Any]) -> Any:
    facility = _facility(args, context)
    return context["agent"].hours.match(postal=facility.postal, name=facility.name)


def _travel_estimate(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return context["agent"]._travel_estimate(context["state"], _facility(args, context), args["transport"])


REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    REGISTRY[spec.name] = spec
    return spec


register(ToolSpec(
    name="rag.retrieve",
    description=(
        "Search the clinical-guidance corpus and return up to top_k cited snippets "
        "(title, snippet, source). Use a short symptom-focused query."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 200},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    allowed_agents=frozenset({"classifier", "handoff", "reflection", "mcp"}),
    handler=_rag_retrieve,
))

register(ToolSpec(
    name="redflags.lookup",
    description=(
        "Look up the deterministic red-flag rules whose name, reason or trigger phrases "
        "contain a term, e.g. 'chest' or 'breath'. Returns rule name, forced acuity and reason."
    ),
    parameters={
        "type": "object",
        "properties": {"term": {"type": "string", "minLength": 3, "maxLength": 60}},
        "required": ["term"],
        "additionalProperties": False,
    },
    allowed_agents=frozenset({"reflection", "mcp"}),
    handler=_redflags_lookup,
))


# Per-case ceilings, sized from Care-Routing's own design maxima so a normal
# case never meets them: <= 12 directory rows checked for hours + <= 3 ReAct
# lookups; <= 5 shortlist + <= 5 walk-replan + <= 3 ReAct + 1 emergency/urgent
# route. A case that hits one is a loop bug, and the gateway stops it there.
_CLINIC_ID = {"type": "string", "minLength": 1, "maxLength": 120}
register(ToolSpec(
    name="clinic.lookup",
    description="Nearest clinics in the CHAS directory to the case's own location (never an argument).",
    parameters={
        "type": "object",
        "properties": {
            "programme": {"type": "string", "enum": ["CHAS"], "default": "CHAS"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 12},
        },
        "additionalProperties": False,
    },
    allowed_agents=frozenset({"routing"}), handler=_clinic_lookup, owner="Marcus Teh",
    contextual=True, per_case_limit=2,
))
register(ToolSpec(
    name="travel.estimate",
    description="Re-estimate travel to one verified candidate by a given transport mode.",
    parameters={
        "type": "object",
        "properties": {
            "clinic_id": _CLINIC_ID,
            # Mirrors routing._SUPPORTED_TRANSPORT; the per-request model schema
            # (routing.routing_schema_for) binds the live set at call time.
            "transport": {"type": "string", "enum": ["walk", "cycle", "public", "drive", "taxi"]},
        },
        "required": ["clinic_id", "transport"],
        "additionalProperties": False,
    },
    allowed_agents=frozenset({"routing"}), handler=_travel_estimate, owner="Marcus Teh",
    contextual=True, per_case_limit=16,
))
register(ToolSpec(
    name="facility.hours.lookup",
    description="Fetch the verified opening-hours record for one verified candidate.",
    parameters={
        "type": "object",
        "properties": {"clinic_id": _CLINIC_ID},
        "required": ["clinic_id"],
        "additionalProperties": False,
    },
    allowed_agents=frozenset({"routing"}), handler=_hours_lookup, owner="Marcus Teh",
    contextual=True, per_case_limit=16,
))

#: The external MCP principal (see app/mcp_server.py).
MCP_PRINCIPAL = "mcp"


def mcp_name(spec: ToolSpec) -> str:
    """MCP tool names may not contain dots."""
    return spec.name.replace(".", "_")


def mcp_tools() -> dict[str, ToolSpec]:
    """[Agentic] MCP name -> spec for every tool published over MCP: open to the
    'mcp' principal, read-only, executable and NOT contextual. The one
    definition the MCP server, the MCP client path and the agent cards share,
    so what a card says is reachable over MCP is what the server publishes."""
    return {
        mcp_name(spec): spec for spec in REGISTRY.values()
        if MCP_PRINCIPAL in spec.allowed_agents and spec.read_only
        and spec.handler is not None and not spec.contextual
    }


def mcp_tools_by_registry_name() -> set[str]:
    return {spec.name for spec in mcp_tools().values()}


def tool_transport() -> str:
    """CAREROUTE_TOOL_TRANSPORT, read per call so a process (or a test) can
    flip it. Anything but "mcp" means in-process — the default."""
    return "mcp" if os.getenv("CAREROUTE_TOOL_TRANSPORT", "inprocess").strip().lower() == "mcp" else "inprocess"


def catalog() -> list[dict[str, Any]]:
    """Every tool in the system. Since 2026-09-24 every one of them is executed
    by this gateway, so the catalogue is simply the registry."""
    return sorted((spec.describe() for spec in REGISTRY.values()), key=lambda t: t["name"])


def _count(tool: str, agent: str, outcome: str) -> None:
    try:
        from .. import metrics

        metrics.inc(metrics.TOOL_CALLS, tool=tool, agent=agent, outcome=outcome)
    except Exception:
        logger.debug("tool-call metric not recorded", exc_info=True)


def call(agent: object, name: str, arguments: Any, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute ONE tool call under the gateway's policy. Never raises.

    Returns {"ok": True, "tool", "observation", "outcome", "transport", "latencyMs"}
    or {"ok": False, "tool", "error", "outcome", "transport", "latencyMs"}.
    `outcome` is the label the metric carries (ok / refused / unknown /
    invalid_arguments / quota_exceeded / error). Error strings name the reason
    and, for a handler failure, only the exception TYPE — an exception message
    can carry data, and this string goes straight back into a prompt.

    `context` is the per-case context a CONTEXTUAL tool needs (module
    docstring); the gateway also keeps that case's call counts in it."""
    from ..agents.base import ToolAccessError, enforce_tool_access

    slug = str(getattr(agent, "SLUG", "unknown"))
    start = time.perf_counter()
    transport = "inprocess"

    def _fail(reason: str, outcome: str) -> dict[str, Any]:
        _count(name if name in REGISTRY else "unknown", slug, outcome)
        return {"ok": False, "tool": name, "error": reason, "outcome": outcome, "transport": transport,
                "latencyMs": round((time.perf_counter() - start) * 1000, 2)}

    spec = REGISTRY.get(name)
    if spec is None:
        return _fail(f"unknown tool {name!r}", "unknown")
    if spec.handler is None:
        return _fail(f"tool {name!r} has no gateway executor", "refused")
    if slug not in spec.allowed_agents:
        return _fail(f"agent {slug!r} is not permitted to call {name!r}", "refused")
    try:
        enforce_tool_access(agent, name)
    except ToolAccessError:
        return _fail(f"{name!r} is not on agent {slug!r}'s tool allow-list", "refused")
    try:
        clean = validate_arguments(spec.parameters, arguments)
    except ToolArgumentError as exc:
        return _fail(f"invalid argument: {exc}", "invalid_arguments")
    if spec.contextual:
        if context is None:
            return _fail(f"tool {name!r} requires a case context", "refused")
        # Per-case quota. The count lives in the case's own context, so two
        # concurrent cases can never spend each other's budget.
        calls: Counter = context.setdefault("calls", Counter())
        if spec.per_case_limit is not None and calls[name] >= spec.per_case_limit:
            return _fail(f"per-case quota of {spec.per_case_limit} {name!r} call(s) exhausted", "quota_exceeded")
        calls[name] += 1
    try:
        if spec.contextual:
            observation = spec.handler(clean, context)
        elif slug != MCP_PRINCIPAL and tool_transport() == "mcp" and name in mcp_tools_by_registry_name():
            # [Agentic] MCP client path: authorised and validated above, executed
            # over the protocol. The MCP server's own principal never takes this
            # branch, so a server cannot recurse into another server.
            from . import mcp_client

            if mcp_client.available():
                transport = "mcp"
                remote = mcp_client.call_tool(mcp_name(spec), clean)
                if not remote.get("ok"):
                    return _fail(f"tool {name!r} failed over MCP: {str(remote.get('error', ''))[:200]}", "error")
                observation = remote["observation"]
            else:
                # A missing optional package must not take the tool away from a
                # triage; say so loudly and run it in-process.
                logger.warning("CAREROUTE_TOOL_TRANSPORT=mcp but the `mcp` package is not installed; "
                               "running %s in-process", name)
                observation = spec.handler(clean)
        else:
            observation = spec.handler(clean)
    except Exception as exc:  # noqa: BLE001 - a tool failure is an observation for the loop, not a crash
        logger.warning("tool %s failed for agent %s: %s", name, slug, type(exc).__name__)
        return _fail(f"tool {name!r} failed ({type(exc).__name__})", "error")
    _count(name, slug, "ok")
    return {"ok": True, "tool": name, "observation": observation, "outcome": "ok", "transport": transport,
            "latencyMs": round((time.perf_counter() - start) * 1000, 2)}
