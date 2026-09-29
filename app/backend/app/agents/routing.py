"""Care-Routing: fixed clinical tier, verified facility-selection loop.

OWNER: Marcus Teh
"""

# NOTE for Marcus Teh (Care-Routing owner) — added 2026-09-25 by James.
# What improved:
# - Public-transport routes work on the live demo. OneMap's PT response is
#   OpenTripPlanner-shaped (plan.itineraries[].legs[]) and was being parsed as a
#   driving route, so every PT request failed. app/services/onemap.py
#   `_transit_route` now parses it (mode TRANSIT), and `_route_geometry` below
#   decodes the per-leg polylines one by one and joins them.
# - Clinic hours (backend/data/gpgowhere_hours.json): all 19 clinics re-verified
#   against GPGoWhere on 24-25 Sep by postal code and exact CHAS name.
# - Live MAP4 now routes to a CHAS GP with a route (it used to go to an ED).
# Tests: tests/services/test_onemap.py, tests/agents/test_routing.py.
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import math
import re
import threading
import time
import unicodedata
from collections.abc import MutableMapping
from datetime import date, datetime
from typing import Any, ClassVar

from cachetools import TTLCache
from geopy.distance import geodesic

from app import guardrail, llm, metrics
from app.models import more_severe
from app.services import onemap as onemap_service
from app.services.onemap import SINGAPORE_TIMEZONE, OneMapClient, OneMapError
from app.tools import registry as tool_registry
from app.tools.clinic_lookup import Clinic, ClinicLookupTool
from app.tools.gpgowhere import GPGoWhereHoursDirectory

from .base import EMBEDDED_INSTRUCTION_GUARD, AgentContract, CaseState, ToolAccessError, enforce_tool_access
from .capability import AGENT, AgentCapability
from .messaging import AgentComms, AgentMessage, ConsumesMessages, enforce_comms

OWNER = "Marcus Teh"
logger = logging.getLogger("careroute.agents.routing")
# NOTE for Marcus (Care-Routing owner) — changed 2026-09-16 by James, courseware audit.
# WHY: OWASP LLM01 mitigation #1 needs the SAME embedded-instruction guard in every
# LLM prompt, asserted by tests/agents/test_prompt_hygiene.py. Only the guard
# sentence (agents.base.EMBEDDED_INSTRUCTION_GUARD) and the SYSTEM_PROMPT alias were
# added; nothing else in this prompt or agent changed. Reword freely as long as the
# shared sentence stays in SYSTEM_PROMPT.
SYSTEM_PROMPT = """You are CareRoute's facility-selection assistant. Pick exactly one
selected_clinic_id from the supplied, already eligible GP candidates. Do not change
acuity or tier. Do not invent a clinic or assume unknown language/accessibility.
Balance travel time, verified open status and the patient's stated preferences.
Return strict JSON: selected_clinic_id, confidence (0 to 1), rationale, tradeoffs
(array of at most 2 strings), tool_call (null unless you need more information).
Keep rationale at most 240 characters and each tradeoff at most 160 characters.

You may act before you decide. If a fact you need is missing, set tool_call to
{"name": <tool>, "arguments": {...}} and you will receive an observation on the
next turn; selected_clinic_id is then provisional and ignored. Tools available:
- travel.estimate {clinic_id, transport}: re-estimate travel to one candidate by
  another transport mode (walk, cycle, public, drive, taxi).
- facility.hours.lookup {clinic_id, transport: null}: the verified opening-hours
  record for one candidate (open now, after-hours, when last verified).
Use at most a few tool calls, then decide. Never request a clinic that is not in
the candidate list.
""" + EMBEDDED_INSTRUCTION_GUARD

# ---------------------------------------------------------------------------
# [Agentic] TOOL REGISTRY for the bounded ReAct loop in `_select`.
#
# NOTE for Marcus (Care-Routing owner) — added 2026-09-15 by James, courseware
# audit. WHY: the Agentic module's Day 2 deck teaches the agent loop (reason →
# act → observe → reason), function calling with declared tool schemas, and a
# tool registry the model chooses from. CareRoute had none of these: every LLM
# call was single-shot, and the tools were invoked by fixed Python before the
# model was asked anything. Your agent was the natural home because it already
# owns two real tools (travel estimate, hours lookup) and a bounded-loop
# philosophy (`_replan_public_transport_failure`). WHAT: the model may now
# *request* one of the two tools below before committing to a clinic. Every
# request is validated against this registry and the verified candidate list,
# executed through the same `enforce_tool_access` gate as the pre-computed
# calls, and the loop is capped at `_MAX_TOOL_TURNS`. WHAT DID NOT CHANGE: tier
# and acuity are still untouchable, the selection is still an ID from the
# verified list, `_validate_decision` is still the trust boundary, and a model
# that never calls a tool behaves exactly as before (all 40 of your tests pass
# unmodified). The trace lands in `state.routing_plan["tool_calls"]`.
#
# [Agentic] 2026-09-24 (James, protocols workstream): every routing tool call —
# the deterministic shortlist ones AND the model-chosen ones — now goes through
# the central gateway (`tools/registry.call`, via `_gateway` below), so the
# platform's policy applies to them: two-key authorisation, argument-schema
# validation, a per-case quota, and the careroute_tool_calls_total metric. The
# schemas therefore live in the gateway, and this model-facing view is DERIVED
# from it: the schema shown to the model is, by construction, the one enforced.
# Your implementations (`_travel_estimate`, `self.hours`, `self.lookup`) are
# unchanged; the gateway calls them with a per-case context.
# ---------------------------------------------------------------------------
ROUTING_TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    name: {"description": tool_registry.REGISTRY[name].description,
           "parameters": tool_registry.REGISTRY[name].parameters}
    for name in ("travel.estimate", "facility.hours.lookup")
}
#: Hard cap on tool calls per selection. Day 2's loop-safety guidance and this
#: repo's own ≤ 9-turn budget both say: a loop without a ceiling is a bug.
_MAX_TOOL_TURNS = 3

# This is an API boundary, not just a prompt instruction: OpenAI Structured
# Outputs constrains its response shape. The selected ID is still checked
# against the locally verified candidate list below. `tool_call` is nullable
# rather than optional because strict mode requires every property to be
# listed in `required`; a model that wants to decide simply sends null.
ROUTING_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_clinic_id": {"type": "string"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "tradeoffs": {
            "type": "array", "items": {"type": "string"},
        },
        "tool_call": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": list(ROUTING_TOOL_REGISTRY)},
                        "arguments": {
                            "type": "object",
                            "properties": {
                                "clinic_id": {"type": "string"},
                                "transport": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            },
                            "required": ["clinic_id", "transport"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["name", "arguments"],
                    "additionalProperties": False,
                },
            ],
        },
    },
    "required": ["selected_clinic_id", "confidence", "rationale", "tradeoffs", "tool_call"],
    "additionalProperties": False,
}

