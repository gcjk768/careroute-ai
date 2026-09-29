"""Centralised, typed configuration read from the environment.

[MLOps] Production practice: all tunables and secrets come from the environment
(12-factor config). Secrets — notably OPENAI_API_KEY — are read here only, never
hard-coded and never logged. Copy `.env.example` to `.env` for local development.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path

from dotenv import load_dotenv

# Load local development credentials before reading settings. Deployment secrets
# remain ordinary environment variables, which take precedence (`override=False`).
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPOSITORY_ROOT / ".env")


def _str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _float(key: str, default: str) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _int(key: str, default: str) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _bool(key: str, default: str = "0") -> bool:
    return os.environ.get(key, default).strip().lower() in {"1", "true", "yes", "on"}


def _csv(key: str, default: str) -> list[str]:
    """Comma-separated setting -> list, blanks dropped.

    A variable that is present but blank falls back to `default`: an operator who
    comments a value out (or ships an empty line in a compose file) should get
    the documented default, not an empty list that silently disables the feature.
    """
    raw = os.environ.get(key, "").strip() or default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _trusted_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse a comma-separated list of IPs/CIDRs into networks.

    Unparseable entries are dropped rather than raising: a typo in one proxy
    address must not stop the whole app from booting, and dropping an entry
    fails CLOSED (that peer simply is not trusted).
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return networks


# --------------------------------------------------------------------------
# [AI-Security] Runtime safety controls
# --------------------------------------------------------------------------
# LLM10 Unbounded Consumption: cap triage requests per client per minute.
RATE_LIMIT_PER_MIN: int = _int("CAREROUTE_RATE_LIMIT_PER_MIN", "30")
# [AI-Security] Abuse monitor: attack-category guardrail blocks per client that
# quarantine it (0 disables), the sliding window they are counted in, and how
# long the quarantine lasts.
ABUSE_BLOCK_THRESHOLD: int = _int("CAREROUTE_ABUSE_BLOCK_THRESHOLD", "5")
ABUSE_WINDOW_S: float = _float("CAREROUTE_ABUSE_WINDOW_S", "600")
ABUSE_COOLDOWN_S: float = _float("CAREROUTE_ABUSE_COOLDOWN_S", "900")

# [AI-Security] LLM02 "inference-time access control" / AIC Day 3 RBAC.
# Shared bearer key for the STAFF surfaces — the escalation queue, the decision
# POST, per-case audit trails and session history. When set, those endpoints
# require `X-Staff-Key: <value>` and answer 401 otherwise. When EMPTY the
# endpoints stay open and /api/health reports `staffAuth: "open"`, so a demo
# still runs without configuration but the state is never silent. Must be set
# before any deployment reachable beyond localhost (Leftover Jobs A3). A real
# clinician identity needs an IdP; this is the smallest honest step.
STAFF_API_KEY: str = _str("CAREROUTE_STAFF_API_KEY", "").strip()

# ASI10 Rogue Agents: a global kill switch. When engaged, the LLM providers are
# short-circuited so the pipeline runs on its deterministic, auditable rules
# only (no autonomous model reasoning) — a fast "revoke agency" control.
KILL_SWITCH: bool = _bool("CAREROUTE_KILL_SWITCH")

# ASI08 Cascading Failures: a circuit breaker on the LLM provider chain. Five
# workers call `llm.complete()` per triage, so a provider that is down is paid
# for FIVE times over — its full timeout each, in series — and one dead upstream
# turns into a multiplied latency failure across every case in flight. That
# amplification, not the original fault, is what ASI08 is about.
#
# After this many consecutive failures a provider is skipped outright until the
# cooldown expires, at which point one trial call decides whether it reopens.
LLM_BREAKER_THRESHOLD: int = _int("CAREROUTE_LLM_BREAKER_THRESHOLD", "3")
LLM_BREAKER_COOLDOWN_SECONDS: float = _float("CAREROUTE_LLM_BREAKER_COOLDOWN", "30")

# SSE transport keepalive. The frontend reaches the triage stream through the
# Next.js /api rewrite, whose http-proxy aborts an upstream that is silent for
# 30 s (its default `proxyTimeout`) WITHOUT ending the browser response, so the
# UI hangs on "Triaging..." with no error. Care Routing's OneMap shortlist and
# the red-flag path's LLM calls both go quiet for 24-30 s. While the
# orchestrator is silent we emit an SSE comment frame every N seconds; comment
# frames carry no `data:` line so clients ignore them, but they reset every idle
# timer between here and the browser. Measured 2026-09-02 through a Next
# rewrite in Chrome: 26 s silent OK, 32 s silent stalls, 40 s with a 10 s
# heartbeat OK. 10 s gives a 3x margin under the 30 s default. 0 disables.
SSE_HEARTBEAT_SECONDS: float = _float("CAREROUTE_SSE_HEARTBEAT_SECONDS", "10")

# LLM10 again: which peers may set X-Forwarded-For on our behalf. `X-Forwarded-For`
# is an ordinary request header, so believing it unconditionally hands every
# caller a fresh rate-limit bucket per request — the limiter is then bypassed by
# adding one header. Only a peer listed here (IP or CIDR) has its XFF honoured.
# Default empty = trust nobody, which is correct for a container reached
# directly; set it to your ingress/load-balancer addresses when there IS one.
TRUSTED_PROXIES: list = _trusted_networks(_str("CAREROUTE_TRUSTED_PROXIES", ""))

# Browser origins allowed to call this API. Kept in config rather than in
# main.py because it is the one CORS value that changes per deployment, and
# `allow_credentials=True` means a careless edit to "*" would be a real
# vulnerability — an operator should be able to set it without touching code.
_DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://localhost:3000"
CORS_ORIGINS: list[str] = _csv("CAREROUTE_CORS_ORIGINS", _DEFAULT_CORS_ORIGINS)


# --------------------------------------------------------------------------
# LLM provider chain
# --------------------------------------------------------------------------
# Order in which LLM providers are attempted. The first one that is available
# and returns a usable answer wins; if every provider fails, the agents fall
# back to deterministic rule-based logic (see agents.py). Comma-separated.
#   default: the hosted OpenAI API only (local-tool providers were removed)
LLM_PROVIDER_ORDER: list[str] = [
    p.strip() for p in _str("LLM_PROVIDER_ORDER", "openai").split(",") if p.strip()
]

# --- OpenAI / ChatGPT API (the only provider) -------------------------------------
# Enabled only when OPENAI_API_KEY is set. Uses the Chat Completions REST API;
# callers may request strict JSON-schema output for bounded decisions.
OPENAI_API_KEY: str = _str("OPENAI_API_KEY", "")
OPENAI_BASE_URL: str = _str("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL: str = _str("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT: float = _float("OPENAI_TIMEOUT", "30")
# Sent only to reasoning models (gpt-5*, o-series): "minimal" | "low" | "medium" | "high".
# Empty = the model's own default. "low" (the default here) keeps GPT-5 latency near gpt-4o-mini's.
OPENAI_REASONING_EFFORT: str = _str("OPENAI_REASONING_EFFORT", "low")

# [MLOps] Optional per-case LLM tracing (app/tracing.py). Each sink is off
# unless its keys are set. Langfuse is self-hostable (`docker compose --profile
# tracing up`); LangSmith is hosted, so prompts (already PII-masked) leave the stack.
LANGFUSE_PUBLIC_KEY: str = _str("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY: str = _str("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST: str = _str("LANGFUSE_HOST", "http://localhost:3000")
LANGSMITH_API_KEY: str = _str("LANGSMITH_API_KEY", "")
LANGSMITH_ENDPOINT: str = _str("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
LANGSMITH_PROJECT: str = _str("LANGSMITH_PROJECT", "careroute")


# [AI-Security] LLM02 escape hatch. llm.py never puts prompt or response CONTENT
# into an exception or a log line, because those cross into log aggregation —
# a different trust boundary from the LLM provider, and one where PHI must not
# land. Set to 1 ONLY on a local machine with synthetic data when you need to
# see what a provider actually returned. Never enable it in production.
DEBUG_LLM: bool = _bool("CAREROUTE_DEBUG_LLM")


# --------------------------------------------------------------------------
# Safety NLP shadow pipeline
# --------------------------------------------------------------------------
# Safety NLP remains shadow-only by default. Phase 6 may activate accepted
# findings only through the separate educational-prototype flag below. These
# switches never weaken the deterministic floor, and release containers use
# local artifacts rather than downloading models at request time.
SAFETY_NLP_ENABLED: bool = _bool("CAREROUTE_SAFETY_NLP")
SAFETY_NLP_NLLB_ENABLED: bool = _bool("CAREROUTE_SAFETY_NLP_TRANSLATION_NLLB")
SAFETY_NLP_MADLAD_ENABLED: bool = _bool("CAREROUTE_SAFETY_NLP_TRANSLATION_MADLAD")
SAFETY_NLP_DIRECT_NLI_ENABLED: bool = _bool("CAREROUTE_SAFETY_NLP_DIRECT_NLI")
SAFETY_NLP_MODEL_MANIFEST: str = _str("CAREROUTE_SAFETY_MODEL_MANIFEST", "models/safety/manifest.json")
SAFETY_NLP_MODEL_CACHE: str = _str("CAREROUTE_SAFETY_MODEL_CACHE", "models/safety/cache")
SAFETY_NLP_DEVICE: str = _str("CAREROUTE_SAFETY_NLP_DEVICE", "cpu")
SAFETY_NLP_TIMEOUT_MS: int = _int("CAREROUTE_SAFETY_NLP_TIMEOUT_MS", "1000")
SAFETY_NLP_MAX_INPUT_CHARS: int = _int("CAREROUTE_SAFETY_NLP_MAX_INPUT_CHARS", "2000")
SAFETY_PROTOTYPE_SEMANTIC_ACTIVATION: bool = _bool("CAREROUTE_SAFETY_PROTOTYPE_SEMANTIC_ACTIVATION")
# Phase 6 educational-prototype activation thresholds. These are separate
# from model-internal candidate thresholds and only govern intervention.
SAFETY_NLP_ACTIVATION_THRESHOLD: float = _float("CAREROUTE_SAFETY_NLP_ACTIVATION_THRESHOLD", "0.85")
SAFETY_LLM_ACTIVATION_THRESHOLD: float = _float("CAREROUTE_SAFETY_LLM_ACTIVATION_THRESHOLD", "0.85")
SAFETY_SEMANTIC_REVIEW_THRESHOLD: float = _float("CAREROUTE_SAFETY_SEMANTIC_REVIEW_THRESHOLD", "0.60")
# Local integration harness only. It invokes the bounded LLM through the real
# API/orchestrator path, but cannot change the triage outcome.
SAFETY_NLP_TEST_FORCE_UNCERTAINTY: bool = _bool("CAREROUTE_SAFETY_NLP_TEST_FORCE_UNCERTAINTY")

# --------------------------------------------------------------------------
# Safety bounded LLM adjudication
# --------------------------------------------------------------------------
# Phase 5 keeps adjudication independent from additive activation. The LLM may
# classify uncertain semantic cases only when enabled here; Phase 6's prototype
# activation flag still controls whether accepted semantic positives can change
# acuity.
SAFETY_LLM_ENABLED: bool = _bool("CAREROUTE_SAFETY_LLM")
SAFETY_LLM_PROVIDER_ORDER: list[str] = [
    p.strip() for p in _str("CAREROUTE_SAFETY_LLM_PROVIDER_ORDER", "openai").split(",") if p.strip()
]
SAFETY_LLM_MODEL: str = _str("CAREROUTE_SAFETY_LLM_MODEL", OPENAI_MODEL)
SAFETY_LLM_TIMEOUT_MS: int = _int("CAREROUTE_SAFETY_LLM_TIMEOUT_MS", "5000")

# --------------------------------------------------------------------------
# [Microservices] One agent, one container
# (docs/design/specs/2026-09-19-agent-microservices-design.md)
# --------------------------------------------------------------------------
# "inprocess" (default): every worker runs inside this process, exactly as
# before. "http": the intake-gateway calls each worker's container.
AGENT_TRANSPORT: str = _str("AGENT_TRANSPORT", "inprocess").strip().lower()
# Base URL of each agent container, e.g. CAREROUTE_AGENT_URL_SAFETY. The
# defaults are the loopback ports the containers share inside one ECS task.
AGENT_URLS: dict[str, str] = {
    slug: _str(f"CAREROUTE_AGENT_URL_{slug.upper()}", f"http://localhost:{port}").rstrip("/")
    for slug, port in (("classifier", 8101), ("safety", 8102), ("routing", 8103),
                       ("reflection", 8104), ("hitl", 8105), ("handoff", 8106))
}
# Longer than one LLM call: an agent may make one (via llm-gateway) per request.
AGENT_TIMEOUT_SECONDS: float = _float("CAREROUTE_AGENT_TIMEOUT", "90")
# Shared secret on every gateway -> service call. Empty disables the check
# (local development); set it in every container in production.
INTERNAL_TOKEN: str = _str("CAREROUTE_INTERNAL_TOKEN", "").strip()
# When set, llm.complete() / rag.retrieve_detailed() call the shared services
# instead of running in-process. Never set them ON those services themselves.
LLM_GATEWAY_URL: str = _str("CAREROUTE_LLM_GATEWAY_URL", "").strip().rstrip("/")
RAG_SERVICE_URL: str = _str("CAREROUTE_RAG_SERVICE_URL", "").strip().rstrip("/")
# Durable backing for the escalation queue, case store and audit trail.
REDIS_URL: str = _str("CAREROUTE_REDIS_URL", "").strip()