_CARE_TIER_BY_ACUITY = {
    "P1_RESUSCITATION": "Emergency Department", "P2_EMERGENT": "Emergency Department",
    "P3_URGENT": "Urgent Care", "P4_NON_URGENT": "GP", "P5_SELF_CARE": "Telehealth",
}
#: How cautious each tier is, most urgent last — see `tier_rank`.
_TIER_URGENCY = {"Telehealth": 0, "GP": 1, "Urgent Care": 2, "Emergency Department": 3}
_FALLBACK_BY_TIER = {
    "Emergency Department": ("Nearest public 24-hour Emergency Department (location unavailable)", 0),
    "Urgent Care": ("Nearest urgent-care facility (directory unavailable)", 45),
    "GP": ("Nearest CHAS GP clinic (location or hours unavailable)", 90),
    "Telehealth": ("CareRoute Telehealth", 15),
}
# P2 destinations are a deliberately small, code-reviewed reference dataset,
# not an LLM-generated or scraped list.  It contains the public 24-hour ED
# destinations only: general public hospitals with an ED plus KKH's emergency
# service. Alexandra Hospital and IMH are intentionally excluded; private
# hospitals are not used for this emergency default. Coordinates/address were
# verified against OneMap on 2026-09-22 and should be reviewed periodically.
_PUBLIC_24H_EDS: tuple[dict[str, object], ...] = (
    {"id": "cgh", "name": "Changi General Hospital", "address": "2 Simei Street 3, Singapore 529889", "latitude": 1.340825955005227, "longitude": 103.9494672270745},
    {"id": "ktph", "name": "Khoo Teck Puat Hospital", "address": "90 Yishun Central, Singapore 768828", "latitude": 1.424081018718208, "longitude": 103.8385788886893},
    {"id": "kkh", "name": "KK Women's and Children's Hospital", "address": "100 Bukit Timah Road, Singapore 229899", "latitude": 1.310490074290648, "longitude": 103.8468133216193},
    {"id": "nuh", "name": "National University Hospital", "address": "5 Lower Kent Ridge Road, Singapore 119074", "latitude": 1.294835541892745, "longitude": 103.7837257956727},
    {"id": "ntfgh", "name": "Ng Teng Fong General Hospital", "address": "1 Jurong East Street 21, Singapore 609606", "latitude": 1.333606191315339, "longitude": 103.7454483629276},
    {"id": "skh", "name": "Sengkang General Hospital", "address": "110 Sengkang East Way, Singapore 544886", "latitude": 1.394393085742186, "longitude": 103.8931640854246},
    {"id": "sgh", "name": "Singapore General Hospital", "address": "1 Hospital Crescent, Singapore 169608", "latitude": 1.279643839702221, "longitude": 103.8355417646629},
    {"id": "ttsh", "name": "Tan Tock Seng Hospital", "address": "11 Jalan Tan Tock Seng, Singapore 308433", "latitude": 1.319673335099754, "longitude": 103.8479397660215},
    {"id": "wh", "name": "Woodlands Hospital", "address": "17 Woodlands Drive 17, Singapore 737628", "latitude": 1.42468138298224, "longitude": 103.7947438208185},
)
_SPEED_KMH = {"walk": 4.8, "cycle": 15.0, "public": 22.0, "drive": 30.0, "taxi": 30.0}
_ONEMAP_ROUTE_TYPE = {
    "walk": "walk", "cycle": "cycle", "public": "pt", "drive": "drive", "taxi": "drive",
}
_MAX_CANDIDATES = 5
_ROUTE_CACHE_SECONDS = 300
_ROUTE_CACHE_LOCK = threading.Lock()
#: The route cache is keyed by (origin, destination, transport) with coordinates
#: rounded to ~1 m, so a plain dict gained an entry for every distinct patient
#: location and never gave one back — an unbounded process-lifetime dict on a
#: long-running server. At <=5 shortlisted clinics per case this holds roughly a
#: hundred recent cases' worth of routes, which is far more than the 5-minute TTL
#: can actually keep warm.
_ROUTE_CACHE_MAXSIZE = 512
_MAX_WALK_REPLAN_MINUTES = 15
# A complete P4 case can make up to five shortlist estimates and up to five
# public-transport-to-walk replan checks. Limit all real OneMap requests to
# that bounded amount; ReAct requests then receive a safe local estimate once
# the budget has been spent rather than creating uncontrolled extra traffic.
_MAX_ONEMAP_CALLS_PER_CASE = 10
_ONEMAP_CIRCUIT_FAILURE_THRESHOLD = 3
_ONEMAP_CIRCUIT_COOLDOWN_SECONDS = 60.0
_SUPPORTED_TRANSPORT = frozenset(_SPEED_KMH)
_TAXI_ALIASES = frozenset({
    "grab", "grabcar", "uber", "ryde", "tada", "gojek", "private hire",
    "private-hire", "private hire car", "phv", "ride hailing", "ride-hailing",
    "comfortdelgro", "comfort delgro", "cdg zig", "zig", "transcab", "trans-cab",
})
# OneMap only routes inside Singapore. These deliberately generous bounds reject
# obviously wrong coordinates without rejecting valid edge-of-island locations.
_SINGAPORE_LATITUDE_RANGE = (1.13, 1.48)
_SINGAPORE_LONGITUDE_RANGE = (103.60, 104.10)
_MAX_LLM_RESPONSE_CHARS = 4096
_MAX_CLINIC_TEXT_CHARS = 240
_HOURS_STALE_AFTER_DAYS = 35
_STEP_DISTANCE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|km)\s*$", re.IGNORECASE)
_ARRIVAL_RE = re.compile(r"\b(arriv|destination)\b", re.IGNORECASE)
_HEAD_RE = re.compile(r"^head\s+(.+)$", re.IGNORECASE)
_TURN_RE = re.compile(r"^turn\s+(left|right)\b", re.IGNORECASE)

# Provider health is process-wide because the orchestrator owns a long-lived
# CareRoutingAgent. Keys are provider implementation types: real OneMapClient
# instances share a circuit, while independent test doubles do not leak state
# into each other. The lock keeps concurrent web requests from racing updates.
_ONEMAP_CIRCUITS: dict[str, dict[str, float | int]] = {}
_ONEMAP_CIRCUITS_LOCK = threading.Lock()


def _circuit_status(key: str | None) -> str:
    """Circuit state for one provider key. `None` means no provider at all."""
    if key is None:
        return "not_configured"
    with _ONEMAP_CIRCUITS_LOCK:
        state = _ONEMAP_CIRCUITS.get(key)
        if state and float(state["opened_until"]) > time.monotonic():
            return "open"
    return "closed"


def onemap_status() -> dict[str, str | bool]:
    """[Ops] Deployment readiness for OneMap, for `/api/health`.

    Exists because missing credentials degrade SILENTLY: the agent keeps
    returning a labelled straight-line estimate and the patient still sees a
    map (tiles are unauthenticated), so a demo deployed without
    ONEMAP_EMAIL/ONEMAP_PASSWORD looks healthy until someone reads the fine
    print under the map. Same reason `staffAuth` is reported.

    `configured` is known at boot and catches the deploy-config drift before
    any traffic; `circuit` catches credentials that are present but rejected,
    once real routing calls have failed. Neither costs a network call, so the
    ALB health check stays cheap.

    routing: "live"           -> real OneMap itineraries and route geometry
             "estimate_only"  -> distance-and-speed fallback for every case
    """
    configured = bool(onemap_service.ONEMAP_EMAIL and onemap_service.ONEMAP_PASSWORD)
    key = f"{OneMapClient.__module__}.{OneMapClient.__qualname__}" if configured else None
    circuit = _circuit_status(key)
    return {
        "configured": configured,
        "circuit": circuit,
        "routing": "live" if configured and circuit == "closed" else "estimate_only",
    }


def tier_for_acuity(code: str) -> str:
    """Clinical policy floor. This function is deliberately not LLM-controlled."""
    # An unknown acuity must never silently become a low-acuity GP route.
    return _CARE_TIER_BY_ACUITY.get(code, "Emergency Department")


def tier_rank(tier: str) -> int:
    """How CAUTIOUS a care tier is; higher is more urgent.

    Exists so a caller can ask "is this tier below the acuity floor?" instead of
    "is it different from it?". Equality alone cannot tell an under-routed case
    apart from a deliberately more cautious one, and treating them the same is
    how an escalation-only critic ends up de-escalating (see ReflectionAgent).

    An unrecognised tier ranks BELOW every real one: an unknown destination is
    not evidence of caution, so it stays correctable.
    """
    return _TIER_URGENCY.get(tier, -1)


def reroute(state: CaseState) -> tuple[str, str, int]:
    """Network-free safety floor used by the Reflection agent."""
    tier = tier_for_acuity(state.acuity_code)
    clinic, wait = _FALLBACK_BY_TIER[tier]
    state.care_tier, state.clinic, state.wait_time_min = tier, clinic, wait
    # Every navigation field describes the clinic we just abandoned. The API
    # response and the map render straight from them, so leaving them behind
    # hands the patient turn-by-turn directions — and a pin — for a destination
    # this call decided against. Mirrors what `CareRoutingAgent.run` clears on
    # its own non-GP path.
    state.travel_estimate_source = "not_applicable"
    state.route_instructions = []
    state.route_available = False
    state.route_geometry = []
    state.clinic_latitude, state.clinic_longitude = None, None
    state.effective_transport_mode = None
    state.routing_plan = {}
    state.alternative_clinics = []
    state.routing_clarification = None
    return tier, clinic, wait


class RoutingToolError(RuntimeError):
    """The gateway refused or failed a routing tool call. `outcome` is the
    gateway's label (refused / quota_exceeded / invalid_arguments / error)."""

    def __init__(self, message: str, *, outcome: str) -> None:
        super().__init__(message)
        self.outcome = outcome


def _facility_id(clinic: Clinic) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", clinic.name.lower()).strip("-")
    return f"chas-{clinic.postal}-{name}"[:120]


def _clean_external_text(value: object, *, limit: int = _MAX_CLINIC_TEXT_CHARS) -> str:
    """Normalize untrusted directory/map text before displaying or prompting it.

    Clinic and route data are external data, not instructions. Reject text that
    contains control characters, is too long, or trips the output-injection
    screen rather than letting it reach the UI or the selection model.
    """
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = "".join(char for char in text if char.isprintable())
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > limit or guardrail.screen_output(text).status == "flagged":
        return ""
    return text


def _is_singapore_coordinate(latitude: object, longitude: object) -> bool:
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return False
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return False
    return (
        math.isfinite(latitude) and math.isfinite(longitude)
        and _SINGAPORE_LATITUDE_RANGE[0] <= latitude <= _SINGAPORE_LATITUDE_RANGE[1]
        and _SINGAPORE_LONGITUDE_RANGE[0] <= longitude <= _SINGAPORE_LONGITUDE_RANGE[1]
    )


def _transport_mode(value: object) -> str | None:
    """Return only an explicitly supported transport value; never guess."""
    mode = str(value).strip().lower() if isinstance(value, str) else ""
    if mode in _TAXI_ALIASES:
        return "taxi"
    return mode if mode in _SUPPORTED_TRANSPORT else None


def _hours_freshness(verified_at: object) -> str:
    """Classify provenance without treating an unparseable date as current.

    "Today" is resolved in Singapore time, not the host's local zone: these are
    Singapore clinic opening hours, so a server running in UTC must not report
    hours as a day staler than they are.
    """
    try:
        today = datetime.now(SINGAPORE_TIMEZONE).date()
        age_days = (today - date.fromisoformat(str(verified_at))).days
    except (TypeError, ValueError):
        return "unknown"
    return "fresh" if 0 <= age_days <= _HOURS_STALE_AFTER_DAYS else "stale"


def _safe_clinic(clinic: object) -> Clinic | None:
    """Validate a directory row before coordinates/text reach maps or the LLM."""
    if not isinstance(clinic, Clinic) or not _is_singapore_coordinate(clinic.latitude, clinic.longitude):
        return None
    name = _clean_external_text(clinic.name)
    address = _clean_external_text(clinic.address)
    postal = _clean_external_text(clinic.postal, limit=12)
    phone = _clean_external_text(clinic.phone, limit=40)
    if not name or not address or not re.fullmatch(r"\d{6}", postal):
        return None
    raw_programmes = clinic.programmes if isinstance(clinic.programmes, (list, tuple)) else []
    programmes = [p for p in (_clean_external_text(item, limit=40) for item in raw_programmes) if p]
    return Clinic(name, address, postal, phone, float(clinic.latitude), float(clinic.longitude), programmes)


def _one_map_url(latitude: float, longitude: float) -> str:
    """Official OneMap location view for a verified clinic coordinate."""
    return f"https://www.onemap.gov.sg/?lat={latitude:.6f}&lng={longitude:.6f}&zoom=17"


def routing_schema_for(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind the model's selection field to the verified candidate IDs.

    Structured Outputs guarantees the response shape; this per-request enum
    also constrains the only action the model may take. Local validation below
    remains the final trust boundary.
    """
    schema = copy.deepcopy(ROUTING_RESPONSE_SCHEMA)
    ids = [candidate["clinic_id"] for candidate in candidates]
    schema["properties"]["selected_clinic_id"] = {"type": "string", "enum": ids}
    # The tool-call action space is bound the same way: a tool may only be
    # asked about a clinic the deterministic filter already verified, and only
    # by a transport mode the estimator supports.
    tool_object = schema["properties"]["tool_call"]["anyOf"][1]
    tool_object["properties"]["arguments"]["properties"]["clinic_id"] = {"type": "string", "enum": ids}
    tool_object["properties"]["arguments"]["properties"]["transport"] = {
        "anyOf": [{"type": "string", "enum": sorted(_SUPPORTED_TRANSPORT)}, {"type": "null"}],
    }
    return schema


class CareRoutingAgent(ConsumesMessages):
    """Maps acuity -> care tier and looks up a mock clinic + wait time."""
    """Selects a GP only after deterministic eligibility filtering.

    The LLM's action space is the supplied ``clinic_id`` list.  It has no
    output channel for a wrong-tier, closed or invented provider.
    """

    SLUG = "routing"
    TOOL_ALLOWLIST: ClassVar[list[str]] = ["clinic.lookup", "facility.hours.lookup", "travel.estimate", "llm.complete"]
    AUTONOMY_LEVEL = 2
    PROMPT_PATTERN = (
        "Filter verified CHAS/GPGoWhere candidates, then a bounded ReAct loop: the model may call "
        "travel.estimate / facility.hours.lookup (<= 3 turns, registry-validated) before a constrained "
        "JSON ID selection with validation."
    )
    CAPABILITY = AgentCapability(
        reasoning="Trades off eligible clinics' travel, verified hours and explicit patient preferences in a bounded ID-only choice.",
        action_space=(
            "select an eligible clinic",
            "call travel.estimate or facility.hours.lookup on an eligible clinic before deciding (<= 3 turns)",
            "select deterministic lowest-cost clinic",
            "use safe tier fallback",
        ),
        memory="Reads final classifier/safety announcements; facility-hours snapshot is versioned reference data, not patient memory.",
        tools=("clinic.lookup", "facility.hours.lookup", "travel.estimate", "llm.complete"),
        uses_trained_model=False, classification=AGENT,
        justification="Agency is restricted to logistics among validated GP candidates; clinical acuity and facility eligibility remain deterministic safety controls.",
    )
    CONTRACT = AgentContract(
        writes=frozenset({
            "care_tier", "clinic", "wait_time_min", "travel_estimate_source", "route_instructions", "route_geometry", "route_available",
            "clinic_latitude", "clinic_longitude", "routing_reason", "routing_clarification", "routing_plan", "alternative_clinics", "effective_transport_mode",
        }),
        returns=frozenset({"source", "care_tier", "clinic", "wait_time_min"}),
    )
    COMMS = AgentComms(publishes=frozenset({"care.routed"}), subscribes=frozenset({"acuity.classified", "safety.override"}))

    def __init__(
        self,
        lookup: ClinicLookupTool | None = None,
        hours: GPGoWhereHoursDirectory | None = None,
        maps: OneMapClient | None = None,
        *,
        use_onemap: bool = True,
        route_cache: MutableMapping[tuple, tuple[float, tuple]] | None = None,
    ) -> None:
        self.lookup = lookup or ClinicLookupTool()
        self.hours = hours or GPGoWhereHoursDirectory()
        self.maps = maps
        # Accepted from the caller so a per-REQUEST agent instance can keep
        # sharing one process-wide cache: the entries are public directory
        # geometry, not case data, and rebuilding the cache per request would
        # mean paying OneMap for routes we already have.
        self._route_cache: MutableMapping[tuple, tuple[float, tuple]] = (
            route_cache if route_cache is not None
            else TTLCache(maxsize=_ROUTE_CACHE_MAXSIZE, ttl=_ROUTE_CACHE_SECONDS)
        )
        self._onemap_calls = 0
        self._onemap_budget_remaining = _MAX_ONEMAP_CALLS_PER_CASE
        self._onemap_budget_exhausted = False
        # OneMap is an enhancement, never a dependency for a safety routing
        # decision. Missing credentials leave the offline estimate available.
        if self.maps is None and use_onemap:
            with contextlib.suppress(OneMapError):
                self.maps = OneMapClient()

    def _onemap_provider_key(self) -> str | None:
        if self.maps is None:
            return None
        provider_type = type(self.maps)
        return f"{provider_type.__module__}.{provider_type.__qualname__}"

    def _onemap_circuit_allows_request(self) -> bool:
        """Return whether OneMap is healthy enough for one real request.

        A circuit opens after consecutive provider failures and closes after a
        cooldown plus a successful request. Skipping a call is safe because
        `_travel_estimate` always has a clearly labelled local estimate.
        """
        key = self._onemap_provider_key()
        if key is None:
            return False
        now = time.monotonic()
        with _ONEMAP_CIRCUITS_LOCK:
            state = _ONEMAP_CIRCUITS.setdefault(key, {"failures": 0, "opened_until": 0.0})
            opened_until = float(state["opened_until"])
            if opened_until > now:
                return False
            if opened_until:
                # Cooldown elapsed: allow a controlled probe rather than
                # carrying failures forward indefinitely.
                state.update({"failures": 0, "opened_until": 0.0})
            return True

    def _record_onemap_success(self) -> None:
        key = self._onemap_provider_key()
        if key is None:
            return
        with _ONEMAP_CIRCUITS_LOCK:
            _ONEMAP_CIRCUITS[key] = {"failures": 0, "opened_until": 0.0}

    def _record_onemap_failure(self) -> None:
        key = self._onemap_provider_key()
        if key is None:
            return
        now = time.monotonic()
        with _ONEMAP_CIRCUITS_LOCK:
            state = _ONEMAP_CIRCUITS.setdefault(key, {"failures": 0, "opened_until": 0.0})
            failures = int(state["failures"]) + 1
            state["failures"] = failures
            if failures >= _ONEMAP_CIRCUIT_FAILURE_THRESHOLD:
                state["opened_until"] = now + _ONEMAP_CIRCUIT_COOLDOWN_SECONDS

    def _onemap_circuit_status(self) -> str:
        return _circuit_status(self._onemap_provider_key())

    def _reset_case_tool_budget(self) -> None:
        """Reset per-case provider accounting; the circuit remains shared."""
        self._onemap_calls = 0
        self._onemap_budget_remaining = _MAX_ONEMAP_CALLS_PER_CASE
        self._onemap_budget_exhausted = False
        # A new case starts a new gateway context, and with it a fresh quota.
        self._case_tool_context: dict[str, Any] | None = None

    def _gateway(self, state: CaseState, name: str, arguments: dict[str, Any], facility: Clinic | None = None) -> Any:
        """[Agentic] Run one routing tool THROUGH the central gateway and return
        its observation; raise `RoutingToolError` if the gateway said no.

        The context is per case (keyed on the CaseState object, so helpers
        called outside `run()` get one too) and carries what the tool needs but
        the arguments must never hold: the patient's location, the verified
        facilities, this agent's OneMap client and budget. `facility` registers
        the one verified facility this call is about under its id.
        """
        context = getattr(self, "_case_tool_context", None)
        if context is None or context["state"] is not state:
            context = {"agent": self, "state": state, "facilities": {}}
            self._case_tool_context = context
        if facility is not None:
            context["facilities"][arguments["clinic_id"]] = facility
        result = tool_registry.call(self, name, arguments, context=context)
        if not result["ok"]:
            raise RoutingToolError(result["error"], outcome=result["outcome"])
        return result["observation"]

    def emit(self, state: CaseState) -> AgentMessage:
        enforce_comms(self, "care.routed")
        return AgentMessage(sender=self.SLUG, recipient="broadcast", intent="care.routed", payload={
            "care_tier": state.care_tier, "clinic": state.clinic, "wait_time_min": state.wait_time_min,
        })

    @staticmethod
    def _has_location(state: CaseState) -> bool:
        return _is_singapore_coordinate(state.latitude, state.longitude)

    @staticmethod
    def _nearest_public_ed(state: CaseState) -> dict[str, object] | None:
        """Return the nearest reviewed public 24-hour ED for a P2 case.

        This is intentionally a direct, deterministic lookup: P2 does not use
        the GP directory, OneMap road routing, or an LLM. It gives the UI a
        verified destination marker, but does not replace emergency services or
        claim that it knows ED capacity, ambulance dispatch, or live wait time.
        """
        if not CareRoutingAgent._has_location(state):
            return None
        return min(
            _PUBLIC_24H_EDS,
            key=lambda facility: geodesic(
                (state.latitude, state.longitude),
                (float(facility["latitude"]), float(facility["longitude"])),
            ).km,
        )

    def _urgent_destination(self, state: CaseState) -> dict[str, Any] | None:
        """[Agentic] The nearest option it is SAFE to send a P3 patient to.

        Until 2026-09-24 P3 got a placeholder ("directory unavailable") because
        no urgent-care provider dataset exists. The data we DO have is enough
        for an honest answer: a CHAS GP whose reviewed hours say it is open now
        AND were verified within `_HOURS_STALE_AFTER_DAYS`, or a reviewed public
        24-hour ED, which is always open and always appropriate for P3. Whichever
        is nearer wins. Unknown or stale hours do NOT qualify here, although P4
        tolerates them with a penalty: sending an urgent patient to a door that
        may be shut costs time an urgent patient does not have. Deterministic,
        no LLM, and every directory/hours call goes through the gateway. A
        directory outage leaves the EDs, so P3 always gets a destination when
        the location is known.
        """
        if not self._has_location(state):
            return None
        options: list[dict[str, Any]] = []
        try:
            enforce_tool_access(self, "clinic.lookup")
            clinics = self._gateway(state, "clinic.lookup", {"programme": "CHAS", "limit": 12})
        except (ToolAccessError, RoutingToolError):
            logger.warning("P3 GP directory unavailable; urgent destination limited to public EDs", exc_info=True)
            clinics = []
        for raw_clinic in clinics:
            clinic = _safe_clinic(raw_clinic)
            if clinic is None:
                continue
            clinic_id = _facility_id(clinic)
            try:
                enforce_tool_access(self, "facility.hours.lookup")
                profile = self._gateway(state, "facility.hours.lookup", {"clinic_id": clinic_id}, clinic)
                verified_open = (
                    profile is not None and profile.is_open() and _hours_freshness(profile.verified_at) == "fresh"
                )
            except Exception:
                # Unknown hours are not "open" for an urgent patient.
                logger.debug("P3 hours lookup failed for %s", clinic.postal, exc_info=True)
                verified_open = False
            if verified_open:
                options.append({"kind": "gp_verified_open", "facility_id": clinic_id, "clinic": clinic,
                                "dataset": "chas_gp_reviewed_open_hours", "hours_freshness": "fresh"})
        gp_open = len(options)
        for facility in _PUBLIC_24H_EDS:
            options.append({
                "kind": "public_24h_ed", "facility_id": str(facility["id"]),
                "dataset": "public_24h_ed_static_reference", "hours_freshness": "always_open",
                "clinic": Clinic(str(facility["name"]), str(facility["address"]), "000000", "",
                                 float(facility["latitude"]), float(facility["longitude"]), []),
            })
        origin = (state.latitude, state.longitude)
        for option in options:
            option["distance_km"] = geodesic(origin, (option["clinic"].latitude, option["clinic"].longitude)).km
        choice = min(options, key=lambda option: (option["distance_km"], option["facility_id"]))
        choice["gp_options_open"] = gp_open
        return choice

    async def _route_urgent(self, state: CaseState, tier: str) -> dict[str, Any] | None:
        """Publish the P3 destination; None means "no location, use the placeholder"."""
        choice = await asyncio.to_thread(self._urgent_destination, state)
        if choice is None:
            return None
        clinic: Clinic = choice["clinic"]
        state.clinic = clinic.name
        state.clinic_latitude, state.clinic_longitude = clinic.latitude, clinic.longitude
        state.wait_time_min = _FALLBACK_BY_TIER[tier][1]
        state.travel_estimate_source, state.route_instructions, state.route_available, state.route_geometry = (
            "not_applicable", [], False, [])
        if choice["kind"] == "gp_verified_open":
            state.routing_reason = (
                "Nearest urgent-care option: a CHAS GP whose reviewed opening hours show it open now. "
                "No live queue or capacity data. If symptoms worsen, go to an Emergency Department or call 995."
            )
        else:
            state.routing_reason = (
                "Nearest reviewed public 24-hour Emergency Department: no nearer GP with current, verified "
                "opening hours was found. Live wait and capacity are not known."
            )
        transport = _transport_mode(getattr(state, "transport_mode", None))
        if transport is None:
            # The destination stands; only the route needs the one logistics answer.
            state.routing_clarification = self._routing_clarification(state, "transport_mode")
        else:
            try:
                _distance, travel, source, directions, geometry = await asyncio.to_thread(
                    self._gateway, state, "travel.estimate",
                    {"clinic_id": choice["facility_id"], "transport": transport}, clinic,
                )
                state.effective_transport_mode = transport
                state.wait_time_min = travel
                state.travel_estimate_source = source
                state.route_instructions = list(directions)
                state.route_available = source == "onemap_route"
                state.route_geometry = [list(point) for point in geometry]
            except RoutingToolError:
                logger.warning("P3 travel estimate unavailable; destination kept without a route", exc_info=True)
        state.routing_plan = {
            # Same keys the P1/P2/P3 plan always carried, so no reader breaks.
            "emergency_destination": None,
            "self_transport": {"confirmed": False, "requested_transport": None, "route_requested": False,
                               "clinical_clearance": "not_assessed"},
            "urgent_destination": {
                "kind": choice["kind"], "dataset": choice["dataset"], "facility_id": choice["facility_id"],
                "address": clinic.address, "selection": "nearest_geodesic_distance",
                "hours_freshness": choice["hours_freshness"], "gp_options_open": choice["gp_options_open"],
                "live_capacity_known": False, "live_wait_known": False,
            },
        }
        return {"source": "safety_floor", "care_tier": tier, "clinic": state.clinic,
                "wait_time_min": state.wait_time_min, "reason": state.routing_reason,
                "travel_estimate_source": state.travel_estimate_source,
                "route_instructions": state.route_instructions, "route_available": state.route_available,
                "route_geometry": state.route_geometry,
                "clinic_latitude": state.clinic_latitude, "clinic_longitude": state.clinic_longitude,
                "one_map_url": _one_map_url(clinic.latitude, clinic.longitude),
                "routing_plan": state.routing_plan,
                "routing_clarification": state.routing_clarification}

    @staticmethod
    def _routing_input_issue(state: CaseState) -> str | None:
        """Return a patient-safe reason rather than routing on malformed input."""
        if state.latitude is None or state.longitude is None:
            return "Location is required to find nearby clinics."
        if not _is_singapore_coordinate(state.latitude, state.longitude):
            return "The supplied location is outside OneMap's Singapore coverage."
        if _transport_mode(getattr(state, "transport_mode", None)) is None:
            return "Please choose how you will travel before requesting a route."
        return None

    @staticmethod
    def _routing_clarification(state: CaseState, issue: str) -> dict[str, Any] | None:
        """Return one structured logistics question; never ask clinical questions."""
        if state.latitude is None or state.longitude is None:
            return {
                "kind": "location",
                "question": "May we use your location to find nearby verified clinics and directions?",
                "required_for": "nearby_clinic_routing",
            }
        if _transport_mode(getattr(state, "transport_mode", None)) is None:
            return {
                "kind": "transport_mode",
                "question": "How will you travel to the clinic?",
                "options": ["walk", "cycle", "public", "drive", "taxi"],
                "required_for": "route_estimate",
            }
        return None

    @staticmethod
    def _clinic_from_candidate(candidate: dict[str, Any]) -> Clinic:
        """Reconstruct only a previously validated clinic for a replan attempt."""
        return Clinic(
            str(candidate["name"]), str(candidate["address"]),
            str(candidate["postal"]), str(candidate.get("phone", "")),
            float(candidate["latitude"]), float(candidate["longitude"]), list(candidate.get("programmes", [])),
        )

    @staticmethod
    def _candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
        """Expose bounded verified logistics for comparison, never raw tool data."""
        return {
            "clinic_id": candidate["clinic_id"],
            "name": candidate["name"],
            "address": candidate["address"],
            "travel_time_min": candidate["travel_time_min"],
            "travel_estimate_source": candidate["travel_estimate_source"],
            "open_status": candidate["open_status"],
            "hours_freshness": candidate["hours_freshness"],
            "affordability_match": candidate["affordability_match"],
        }

    @staticmethod
    def _human_route_instructions(
        raw_steps: object, clinic: Clinic, *, transport: str, distance_km: float, travel_minutes: int,
    ) -> tuple[str, ...]:
        """Turn OneMap's terse maneuver list into safe display text.

        The route API often supplies only maneuvers (for example, ``Turn Left``)
        rather than street names.  We preserve a small number of those steps,
        add the distance when OneMap supplies it, and always state the actual
        destination.  This deliberately does not invent road names or claim a
        turn is more precise than the provider data.
        """
        mode = {
            "pt": "Use public transport", "public": "Use public transport", "walk": "Walk",
            "cycle": "Cycle", "drive": "Drive", "taxi": "Take a taxi",
        }.get(transport, "Walk")
        distance = f"{round(distance_km * 1000):,} m" if distance_km < 1 else f"{distance_km:.2f} km"
        summary = f"{mode} to {clinic.name}: about {distance} ({travel_minutes} min)."
        steps: list[str] = []
        previous_route_name = ""

        if isinstance(raw_steps, (list, tuple)):
            for raw_step in raw_steps:
                if not isinstance(raw_step, (list, tuple)) or not raw_step:
                    continue
                text = _clean_external_text(raw_step[-1])
                if not text:
                    continue
                text = re.sub(r"\s+", " ", text)
                if _ARRIVAL_RE.search(text):
                    # The provider's own arrival line is dropped: this method
                    # always appends its own `destination` below, which names
                    # the clinic and address rather than a bare "arrive".
                    continue

                # OneMap's documented second value is the route/road name.
                # It is often blank for footpaths, so use it only when the
                # provider explicitly supplied a meaningful value.
                route_name = _clean_external_text(raw_step[1]) if len(raw_step) > 1 else ""
                if route_name.lower() in {"", "unknown", "walking", "driving"}:
                    route_name = ""
                elif route_name.isupper() or route_name.islower():
                    # OneMap commonly returns all-caps road names. Title casing
                    # is easier to scan, while mixed-case names are retained.
                    route_name = route_name.title()

                # OneMap records the segment distance as a separate value in
                # some route types. Search defensively rather than depending on
                # a fixed array position.
                segment_distance = next(
                    (str(value).strip() for value in raw_step if isinstance(value, str) and _STEP_DISTANCE_RE.fullmatch(value)),
                    None,
                )
                head = _HEAD_RE.match(text)
                turn = _TURN_RE.match(text)
                repeated_road_turn = bool(
                    turn and route_name and previous_route_name
                    and route_name.casefold() == previous_route_name.casefold()
                )
                if repeated_road_turn:
                    display = f"Continue on {route_name}"
                elif head:
                    display = f"Head {head.group(1).lower()}"
                elif turn:
                    display = f"Turn {turn.group(1).lower()}"
                else:
                    display = text.rstrip(".")
                if route_name and route_name.casefold() not in display.casefold():
                    connector = "on" if head else "onto" if turn else "via"
                    display += f" {connector} {route_name}"
                if segment_distance and segment_distance not in {"0m", "0 km", "0km"}:
                    display += f" and continue for {segment_distance}"
                display += "."
                # A sequence of identical terse provider maneuvers adds no
                # useful information without road names; retain it once.
                if not steps or steps[-1] != display:
                    steps.append(display)
                previous_route_name = route_name

        # Keep the mobile response readable. The provider's map remains the
        # source of detailed live navigation; this is a concise route preview.
        if len(steps) > 4:
            steps = [*steps[:4], "Continue following OneMap's live turn-by-turn navigation."]
        destination = f"Arrive at {clinic.name} — {clinic.address}."
        return (summary, *steps, destination)

    @staticmethod
    def _route_geometry(raw_geometry: object) -> tuple[tuple[float, float], ...]:
        """Decode and validate OneMap's encoded polyline without trusting it.

        OneMap's route service returns a standard encoded polyline. Test
        adapters may provide already-decoded coordinate pairs. In both cases,
        retain only bounded Singapore points so malformed provider data cannot
        overwhelm the UI or draw a route elsewhere.
        """
        points: list[tuple[float, float]] = []
        if isinstance(raw_geometry, (list, tuple)) and raw_geometry and all(isinstance(p, str) for p in raw_geometry):
            # Public transport: one encoded polyline per leg. Each is delta-
            # encoded from zero, so decode separately, then join in order.
            for leg in raw_geometry[:50]:
                decoded = CareRoutingAgent._route_geometry(leg)
                if not decoded:
                    return ()
                points.extend(decoded)
            return tuple(points[:5_000]) if len(points) >= 2 else ()
        if isinstance(raw_geometry, (list, tuple)):
            for point in raw_geometry[:5_000]:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    return ()
                latitude, longitude = point
                if not _is_singapore_coordinate(latitude, longitude):
                    return ()
                points.append((float(latitude), float(longitude)))
            return tuple(points) if len(points) >= 2 else ()
        if not isinstance(raw_geometry, str) or not raw_geometry or len(raw_geometry) > 100_000:
            return ()

        index = latitude = longitude = 0
        try:
            while index < len(raw_geometry) and len(points) < 5_000:
                values: list[int] = []
                for _ in range(2):
                    shift = value = 0
                    while True:
                        if index >= len(raw_geometry):
                            return ()
                        byte = ord(raw_geometry[index]) - 63
                        index += 1
                        if byte < 0 or byte > 63:
                            return ()
                        value |= (byte & 0x1F) << shift
                        shift += 5
                        if not byte & 0x20:
                            break
                        if shift > 30:
                            return ()
                    values.append(~(value >> 1) if value & 1 else value >> 1)
                latitude += values[0]
                longitude += values[1]
                point = (latitude / 100_000, longitude / 100_000)
                if not _is_singapore_coordinate(*point):
                    return ()
                points.append(point)
        except (TypeError, ValueError, OverflowError):
            return ()
        return tuple(points) if len(points) >= 2 and index == len(raw_geometry) else ()

    def _travel_estimate(
        self, state: CaseState, clinic: Clinic, transport: str
    ) -> tuple[float, int, str, tuple[str, ...], tuple[tuple[float, float], ...]]:
        """Prefer OneMap's route duration; fall back to a local safe estimate."""
        speed = _SPEED_KMH.get(transport, _SPEED_KMH["walk"])
        distance_km = geodesic((state.latitude, state.longitude), (clinic.latitude, clinic.longitude)).km
        fallback_minutes = max(1, math.ceil(distance_km / speed * 60))
        if self.maps is None:
            return round(distance_km, 2), fallback_minutes, "geodesic_speed_estimate", (), ()
        cache_key = (
            round(float(state.latitude), 5), round(float(state.longitude), 5),
            round(clinic.latitude, 5), round(clinic.longitude, 5), transport,
        )
        # The cache is shared by every request in the process and the TTL map
        # is not thread-safe; a miss racing an eviction raised KeyError out of
        # run(). A cache fault is a cache miss. The value holds route FACTS
        # (distance, minutes, provider steps, geometry), never rendered text:
        # two clinics at one geocoded point share the route but not the
        # "Arrive at ..." line.
        try:
            with _ROUTE_CACHE_LOCK:
                cached = self._route_cache.get(cache_key)
        except Exception:  # noqa: BLE001 - a cache fault is a cache miss, never a failed triage
            cached = None
        if cached and time.monotonic() - cached[0] < _ROUTE_CACHE_SECONDS:
            route_distance_km, route_minutes, raw_steps, geometry = cached[1]
            directions = self._human_route_instructions(
                raw_steps, clinic, transport=transport, distance_km=route_distance_km, travel_minutes=route_minutes,
            )
            return route_distance_km, route_minutes, "onemap_route", directions, geometry
        if self._onemap_budget_remaining <= 0:
            self._onemap_budget_exhausted = True
            metrics.inc(metrics.ROUTING_FALLBACKS, reason="onemap_tool_budget_exhausted")
            return round(distance_km, 2), fallback_minutes, "geodesic_speed_estimate", (), ()
        if not self._onemap_circuit_allows_request():
            metrics.inc(metrics.ROUTING_FALLBACKS, reason="onemap_circuit_open")
            return round(distance_km, 2), fallback_minutes, "geodesic_speed_estimate", (), ()
        self._onemap_budget_remaining -= 1
        self._onemap_calls += 1
        try:
            route = self.maps.route(
                state.latitude, state.longitude, clinic.latitude, clinic.longitude,
                route_type=_ONEMAP_ROUTE_TYPE.get(transport, "walk"),
            )
            seconds = route.get("time")
            metres = route.get("distance")
            if (
                isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                and math.isfinite(seconds) and 0 <= seconds <= 12 * 60 * 60
            ):
                # A route distance outside Singapore is evidence of malformed
                # provider data, not a reason to show an implausible route.
                if isinstance(metres, (int, float)) and not isinstance(metres, bool):
                    if not math.isfinite(metres) or not 0 <= metres <= 200_000:
                        raise ValueError("implausible OneMap route distance")
                    route_distance_km = float(metres) / 1000
                else:
                    route_distance_km = distance_km
                raw_steps = route.get("instructions") or []
                route_distance_km = round(route_distance_km, 2)
                route_minutes = max(1, math.ceil(seconds / 60))
                directions = self._human_route_instructions(
                    raw_steps, clinic, transport=transport, distance_km=route_distance_km, travel_minutes=route_minutes,
                )
                geometry = self._route_geometry(route.get("geometry"))
                result = (route_distance_km, route_minutes, "onemap_route", directions, geometry)
                try:
                    with _ROUTE_CACHE_LOCK:
                        self._route_cache[cache_key] = (
                            time.monotonic(), (route_distance_km, route_minutes, raw_steps, geometry),
                        )
                except Exception:  # noqa: BLE001 - a cache fault is a cache miss, never a failed triage
                    logger.debug("route cache write failed", exc_info=False)
                self._record_onemap_success()
                return result
        except Exception:
            # Network/authentication/rate-limit faults must not stop routing.
            logger.debug("OneMap route unavailable; using geodesic estimate", exc_info=True)
        # Reached on a provider fault *or* an implausible payload: both count
        # against the circuit breaker and are reported as a fallback.
        self._record_onemap_failure()
        metrics.inc(metrics.ROUTING_FALLBACKS, reason="map_unavailable_or_invalid")
        return round(distance_km, 2), fallback_minutes, "geodesic_speed_estimate", (), ()

    def _candidates(self, state: CaseState) -> list[dict[str, Any]]:
        """Observe, enrich and deterministically filter GP candidates."""
        if not self._has_location(state):
            return []
        enforce_tool_access(self, "clinic.lookup")
        # [Agentic] Through the gateway. A refusal or directory fault raises,
        # exactly as the direct call did, and `run()` degrades to the fallback.
        clinics = self._gateway(state, "clinic.lookup", {"programme": "CHAS", "limit": 12})
        transport = _transport_mode(getattr(state, "transport_mode", None))
        if transport is None:
            return []
        max_travel = getattr(state, "max_travel_time_min", None)
        preferred = getattr(state, "preferred_clinic_id", None)
        result: list[dict[str, Any]] = []
        for raw_clinic in clinics:
            clinic = _safe_clinic(raw_clinic)
            if clinic is None:
                continue
            enforce_tool_access(self, "facility.hours.lookup")
            clinic_id = _facility_id(clinic)
            try:
                # [Agentic] Through the gateway; any gateway failure lands in
                # the same "hours unknown" branch a raising lookup always did.
                profile = self._gateway(state, "facility.hours.lookup", {"clinic_id": clinic_id}, clinic)
                # A confirmed closed clinic is ineligible. Unknown hours remain
                # selectable but are explicitly penalised and disclosed. The
                # open check sits INSIDE the guard: one unparsable window in
                # the snapshot used to make the whole lookup raise, and every
                # patient near that row fell to the placeholder clinic.
                if profile is not None and not profile.is_open():
                    continue
            except Exception:
                # A broken/expired hours snapshot must not make the router
                # crash or treat an unverified clinic as confirmed open.
                logger.debug("Hours lookup failed for %s; treating as unknown", clinic.postal, exc_info=True)
                profile = None
            freshness = _hours_freshness(profile.verified_at) if profile else "unknown"
            # A record nobody has re-verified in over a month is not evidence
            # that the clinic still opens; it is the same state of knowledge as
            # having no record at all. Reporting the staleness and then scoring
            # it as verified-open let a stale row outrank an honestly-unknown
            # clinic — and a nearer stale clinic beat a farther one someone had
            # actually checked. It stays selectable and disclosed, exactly like
            # a missing profile, and carries the same penalty.
            verified_open = profile is not None and freshness != "stale"
            metrics.inc(metrics.ROUTING_DATA_FRESHNESS, status=freshness)
            enforce_tool_access(self, "travel.estimate")
            # Cheap local estimate gates the initial candidate set. OneMap is
            # used only for the final shortlist, which bounds external calls.
            distance = geodesic((state.latitude, state.longitude), (clinic.latitude, clinic.longitude)).km
            travel = max(1, math.ceil(distance / _SPEED_KMH.get(transport, _SPEED_KMH["walk"]) * 60))
            if max_travel is not None and travel > max_travel:
                continue
            open_status = "open" if verified_open else "unknown"
            score = travel + (0 if verified_open else 20) - (15 if clinic_id == preferred else 0)
            result.append({
                "_clinic": clinic, "clinic_id": clinic_id, "name": clinic.name, "address": clinic.address,
                "postal": clinic.postal, "phone": clinic.phone, "distance_km": round(distance, 2), "travel_time_min": travel,
                "latitude": clinic.latitude, "longitude": clinic.longitude,
                "travel_estimate_source": "geodesic_speed_estimate",
                "open_status": open_status,
                "after_hours": profile.after_hours if profile else None,
                "hours_source": profile.source if profile else "unknown",
                "hours_verified_at": profile.verified_at if profile else None,
                "hours_freshness": freshness,
                # CHAS eligibility is evidenced by the lookup filter. The
                # public directory does not prove wheelchair access or staff
                # languages, so those facts must remain unknown.
                "affordability_match": "chas_eligible" if "CHAS" in clinic.programmes else "not_verified",
                "accessibility_match": "not_requested" if getattr(state, "accessibility_need", "none") == "none" else "not_verified",
                "language_support_match": "not_requested" if not getattr(state, "preferred_language", None) else "not_verified",
                "availability_status": "not_connected",
                "programmes": clinic.programmes, "score": score,
            })
        shortlisted = sorted(result, key=lambda item: (item["score"], item["clinic_id"]))[:_MAX_CANDIDATES]
        final: list[dict[str, Any]] = []
        for candidate in shortlisted:
            clinic = candidate.pop("_clinic")
            distance, travel, travel_source, directions, geometry = self._gateway(
                state, "travel.estimate", {"clinic_id": candidate["clinic_id"], "transport": transport}, clinic,
            )
            if max_travel is not None and travel > max_travel:
                continue
            candidate["distance_km"] = distance
            candidate["travel_time_min"] = travel
            candidate["travel_estimate_source"] = travel_source
            candidate["route_instructions"] = list(directions)
            candidate["route_geometry"] = [list(point) for point in geometry]
            candidate["score"] = travel + (0 if candidate["open_status"] == "open" else 20) - (
                15 if candidate["clinic_id"] == preferred else 0
            )
            final.append(candidate)
        return sorted(final, key=lambda item: (item["score"], item["clinic_id"]))

    def _replan_public_transport_failure(
        self, state: CaseState, candidates: list[dict[str, Any]], transport: str,
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
        """Try a verified nearby walking plan when every PT itinerary fails.

        This is a bounded observe-plan-act-evaluate loop: it never changes the
        clinical tier or invents transit details, and it only offers walking
        when OneMap verifies a short route within the user's stated cap.
        """
        plan: dict[str, Any] = {
            "requested_transport": transport,
            "effective_transport": transport,
            "replanned": False,
            "steps": [],
        }
        if transport != "public" or any(c["travel_estimate_source"] == "onemap_route" for c in candidates):
            return candidates, transport, plan

        plan["steps"].append("No verified public-transport itinerary was available.")
        max_travel = getattr(state, "max_travel_time_min", None)
        walking: list[dict[str, Any]] = []
        for candidate in candidates:
            clinic = self._clinic_from_candidate(candidate)
            distance, minutes, source, directions, geometry = self._gateway(
                state, "travel.estimate", {"clinic_id": candidate["clinic_id"], "transport": "walk"}, clinic,
            )
            if (
                source != "onemap_route"
                or minutes > _MAX_WALK_REPLAN_MINUTES
                or (max_travel is not None and minutes > max_travel)
            ):
                continue
            replanned = dict(candidate)
            replanned.update({
                "distance_km": distance,
                "travel_time_min": minutes,
                "travel_estimate_source": source,
                "route_instructions": list(directions),
                "route_geometry": [list(point) for point in geometry],
            })
            replanned["score"] = minutes + (0 if replanned["open_status"] == "open" else 20)
            walking.append(replanned)

        if not walking:
            plan["outcome"] = "no_verified_walking_alternative"
            return candidates, transport, plan

        plan.update({
            "effective_transport": "walk",
            "replanned": True,
            "outcome": "verified_nearby_walking_alternative",
            "steps": [
                *plan["steps"],
                "Verified nearby walking routes were compared because walking is practical for this journey.",
            ],
        })
        return sorted(walking, key=lambda item: (item["score"], item["clinic_id"])), "walk", plan

    @staticmethod
    def _validate_selection(candidate_id: object, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not isinstance(candidate_id, str):
            return None
        return next((candidate for candidate in candidates if candidate["clinic_id"] == candidate_id), None)

    @staticmethod
    def _validate_decision(data: object, candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Treat LLM output as untrusted data; return only a safe decision."""
        if not isinstance(data, dict):
            return None
        selected = CareRoutingAgent._validate_selection(data.get("selected_clinic_id"), candidates)
        confidence = data.get("confidence")
        rationale = data.get("rationale")
        tradeoffs = data.get("tradeoffs")
        if (
            selected is None
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
            or not isinstance(rationale, str)
            or not isinstance(tradeoffs, list)
            or len(tradeoffs) > 2
            or any(not isinstance(item, str) or len(item) > 160 for item in tradeoffs)
        ):
            return None
        rationale = rationale.strip()
        if not rationale:
            return None
        # LLM05: never propagate a prompt leak or injected instruction through
        # the router's rationale, even though the selection itself is bounded.
        if guardrail.screen_output(" ".join([rationale, *tradeoffs])).status == "flagged":
            return None
        # Explanation text is not an action. A model occasionally exceeds the
        # requested display limit, so preserve a valid, locally verified clinic
        # decision after screening the *full* model output, then bound what is
        # returned to callers. Invalid actions and unsafe text still fall back.
        rationale_truncated = len(rationale) > 240
        rationale = rationale[:240].rstrip()
        return selected, {
            "confidence": float(confidence),
            "rationale": rationale,
            "tradeoffs": tradeoffs,
            "rationale_truncated": rationale_truncated,
        }

    @staticmethod
    def _evaluate_final_selection(
        state: CaseState, selected: dict[str, Any], candidates: list[dict[str, Any]], *, tier: str,
    ) -> dict[str, Any]:
        """Independently verify the selected logistics action before publishing it.

        This is deliberately deterministic rather than a second LLM opinion.
        The evaluator checks only facts and policy constraints that are already
        available locally: the candidate must remain verified and geographically
        valid, must fit the fixed GP tier, cannot be known closed, and cannot
        exceed the patient's stated travel limit. It does *not* infer medical
        suitability, opening hours, accessibility, or language support.
        """
        selected_id = selected.get("clinic_id") if isinstance(selected, dict) else None
        travel_time = selected.get("travel_time_min") if isinstance(selected, dict) else None
        max_travel = getattr(state, "max_travel_time_min", None)
        candidate_match = CareRoutingAgent._validate_selection(selected_id, candidates)
        travel_is_valid = (
            isinstance(travel_time, (int, float)) and not isinstance(travel_time, bool)
            and math.isfinite(travel_time) and 1 <= travel_time <= 12 * 60
        )
        checks = {
            "fixed_tier_is_gp": tier == "GP",
            "verified_candidate": candidate_match is not None,
            "singapore_destination": (
                isinstance(selected, dict)
                and _is_singapore_coordinate(selected.get("latitude"), selected.get("longitude"))
            ),
            "travel_time_valid": travel_is_valid,
            "within_travel_limit": (
                travel_is_valid and (max_travel is None or travel_time <= max_travel)
            ),
            "not_known_closed": isinstance(selected, dict) and selected.get("open_status") != "closed",
        }
        failed_checks = [name for name, passed in checks.items() if not passed]
        return {
            "status": "accepted" if not failed_checks else "rejected",
            "selected_clinic_id": selected_id if isinstance(selected_id, str) else None,
            "checks": checks,
            "failed_checks": failed_checks,
            "purpose": "Final deterministic verification of the routing action before publication.",
        }

    # ------------------------------------------------------------------
    # [Agentic] Tool-call validation + execution for the ReAct loop.
    # NOTE for Marcus: these two helpers are new (2026-09-15, James). They sit
    # between the model and your existing `_travel_estimate` / `self.hours`
    # code and never bypass either; see the registry note near the top.
    # ------------------------------------------------------------------
    @staticmethod
    def _tool_call_request(data: object, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Return a validated {name, arguments} if the model asked for a tool,
        else None. Anything malformed is treated as "no tool call" so a bad
        request degrades to the ordinary selection validation, never to an
        exception or an unregistered action."""
        if not isinstance(data, dict):
            return None
        call = data.get("tool_call")
        if not isinstance(call, dict):
            return None
        name = call.get("name")
        arguments = call.get("arguments")
        if name not in ROUTING_TOOL_REGISTRY or not isinstance(arguments, dict):
            return {"name": str(name)[:60], "arguments": {}, "invalid": "unregistered tool or malformed arguments"}
        clinic_id = arguments.get("clinic_id")
        if CareRoutingAgent._validate_selection(clinic_id, candidates) is None:
            return {"name": name, "arguments": {}, "invalid": "clinic_id is not a verified candidate"}
        transport = arguments.get("transport")
        if name == "travel.estimate" and _transport_mode(transport) is None:
            return {"name": name, "arguments": {"clinic_id": clinic_id}, "invalid": "unsupported transport mode"}
        cleaned = {"clinic_id": clinic_id}
        if name == "travel.estimate":
            cleaned["transport"] = _transport_mode(transport)
        return {"name": name, "arguments": cleaned}

    async def _run_tool(self, state: CaseState, request: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """Execute one validated tool request and return an OBSERVATION dict.

        Observations are logistics only (numbers, enums, cleaned directory
        strings) — never patient text — so they are safe to feed back into the
        prompt. Failures become an observation with an `error`, not an
        exception: the loop must always be able to continue to a decision.
        """
        name = request["name"]
        if request.get("invalid"):
            return {"tool": name, "error": request["invalid"]}
        candidate = self._validate_selection(request["arguments"]["clinic_id"], candidates)
        clinic = self._clinic_from_candidate(candidate)
        # The allow-list is enforced at the moment of use, exactly as for the
        # pre-computed calls — a revoked tool fails the request, not the case.
        enforce_tool_access(self, name)
        try:
            # [Agentic] The model-chosen call goes through the gateway: its
            # arguments are re-validated against the registry schema there and
            # it counts against this case's quota. A gateway refusal becomes the
            # same "tool unavailable" observation a failing tool always did.
            if name == "travel.estimate":
                transport = request["arguments"]["transport"]
                distance, minutes, source, _directions, _geometry = await asyncio.to_thread(
                    self._gateway, state, name, {"clinic_id": candidate["clinic_id"], "transport": transport}, clinic,
                )
                return {
                    "tool": name, "clinic_id": candidate["clinic_id"], "transport": transport,
                    "distance_km": distance, "travel_time_min": minutes, "travel_estimate_source": source,
                }
            profile = await asyncio.to_thread(
                self._gateway, state, name, {"clinic_id": candidate["clinic_id"]}, clinic,
            )
            if profile is None:
                return {"tool": name, "clinic_id": candidate["clinic_id"], "open_now": None,
                        "hours_freshness": "unknown", "hours_source": "unknown"}
            return {
                "tool": name, "clinic_id": candidate["clinic_id"],
                "open_now": bool(profile.is_open()),
                "after_hours": profile.after_hours,
                "hours_source": _clean_external_text(profile.source, limit=60),
                "hours_verified_at": _clean_external_text(profile.verified_at, limit=40) or None,
                "hours_freshness": _hours_freshness(profile.verified_at),
            }
        except Exception:
            logger.debug("routing tool %s failed inside the ReAct loop", name, exc_info=True)
            return {"tool": name, "clinic_id": candidate["clinic_id"], "error": "tool unavailable"}

    async def _select(
        self, state: CaseState, candidates: list[dict[str, Any]], *, effective_transport: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        # NOTE for Marcus (2026-09-15, James): this is now a bounded ReAct loop.
        # Turn 1 is byte-for-byte the prompt you wrote (the privacy test that
        # pins its keys still passes). If the model answers with a `tool_call`,
        # the tool runs, its observation is appended under an "observations"
        # key, and the model is asked again — at most `_MAX_TOOL_TURNS` times.
        # A model that never calls a tool takes exactly the old single-shot
        # path. The deterministic fallback at the bottom is unchanged.
        enforce_tool_access(self, "llm.complete")
        prompt_payload = {
            "acuity": state.acuity_code, "fixed_tier": "GP",
            "preferences": {"transport": effective_transport or _transport_mode(getattr(state, "transport_mode", None)) or "unknown",
                            "max_travel_time_min": getattr(state, "max_travel_time_min", None),
                            # Client free text, and the only value in this prompt
                            # that was not screened. Directory and route strings
                            # all go through `_clean_external_text`; a preference
                            # the caller typed deserves no more trust than a row
                            # from an external directory.
                            "preferred_clinic_id": _clean_external_text(
                                getattr(state, "preferred_clinic_id", None), limit=120) or None,
                            "accessibility_need": getattr(state, "accessibility_need", "none"),
                            "preferred_language": getattr(state, "preferred_language", None),
                            "affordability_preference": getattr(state, "affordability_preference", "standard")},
            # Bounded logistics only: the full candidate carried the route
            # polyline (thousands of points), the step list, phone and postal —
            # tens of thousands of tokens the model cannot use, and an oversize
            # prompt silently became the deterministic fallback.
            "candidates": [self._candidate_summary(c) for c in candidates],
        }
        schema = routing_schema_for(candidates)
        tool_trace: list[dict[str, Any]] = []
        seen_requests: set[str] = set()
        try:
            for turn in range(_MAX_TOOL_TURNS + 1):
                prompt = json.dumps(prompt_payload)
                raw = await llm.complete(SYSTEM_PROMPT, prompt, json_mode=True, json_schema=schema,
                                         task="routing.select")
                if not isinstance(raw, str) or len(raw) > _MAX_LLM_RESPONSE_CHARS:
                    raise ValueError("LLM routing response was missing or too large")
                data = json.loads(raw)

                request = self._tool_call_request(data, candidates)
                if request is not None and turn >= _MAX_TOOL_TURNS:
                    # Budget spent and the model still wants to act. Its
                    # `selected_clinic_id` was declared provisional, so it is
                    # NOT promoted to a decision; the deterministic order wins.
                    logger.debug("routing ReAct loop exhausted its %d-turn budget", _MAX_TOOL_TURNS)
                    break
                if request is not None:
                    # ACT + OBSERVE. A repeated identical request is answered
                    # from the trace rather than re-executed, so a model stuck
                    # on one question cannot burn the budget on the same call.
                    key = json.dumps(request, sort_keys=True)
                    if key in seen_requests:
                        observation = {"tool": request["name"], "error": "already answered above; decide now"}
                    else:
                        seen_requests.add(key)
                        observation = await self._run_tool(state, request, candidates)
                    tool_trace.append({
                        "turn": turn + 1, "tool": request["name"],
                        "arguments": request.get("arguments", {}), "observation": observation,
                    })
                    prompt_payload["observations"] = [t["observation"] for t in tool_trace]
                    continue

                # DECIDE. Same trust boundary as before.
                decision = self._validate_decision(data, candidates)
                if decision:
                    selected, safe_decision = decision
                    safe_decision["tool_trace"] = tool_trace
                    return selected, safe_decision, "llm"
                break
        except Exception:
            # Provider, transport and parser failures are non-critical: the
            # independently verified candidate list has a deterministic order.
            # Logged rather than swallowed so a genuine defect in
            # `_validate_decision` is visible instead of silently degrading
            # every request to the deterministic path forever.
            logger.debug("LLM facility selection unavailable; using deterministic order", exc_info=True)
        selected = candidates[0]
        return selected, {"confidence": None,
                          "rationale": "Lowest deterministic travel/availability score selected because LLM reasoning was unavailable or invalid.",
                          "tradeoffs": [], "tool_trace": tool_trace}, "deterministic_fallback"

    async def run(self, state: CaseState) -> dict:
        self._reset_case_tool_budget()
        state.routing_reason = None
        state.effective_transport_mode = None
        state.routing_clarification = None
        state.routing_plan = {}
        state.alternative_clinics = []
        safety = self.received_payload("safety.override") if getattr(self, "_consumed", False) else None
        # The announcement can only RAISE what the state already says: after a
        # Reflection re-run bumped P2 to P1 the first-pass announcement (forced
        # P2) was still in the inbox and a resuscitation case took the P2
        # branch, self-transport directions included.
        forced = str(safety["forced_acuity"]) if safety and safety.get("triggered") and safety.get("forced_acuity") else None
        code = more_severe(state.acuity_code, forced) if forced else state.acuity_code
        tier = tier_for_acuity(code)
        state.care_tier = tier
        # [Agentic] P3 gets a concrete destination (see `_route_urgent`). It
        # keys on the FINAL code, after any safety override, so a case raised to
        # P2 never reaches this branch; with no location it falls through to
        # the unchanged placeholder below.
        if code == "P3_URGENT":
            urgent = await self._route_urgent(state, tier)
            if urgent is not None:
                return urgent
        # The available GP directory only evidences P4 routing. P1 never gets
        # a hospital recommendation: SCDF dispatch must decide the emergency
        # destination. P2 uses the reviewed public-ED reference dataset below;
        # P3 is handled above by `_route_urgent`; it reaches this block (and
        # its honest placeholder) only when no location was supplied.
        if tier != "GP":
            clinic, wait = _FALLBACK_BY_TIER[tier]
            emergency_destination: dict[str, object] | None = None
            if code == "P1_RESUSCITATION":
                clinic = "Call 995 for SCDF emergency dispatch"
                state.routing_reason = "P1 resuscitation requires immediate SCDF emergency dispatch; the app must not choose a hospital destination."
            elif code == "P2_EMERGENT":
                emergency_destination = self._nearest_public_ed(state)
                if emergency_destination:
                    clinic = str(emergency_destination["name"])
                    state.clinic_latitude = float(emergency_destination["latitude"])
                    state.clinic_longitude = float(emergency_destination["longitude"])
                    state.routing_reason = "Nearest reviewed public 24-hour Emergency Department selected from the static P2 reference dataset; availability and ambulance dispatch are not known."
                else:
                    state.routing_reason = "Location was unavailable, so no specific Emergency Department was selected. Seek emergency medical assistance immediately."
            else:
                state.routing_reason = "This care tier is not eligible for CHAS GP selection."
            state.clinic, state.wait_time_min = clinic, wait
            state.travel_estimate_source, state.route_instructions, state.route_available, state.route_geometry = "not_applicable", [], False, []
            requested_transport = _transport_mode(getattr(state, "transport_mode", None))
            self_transport_confirmed = bool(getattr(state, "emergency_self_transport_confirmed", False))
            if code == "P2_EMERGENT" and emergency_destination and self_transport_confirmed and requested_transport:
                # The patient explicitly opted into directions after the safety
                # warning. This does not certify clinical fitness to travel and
                # never applies to P1 or ambulance dispatch.
                destination = Clinic(
                    str(emergency_destination["name"]), str(emergency_destination["address"]),
                    "000000", "", float(emergency_destination["latitude"]),
                    float(emergency_destination["longitude"]), [],
                )
                _distance, travel, source, directions, geometry = await asyncio.to_thread(
                    self._gateway, state, "travel.estimate",
                    {"clinic_id": str(emergency_destination["id"]), "transport": requested_transport}, destination,
                )
                state.effective_transport_mode = requested_transport
                state.wait_time_min = travel
                state.travel_estimate_source = source
                state.route_instructions = list(directions)
                state.route_available = source == "onemap_route"
                state.route_geometry = [list(point) for point in geometry]
                state.routing_reason = (
                    "Self-transport route requested after explicit patient confirmation. "
                    "If travel is not safe or symptoms worsen, call 995."
                )
            if code == "P2_EMERGENT" and emergency_destination and self_transport_confirmed and not requested_transport:
                # Confirmed, but the API default transport is "unknown": ask
                # the one logistics question instead of returning nothing.
                state.routing_clarification = self._routing_clarification(state, "transport_mode")
            if emergency_destination is None:
                state.clinic_latitude, state.clinic_longitude = None, None
            state.routing_plan = {
                "emergency_destination": (
                    {
                        "dataset": "public_24h_ed_static_reference",
                        "facility_id": emergency_destination["id"],
                        "address": emergency_destination["address"],
                        "selection": "nearest_geodesic_distance",
                        "live_capacity_known": False,
                        "live_wait_known": False,
                    }
                    if emergency_destination else None
                ),
                "self_transport": {
                    "confirmed": self_transport_confirmed if code == "P2_EMERGENT" else False,
                    "requested_transport": requested_transport if code == "P2_EMERGENT" else None,
                    "route_requested": bool(
                        code == "P2_EMERGENT" and emergency_destination and self_transport_confirmed and requested_transport
                    ),
                    "clinical_clearance": "not_assessed",
                },
            }
            return {"source": "safety_floor", "care_tier": tier, "clinic": clinic, "wait_time_min": state.wait_time_min,
                    "reason": state.routing_reason,
                    "travel_estimate_source": state.travel_estimate_source,
                    "route_instructions": state.route_instructions, "route_available": state.route_available,
                    "route_geometry": state.route_geometry,
                    "clinic_latitude": state.clinic_latitude, "clinic_longitude": state.clinic_longitude,
                    "one_map_url": (
                        _one_map_url(state.clinic_latitude, state.clinic_longitude)
                        if state.clinic_latitude is not None and state.clinic_longitude is not None else None
                    ),
                    "routing_plan": state.routing_plan,
                }
        input_issue = self._routing_input_issue(state)
        if input_issue:
            metrics.inc(metrics.ROUTING_FALLBACKS, reason="invalid_input")
            _, clinic, wait = reroute(state)
            state.travel_estimate_source, state.route_instructions, state.route_available, state.route_geometry = "unavailable", [], False, []
            state.clinic_latitude, state.clinic_longitude = None, None
            state.routing_reason = input_issue
            clarification = self._routing_clarification(state, input_issue)
            state.routing_clarification = clarification
            return {
                "source": "fallback", "care_tier": tier, "clinic": clinic, "wait_time_min": wait,
                "reason": state.routing_reason, "travel_estimate_source": state.travel_estimate_source,
                "route_instructions": [], "route_available": False,
                "routing_preference_required": _transport_mode(getattr(state, "transport_mode", None)) is None,
                "routing_clarification": clarification,
            }
        try:
            candidates = await asyncio.to_thread(self._candidates, state)
        except Exception:
            # A directory fault must degrade to the tier fallback below, never
            # abort the pipeline — but it is logged so a persistently broken
            # clinic lookup cannot hide behind the fallback.
            logger.warning("Clinic candidate lookup failed; falling back to tier default", exc_info=True)
            candidates = []
        if not candidates:
            metrics.inc(metrics.ROUTING_FALLBACKS, reason="no_candidates")
            _, clinic, wait = reroute(state)
            state.travel_estimate_source, state.route_instructions, state.route_available, state.route_geometry = "unavailable", [], False, []
            state.clinic_latitude, state.clinic_longitude = None, None
            state.routing_reason = "No eligible nearby GP candidates were available."
            return {"source": "fallback", "care_tier": tier, "clinic": clinic, "wait_time_min": wait,
                    "reason": state.routing_reason,
                    "travel_estimate_source": state.travel_estimate_source, "route_instructions": [], "route_available": False}
        requested_transport = _transport_mode(getattr(state, "transport_mode", None)) or "walk"
        candidates, effective_transport, routing_plan = await asyncio.to_thread(
            self._replan_public_transport_failure, state, candidates, requested_transport,
        )
        selected, decision, source = await self._select(
            state, candidates, effective_transport=effective_transport,
        )
        final_evaluation = self._evaluate_final_selection(state, selected, candidates, tier=tier)
        if source == "llm" and final_evaluation["status"] == "rejected":
            # Keep the LLM trace for audit, but do not publish a selection that
            # no longer meets a factual routing constraint. Candidates are
            # already deterministically ordered by safe logistics score.
            rejected_id = final_evaluation["selected_clinic_id"]
            selected = candidates[0]
            source = "deterministic_fallback"
            decision = {
                "confidence": None,
                "rationale": "Lowest deterministic travel/availability score selected because the proposed LLM routing action failed final verification.",
                "tradeoffs": [],
                "tool_trace": decision.get("tool_trace", []),
                "evaluation_rejected_clinic_id": rejected_id,
            }
            final_evaluation["fallback_applied"] = True
            final_evaluation["fallback_selected_clinic_id"] = selected["clinic_id"]
        if source == "deterministic_fallback":
            metrics.inc(metrics.ROUTING_FALLBACKS, reason="llm_unavailable_or_invalid")
        # [Agentic] The ReAct trace (which tools the model asked for, with what
        # arguments, and what it observed) travels with the plan so the audit
        # trail and the UI can show the agent's actions, not just its answer.
        routing_plan = {
            **routing_plan,
            "final_evaluation": final_evaluation,
            "tool_budget": {
                "llm_tool_call_limit": _MAX_TOOL_TURNS,
                "onemap_call_limit": _MAX_ONEMAP_CALLS_PER_CASE,
                "onemap_calls": self._onemap_calls,
                "onemap_calls_remaining": self._onemap_budget_remaining,
                "onemap_budget_exhausted": self._onemap_budget_exhausted,
            },
            "onemap_circuit": self._onemap_circuit_status(),
            **({"tool_calls": decision["tool_trace"]} if decision.get("tool_trace") else {}),
        }
        state.clinic = selected["name"]
        state.effective_transport_mode = effective_transport
        state.routing_plan = routing_plan
        state.clinic_latitude, state.clinic_longitude = selected["latitude"], selected["longitude"]
        # No queue API is available. Preserve the legacy field as an explicit
        # planning estimate, while returning its provenance so it is not
        # mistaken for a live clinic queue.
        state.wait_time_min = selected["travel_time_min"]
        state.travel_estimate_source = selected["travel_estimate_source"]
        state.route_instructions = list(selected.get("route_instructions", []))
        state.route_available = state.travel_estimate_source == "onemap_route"
        state.route_geometry = list(selected.get("route_geometry", []))
        alternatives = [c["name"] for c in candidates if c["clinic_id"] != selected["clinic_id"]][:2]
        alternative_clinics = [
            self._candidate_summary(c) for c in candidates if c["clinic_id"] != selected["clinic_id"]
        ][:2]
        state.alternative_clinics = alternative_clinics
        return {"source": source, "care_tier": tier, "clinic": state.clinic, "wait_time_min": state.wait_time_min,
                "selected_clinic_id": selected["clinic_id"], "travel_time_min": selected["travel_time_min"],
                "travel_estimate_source": selected["travel_estimate_source"],
                "route_instructions": state.route_instructions, "route_available": state.route_available,
                "route_geometry": state.route_geometry,
                "clinic_latitude": state.clinic_latitude, "clinic_longitude": state.clinic_longitude,
                "one_map_url": _one_map_url(state.clinic_latitude, state.clinic_longitude),
                "requested_transport": requested_transport, "effective_transport": effective_transport,
                "routing_plan": routing_plan,
                "decision_evaluation": final_evaluation,
                "wait_estimate_type": "travel_time_only_no_live_queue", "alternatives": alternatives,
                "alternative_clinics": alternative_clinics,
                "candidate_count": len(candidates),
                "availability_status": selected["availability_status"],
                "hours_freshness": selected["hours_freshness"],
                "accessibility_match": selected["accessibility_match"],
                "language_support_match": selected["language_support_match"],
                "affordability_match": selected["affordability_match"],
                **decision}
