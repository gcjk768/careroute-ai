"""LLM provider chain: the hosted OpenAI API, behind a per-provider breaker.

[Agentic][AI-Security] Design goals (production):
- Never let an LLM failure take down the app. `complete()` raises
  LLMUnavailableError only when EVERY configured provider fails; the workers in
  agents.py catch it and fall back to deterministic rule-based logic, so the
  pipeline always produces a sensible, safe result.
- Provider order is configurable via LLM_PROVIDER_ORDER (default "openai").
  Each provider is a small class implementing the same interface, so adding
  another hosted API (for scale) is one new class. Local-tool providers (a
  developer's CLI, a localhost model server) are deliberately NOT in the
  registry: see _PROVIDERS below.
- Secrets (OPENAI_API_KEY) are read from the environment only and never logged.
  Prompt/response CONTENT is never logged either — this is healthcare data, so
  only provider names and outcomes are emitted to the structured log.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass

import httpx

from . import config, correlation, guardrail, llm_cost, metrics, redact, tracing

logger = logging.getLogger("careroute.llm")



class LLMUnavailableError(RuntimeError):
    """Raised when a provider cannot be reached or returns something unusable."""


# [Microservices] Tests route the gateway call through this (httpx.MockTransport).
_GATEWAY_TRANSPORT: httpx.AsyncBaseTransport | None = None


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def _extract_json(text: str) -> dict | None:
    """Defensively pull a JSON object out of a model response.

    Models often wrap JSON in markdown fences or add chatter around it. Try a
    direct parse, then a fenced block, then the first balanced-looking object.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _excerpt(text: str) -> str:
    """A debug-only suffix carrying the first 160 chars of a provider payload.

    [AI-Security] LLM02: exception messages end up in logs, and logs are a
    different trust boundary from the LLM provider — PHI that redact.py masked
    on the way OUT must not reappear there on the way BACK. So the excerpt is
    empty unless an operator has explicitly set CAREROUTE_DEBUG_LLM=1 on a
    machine handling synthetic data.
    """
    if not config.DEBUG_LLM:
        return ""
    return f" excerpt={(text or '')[:160]!r}"


def _safe_detail(exc: BaseException) -> str:
    """A loggable description of a provider failure, with no payload in it.

    LLMUnavailableError messages are content-free by construction (every raise
    site in this module goes through `_excerpt`). Anything else is arbitrary —
    notably json.JSONDecodeError, whose own message quotes a slice of the
    document it failed on — so only the exception TYPE is reported.
    """
    if isinstance(exc, LLMUnavailableError):
        return str(exc)
    return type(exc).__name__


def _compose_prompt(system: str, prompt: str, json_mode: bool) -> str:
    """Fold the system instruction and JSON directive into a single prompt.

    Kept for providers that take one prompt string rather than a message list
    rather than a separate system-prompt channel.
    """
    parts: list[str] = []
    if system:
        parts.append(system.strip())
    parts.append(prompt.strip())
    if json_mode:
        parts.append("Respond with STRICT JSON only — no markdown, no code fences, no commentary.")
    return "\n\n".join(parts)


def _finalise(text: str, json_mode: bool) -> str:
    """Validate a raw provider response; return text, or a normalised JSON string."""
    text = (text or "").strip()
    if not text:
        raise LLMUnavailableError("provider returned an empty response")
    if json_mode:
        parsed = _extract_json(text)
        if parsed is None:
            # Report the SIZE of the unusable response, never its content: this
            # string is logged by `complete()` and re-raised to the caller.
            raise LLMUnavailableError(
                f"outcome=unparseable_json response_chars={len(text)}{_excerpt(text)}"
            )
        return json.dumps(parsed)
    return text


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------
# [LLM] GPT-5 compatibility. Some models accept only the default sampling parameters
# (gpt-5-mini and gpt-5.5 answer 400 to temperature 0.2; gpt-5.4-mini accepts it —
# measured 2026-09-27), and only reasoning models take `reasoning_effort`. Rather than
# a hard-coded model list that goes stale, a 400 naming an unsupported parameter drops
# it, is remembered for that model, and the call is retried once.
_UNSUPPORTED_PARAMS: dict[str, set[str]] = {}
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _unsupported_param(body_text: str) -> str | None:
    """The parameter a 400 rejects, if it is one we may safely drop."""
    try:
        err = json.loads(body_text).get("error") or {}
    except (ValueError, AttributeError):
        return None
    param, msg = err.get("param"), str(err.get("message") or "")
    if param in ("temperature", "reasoning_effort") and ("nsupported" in msg or "not supported" in msg):
        return param
    return None


class OpenAIProvider:
    """Secondary provider — the OpenAI (ChatGPT) Chat Completions REST API.

    Active only when OPENAI_API_KEY is configured.
    """

    name = "openai"
    # Records the provider's real `usage` block itself (below), so complete()
    # does not add a character-count estimate on top.
    meters_usage = True

    def label(self) -> str:
        return f"openai:{config.OPENAI_MODEL}"

    async def available(self) -> bool:
        return bool(config.OPENAI_API_KEY)

    async def complete(
        self, system: str, prompt: str, json_mode: bool, json_schema: dict | None = None,
        model: str | None = None, timeout_override: float | None = None,
    ) -> str:
        if not config.OPENAI_API_KEY:
            raise LLMUnavailableError("OPENAI_API_KEY is not set")
        chosen = model or config.OPENAI_MODEL
        body: dict = {
            "model": chosen,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if json_schema:
            # Structured Outputs constrains OpenAI's response to the schema.
            # The workers still validate every value before it affects state.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "care_route_decision", "strict": True, "schema": json_schema},
            }
        elif json_mode:
            body["response_format"] = {"type": "json_object"}
        if config.OPENAI_REASONING_EFFORT and chosen.startswith(_REASONING_PREFIXES):
            body["reasoning_effort"] = config.OPENAI_REASONING_EFFORT
        for param in _UNSUPPORTED_PARAMS.get(chosen, ()):
            body.pop(param, None)
        headers = {
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",  # never logged
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(connect=5.0, read=timeout_override or config.OPENAI_TIMEOUT, write=10.0, pool=5.0)
        for _attempt in range(3):   # at most two parameters to drop
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{config.OPENAI_BASE_URL}/chat/completions", json=body, headers=headers
                    )
            except httpx.HTTPError as exc:
                raise LLMUnavailableError(f"openai connection error: {exc}") from exc
            dropped = _unsupported_param(resp.text or "") if resp.status_code == 400 else None
            if dropped is None or dropped not in body:
                break
            _UNSUPPORTED_PARAMS.setdefault(chosen, set()).add(dropped)
            body.pop(dropped)
            logger.info("openai model=%s does not accept %s; retrying without it", chosen, dropped)
        if resp.status_code != 200:
            # An OpenAI error body frequently quotes the offending request back
            # (that is the whole point of a 400), so it must not be re-raised.
            body = resp.text or ""
            raise LLMUnavailableError(
                f"outcome=http_{resp.status_code} body_chars={len(body)}{_excerpt(body)}"
            )
        try:
            payload = resp.json()
            message = payload["choices"][0]["message"]
            if message.get("refusal"):
                raise LLMUnavailableError("openai refused the structured request")
            content = message["content"]
            # [MLOps] Token/cost accounting. The `usage` block was previously
            # parsed away with the rest of the envelope, leaving the per-triage
            # token cost unmeasured. Never let accounting fail a triage.

            with contextlib.suppress(Exception):
                llm_cost.record(chosen, payload.get("usage"))
        except (KeyError, IndexError, ValueError) as exc:
            # json.JSONDecodeError (a ValueError) embeds part of the document in
            # its own message, so report the type only.
            raise LLMUnavailableError(f"outcome=malformed_response ({type(exc).__name__})") from exc
        return _finalise(content, json_mode)


# The hosted OpenAI API is the ONLY provider. The local CLI provider (a
# subprocess on a developer laptop) and the Ollama provider (a localhost server)
# were removed 2026-09-23: neither exists in the deployed service, and a chain
# that can resolve to a dev machine's tool is not a production chain.
# tests/test_llm_providers.py pins this.
_PROVIDERS: dict = {p.name: p for p in (OpenAIProvider(),)}


def _ordered(provider_order: list[str] | None = None) -> list:
    """Providers to try, in the configured order (unknown names ignored)."""
    order = provider_order if provider_order is not None else config.LLM_PROVIDER_ORDER
    return [_PROVIDERS[n] for n in order if n in _PROVIDERS]


# --------------------------------------------------------------------------
# [AI-Security][ASI08] Circuit breaker over the provider chain
# --------------------------------------------------------------------------
class _Breaker:
    """One provider's failure state: closed (normal), open (skipped), half-open.

    OWASP Agentic **ASI08, Cascading Failures** is about *amplification*, not the
    root fault. Five workers call `complete()` per triage; without a breaker a
    single dead provider is waited on five times per case — its whole timeout
    each — and every concurrent case pays the same tax. The breaker converts a
    repeated, already-known failure into an instant skip to the next provider (or
    to the deterministic fallback), so one upstream outage cannot multiply into a
    system-wide latency collapse.

    Deliberately NOT a rate limiter: it counts *consecutive* failures and any
    success resets it, so a flaky-but-usable provider is never locked out.
    """

    __slots__ = ("failures", "opened_at")

    def __init__(self) -> None:
        self.failures = 0
        self.opened_at: float | None = None

    def is_open(self, now: float) -> bool:
        """True while the provider should be skipped without being called."""
        if self.opened_at is None:
            return False
        if now - self.opened_at >= config.LLM_BREAKER_COOLDOWN_SECONDS:
            # Half-open: let exactly one trial call through. It either succeeds
            # (record_success closes the breaker) or fails (record_failure
            # re-opens it for another cooldown).
            self.opened_at = None
            self.failures = config.LLM_BREAKER_THRESHOLD - 1
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self, now: float) -> bool:
        """Count a failure; return True if this one tripped the breaker."""
        self.failures += 1
        if self.failures >= config.LLM_BREAKER_THRESHOLD and self.opened_at is None:
            self.opened_at = now
            return True
        return False


_BREAKERS: dict[str, _Breaker] = {}


def _breaker(name: str) -> _Breaker:
    return _BREAKERS.setdefault(name, _Breaker())


def breaker_state() -> dict[str, dict]:
    """Per-provider breaker state, for /api/health and tests."""
    now = time.monotonic()
    return {
        name: {
            "open": b.opened_at is not None and now - b.opened_at < config.LLM_BREAKER_COOLDOWN_SECONDS,
            "consecutiveFailures": b.failures,
        }
        for name, b in _BREAKERS.items()
    }


def reset_breakers() -> None:
    """Clear all breaker state. For tests and for an operator forcing a retry."""
    _BREAKERS.clear()


# --------------------------------------------------------------------------
# Public API (unchanged signature — agents.py imports `complete`)
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# [Agentic][MLOps] LLM ROUTER — task -> model tier -> per-provider model
#
# ArchAAS Day 3 (AM): a router chooses the model for the TASK, not one model for
# everything. Normalising a sentence or drafting a handoff summary does not need
# the strongest model; classifying acuity and critiquing an assembled decision
# do. Each task names a TIER; each provider maps a tier to a model through the
# environment, defaulting to what the provider used before the router existed:
#
#   OPENAI_MODEL_FAST  (default OPENAI_MODEL)       OPENAI_MODEL_DEEP  (default OPENAI_MODEL)
#
# A call that names no task passes NO model argument, so every existing caller
# (and every provider fake in the test suite) behaves exactly as before. Agents
# opt in with `task=`; the platform-owned callers do so today.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Route:
    tier: str        # "fast" | "deep"
    cacheable: bool  # exact-match response cache allowed for this task
    why: str


ROUTES: dict[str, Route] = {
    "intake.normalise": Route("fast", True,
        "pure text normalisation; identical input should normalise identically"),
    "classifier.classify": Route("deep", True,
        "acuity from symptoms, demographics and retrieved guidance; an identical prompt deserves "
        "an identical answer, which the cache makes true rather than probable"),
    "safety.semantic": Route("deep", False,
        "safety-critical red-flag detection: always asked fresh, never served from memory"),
    "classifier.question": Route("fast", True,
        "one short clarifying question for the interview; wording, not judgement, so the cheap "
        "model suffices, and an identical case + transcript should get the identical question"),
    "patient.translate": Route("fast", True,
        "translate the English question/rationale into the patient's language (app/patient_language.py); "
        "wording, not judgement, and the same text should translate the same way"),
    "routing.select": Route("fast", False,
        "depends on live clinic hours and travel observations that change between calls"),
    "handoff.summary": Route("fast", False,
        "short grounded summary; the case packet differs almost every time"),
    "reflection.critic": Route("deep", False,
        "multi-turn tool loop whose prompt includes tool observations; never cached"),
    "security.brief": Route("deep", False,
        "defender-side triage of scanner findings (app/security_brief.py); runs in CI, not per case"),
    "eval.grounding": Route("deep", True,
        "LLM-as-judge over (context, answer) pairs (app/evals/grounding.py): deep because judging "
        "faithfulness is the harder task, cacheable because the same pair must score the same — a "
        "judge that disagrees with itself cannot grade anything"),
}

# Model per tier when OPENAI_MODEL_<TIER> is unset. Chosen by a head-to-head on the 100
# soak scenarios (scripts/model_soak.py, 2026-09-27) against gpt-4o-mini / gpt-4.1-mini /
# gpt-4.1: both passed 100/100 (every red flag P1/P2 on turn 1), and GPT-5 escalated 10
# routine complaints instead of 17 (a mouth ulcer P3, not P2), with 27% fewer LLM calls,
# at ~1.3 s more per case. Env overrides per deployment.
_TIER_DEFAULTS: dict[str, dict[str, str]] = {
    "openai": {"fast": "gpt-5.4-mini", "deep": "gpt-5.4", "max": "gpt-5.5"},
}
_PROVIDER_ENV = {"openai": "OPENAI_MODEL"}


def route_table() -> dict[str, dict]:
    """The router's decisions, for docs, tests and /api/health-style inspection."""
    return {task: {"tier": r.tier, "cacheable": r.cacheable, "why": r.why} for task, r in ROUTES.items()}


def _model_for(provider_name: str, tier: str) -> str | None:
    base_key = _PROVIDER_ENV.get(provider_name)
    if base_key is None:
        return None
    configured = os.environ.get(f"{base_key}_{tier.upper()}", "").strip()
    if configured:
        return configured
    default = _TIER_DEFAULTS.get(provider_name, {}).get(tier)
    if default:
        return default
    if tier == "max":
        # No max model anywhere: an escalation past deep costs nothing extra — max IS deep.
        return _model_for(provider_name, "deep")
    return getattr(config, base_key, None) or None


def tier_models(active: str | None) -> dict[str, str]:
    """Model per tier for the active provider ("openai:gpt-4o-mini" -> openai), for
    /api/health: its `model` field is only the base model, which no routed call uses."""
    if not active:
        return {}
    name = active.split(":", 1)[0]
    return {t: m for t in ("fast", "deep", "max") if (m := _model_for(name, t))}


def _metric_model(provider, model: str | None) -> str:
    """The `model` label for the latency histogram: the same model name token
    accounting records (llm_cost.record gets the resolved model), so latency and
    cost series for one model join in PromQL. Falling back to the provider name
    labelled an un-routed OpenAI call `model="openai"` while its tokens were
    `model="gpt-4o-mini"` — two names for one model on every dashboard."""
    return model or provider.label().split(":", 1)[-1]


# --------------------------------------------------------------------------
# [Agentic][MLOps] DIFFICULTY-BASED ESCALATION (lecture: "route to a small or a
# large model by difficulty"). The task picks the BASE tier; the caller may add
# difficulty signals it already has -- its own low confidence, thin retrieved
# evidence, a Reflection re-run -- and any one of them moves the call ONE tier up
# the ladder fast -> deep -> max. OPENAI_MODEL_MAX defaults to the deep model and
# FAST/DEEP default to OPENAI_MODEL, so with nothing configured the model and the
# bill are unchanged; an operator buys the stronger model for hard cases only, by
# setting OPENAI_MODEL_MAX (e.g. gpt-4o) and/or OPENAI_MODEL_DEEP.
# --------------------------------------------------------------------------
_ESCALATE = {"fast": "deep", "deep": "max", "max": "max"}


def choose_tier(base_tier: str, difficulty: dict | None, *,
                confidence_floor: float | None = None, min_evidence: int | None = None) -> tuple[str, str]:
    """Pure routing decision: (tier, reason). reason is "base" when not escalated.

    difficulty keys (all optional): confidence (0-1, the caller's own), evidence
    (count of supporting passages / findings), rerun (bool, a Reflection re-run).
    Thresholds default to CAREROUTE_LLM_ESCALATE_CONFIDENCE (0.6) and
    CAREROUTE_LLM_ESCALATE_MIN_EVIDENCE (1).
    """
    if not difficulty:
        return base_tier, "base"
    floor = confidence_floor if confidence_floor is not None else config._float(
        "CAREROUTE_LLM_ESCALATE_CONFIDENCE", "0.6")
    need = min_evidence if min_evidence is not None else config._int("CAREROUTE_LLM_ESCALATE_MIN_EVIDENCE", "1")
    confidence, evidence = difficulty.get("confidence"), difficulty.get("evidence")
    if difficulty.get("rerun"):
        reason = "rerun"
    elif confidence is not None and float(confidence) < floor:
        reason = "low_confidence"
    elif evidence is not None and int(evidence) < need:
        reason = "thin_evidence"
    else:
        return base_tier, "base"
    return _ESCALATE.get(base_tier, base_tier), reason


# [MLOps] Per-request log of LLM calls — task, tier, why, model, latency — for the
# result card's "AI models used" panel. A request opens it (begin_call_log) and every
# call made while serving it, from any agent, appends one entry; outside a request
# nothing is recorded. With agents over HTTP (AGENT_TRANSPORT=http) each agent
# container opens its own log per invoke and returns it; RemoteAgent merges it here.
_CALL_LOG: ContextVar[list | None] = ContextVar("careroute_llm_calls", default=None)


def begin_call_log() -> list:
    """Start recording this request's LLM calls; returns the (shared, mutable) list."""
    log: list = []
    _CALL_LOG.set(log)
    return log


_CALL_KEYS = ("task", "tier", "reason", "model", "cached", "ms")


def merge_call_log(entries: object) -> None:
    """Append calls an agent container reported. The reply came over the network, so
    only the known keys with scalar values are kept, and at most 50 entries."""
    log = _CALL_LOG.get()
    if log is None or not isinstance(entries, list):
        return
    for e in entries[:50]:
        if isinstance(e, dict):
            log.append({k: e.get(k) for k in _CALL_KEYS
                        if isinstance(e.get(k), (str, int, float, bool, type(None)))})


def _log_call(task: str | None, tier: str | None, reason: str, model: str | None,
              started: float, cached: bool = False) -> None:
    log = _CALL_LOG.get()
    if log is not None:
        log.append({"task": task or "untasked",
                    "tier": tier or ("pinned" if reason == "override" else "default"),
                    "reason": reason, "model": model, "cached": cached,
                    "ms": round((time.monotonic() - started) * 1000)})


def _record_route(task: str, tier: str) -> None:
    try:
        from . import metrics

        metrics.inc(metrics.LLM_ROUTER_DECISIONS, task=task, tier=tier)
    except Exception:
        logger.debug("router metric not recorded", exc_info=True)


# --------------------------------------------------------------------------
# [Agentic][MLOps] Response CACHE -- exact-match first, optional SEMANTIC tier
#
# Exact tier: keyed on SHA-256 of (task, tier, system, prompt, json_mode, schema).
#
# Semantic tier (CAREROUTE_LLM_SEMANTIC_CACHE_THRESHOLD, default 0 = OFF): a
# miss on the exact key may still be served by a stored answer whose prompt
# embedding (rag_embed -- the embedder dense retrieval already uses) has cosine
# >= the threshold, within the SAME namespace (task, tier, system, json_mode,
# schema -- only the prompt may differ) and with the SAME critical tokens (every
# number and every negation word). OFF by default because in clinical decision
# support a near-miss hit is a wrong answer delivered confidently: "chest pain
# for an hour" and "chest pain for a day" are similar. The critical-token guard
# stops the worst of that (a changed dose, a flipped "no"), not all of it, so
# turning it on is a clinical-safety decision, not a performance tweak. With no
# embedder installed it stays exact-only.
#
# Only tasks marked cacheable in ROUTES reach either tier -- never
# safety.semantic, routing.select, handoff.summary or reflection.critic.
#
# Held in process memory only, TTL-bounded (CAREROUTE_LLM_CACHE_TTL_S, default
# 300 s; 0 disables) and size-bounded (CAREROUTE_LLM_CACHE_MAX, default 256,
# least-recently-used evicted). Prompts are redacted before the key is built.
# Failures and guard-flagged outputs are never cached, and the kill switch is
# checked before the cache is consulted.
# --------------------------------------------------------------------------
@dataclass
class _Entry:
    expires: float
    value: str
    namespace: str
    critical: frozenset
    vector: object = None  # np.ndarray | None -- L2-normalised prompt embedding


_CACHE: OrderedDict[str, _Entry] = OrderedDict()
_CACHE_STATS = {"hits": 0, "misses": 0, "semanticHits": 0}
_NEGATIONS = frozenset({"no", "not", "never", "without", "denies", "deny", "denied", "none", "nor", "cannot"})
_TOKEN = re.compile(r"[a-z]+(?:n't)?|\d+(?:\.\d+)?")


def _cache_ttl() -> float:
    return config._float("CAREROUTE_LLM_CACHE_TTL_S", "300")


def _cache_max() -> int:
    return config._int("CAREROUTE_LLM_CACHE_MAX", "256")


def _semantic_threshold() -> float:
    """0 (default) disables the semantic tier; so does anything above 1."""
    value = config._float("CAREROUTE_LLM_SEMANTIC_CACHE_THRESHOLD", "0")
    return value if 0 < value <= 1 else 0.0


def reset_cache() -> None:
    _CACHE.clear()
    _CACHE_STATS.update(hits=0, misses=0, semanticHits=0)


def cache_stats() -> dict:
    return {**_CACHE_STATS, "size": len(_CACHE), "ttlSeconds": _cache_ttl(), "maxEntries": _cache_max(),
            "semanticThreshold": _semantic_threshold()}


def _cache_key(task: str, tier: str, system: str, prompt: str, json_mode: bool, json_schema: dict | None) -> str:
    material = json.dumps([task, tier, system, prompt, bool(json_mode), json_schema], sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _critical_tokens(prompt: str) -> frozenset:
    """Numbers and negations: the words a semantic match must never paper over."""
    return frozenset(t for t in _TOKEN.findall(prompt.lower())
                     if t[0].isdigit() or t in _NEGATIONS or t.endswith("n't"))


def _embed(prompt: str):
    """The prompt's normalised embedding, or None (no embedder, or it failed)."""
    try:
        from . import rag_embed

        embedder = rag_embed.get_embedder()
        return None if embedder is None else embedder.embed_queries([prompt])[0]
    except Exception:  # noqa: BLE001 - the semantic tier is an optimisation; exact-match is the fallback
        logger.debug("semantic cache embedding unavailable", exc_info=False)
        return None


class _Lookup:
    """One call's cache context: the exact key plus what the semantic tier needs."""

    __slots__ = ("critical", "key", "namespace", "task", "vector")

    def __init__(self, task, tier, system, prompt, json_mode, json_schema):
        self.task = task
        self.key = _cache_key(task, tier, system, prompt, json_mode, json_schema)
        self.namespace = _cache_key(task, tier, system, "", json_mode, json_schema)
        self.critical = _critical_tokens(prompt)
        self.vector = _embed(prompt) if _semantic_threshold() > 0 else None


def _cache_get(lookup: _Lookup) -> str | None:
    now = time.monotonic()
    entry = _CACHE.get(lookup.key)
    if entry is not None and entry.expires > now:
        _CACHE.move_to_end(lookup.key)
        _CACHE_STATS["hits"] += 1
        _count_cache(lookup.task, "hit", "exact")
        return entry.value
    if entry is not None:
        _CACHE.pop(lookup.key, None)
    kind = "exact"
    if lookup.vector is not None:
        kind = "semantic"
        # ponytail: linear scan over <= CAREROUTE_LLM_CACHE_MAX entries (256 dot
        # products, microseconds); an ANN index only if the cache grows 100x.
        best_key, best = None, _semantic_threshold()
        for key, cand in _CACHE.items():
            if (cand.vector is None or cand.expires <= now or cand.namespace != lookup.namespace
                    or cand.critical != lookup.critical):
                continue
            score = float(cand.vector @ lookup.vector)
            if score >= best:
                best_key, best = key, score
        if best_key is not None:
            _CACHE.move_to_end(best_key)
            _CACHE_STATS["hits"] += 1
            _CACHE_STATS["semanticHits"] += 1
            _count_cache(lookup.task, "hit", "semantic")
            return _CACHE[best_key].value
    _CACHE_STATS["misses"] += 1
    _count_cache(lookup.task, "miss", kind)
    return None


def _cache_put(lookup: _Lookup, value: str) -> None:
    ttl = _cache_ttl()
    if ttl <= 0:
        return
    _CACHE[lookup.key] = _Entry(time.monotonic() + ttl, value, lookup.namespace, lookup.critical, lookup.vector)
    _CACHE.move_to_end(lookup.key)
    while len(_CACHE) > max(1, _cache_max()):
        _CACHE.popitem(last=False)


def _count_cache(task: str, outcome: str, kind: str = "exact") -> None:
    with contextlib.suppress(Exception):
        metrics.inc(metrics.LLM_CACHE, task=task, outcome=outcome, kind=kind)


# --------------------------------------------------------------------------
# [AI-Security] PER-CALL GUARDRAILS. `guardrail.screen` runs once, on the
# patient's words, at the edge. A prompt is assembled from more than that --
# retrieved guidance, remembered visits, tool observations, a critic's hint --
# and none of those passed the edge screen. So EVERY call is screened here, on
# the side that actually calls a provider (llm-gateway when split):
#   input   PII redaction (redact.py, always) + injection screen
#           (guardrail.screen_llm_input)
#   output  LLM05 leak/injection screen (guardrail.screen_output)
# Policy per task: "block" raises LLMUnavailableError -- which every caller
# already answers with its deterministic path -- and "flag" counts + audits only.
# A gateway guard must never turn an agent's FAIL-CLOSED handling into a silent
# one: the Reflection critic screens its own output and ESCALATES on a hit
# (agents/reflection.py), so blocking it here would drop that escalation. Tasks
# whose job is to read attack text (security.brief) or grade arbitrary text
# (eval.grounding) are flag-only. The deterministic path never reaches this.
# --------------------------------------------------------------------------
_GUARD_POLICY: dict[str, tuple[str, str]] = {   # task -> (input action, output action)
    "reflection.critic": ("flag", "flag"),
    "security.brief": ("flag", "flag"),
    "eval.grounding": ("flag", "flag"),
}
_DEFAULT_GUARD = ("block", "block")


def _guard_event(task: str | None, stage: str, action: str, detail: str) -> None:
    """Count and audit one guard event. Content-free: task, kinds, category."""
    label = task or "untasked"
    with contextlib.suppress(Exception):
        metrics.inc(metrics.LLM_GUARD, task=label, stage=stage, action=action)
    case_id = correlation.get_case()
    if case_id and case_id != correlation.UNSET:
        with contextlib.suppress(Exception):
            from .audit import audit_log

            audit_log.record(case_id, "llm-gateway", f"llm.guard.{stage}.{action}", f"task={label} {detail}")
    logger.info("llm guard task=%s stage=%s action=%s %s", label, stage, action, detail)


def _guard_action(task: str | None, stage: int) -> str:
    return "blocked" if _GUARD_POLICY.get(task or "", _DEFAULT_GUARD)[stage] == "block" else "flagged"


def _guard_input(task: str | None, prompt: str) -> str:
    """Redact, then screen. Returns the prompt to send; raises when blocked."""
    masked, kinds = redact.redact(prompt)
    if kinds:
        _guard_event(task, "input", "redacted", f"kinds={','.join(kinds)}")
    verdict = guardrail.screen_llm_input(masked)
    if verdict.status != "pass":
        action = _guard_action(task, 0)
        _guard_event(task, "input", action, f"category={verdict.category}")
        if action == "blocked":
            raise LLMUnavailableError("per-call guardrail blocked the prompt (injection pattern)")
    return masked


def _guard_output(task: str | None, text: str) -> bool:
    """True if the output is clean (and may be cached). Raises when blocked."""
    if guardrail.screen_output(text).status == "pass":
        return True
    action = _guard_action(task, 1)
    _guard_event(task, "output", action, "category=output_leak")
    if action == "blocked":
        raise LLMUnavailableError("per-call guardrail suppressed the model output (LLM05)")
    return False


def _meter(provider, model: str | None, system: str, prompt: str, result: str) -> None:
    """Cost metering on EVERY provider path: a provider that does not record its
    own `usage` (only OpenAIProvider does) is metered by estimate."""
    if getattr(provider, "meters_usage", False):
        return
    with contextlib.suppress(Exception):
        llm_cost.record_estimated(_metric_model(provider, model), f"{system}\n{prompt}", result)


def _count_route(task: str | None, model: str, reason: str) -> None:
    with contextlib.suppress(Exception):
        metrics.inc(metrics.LLM_ROUTE, task=task or "untasked", model=model, reason=reason)


async def complete(
    system: str, prompt: str, json_mode: bool = False, json_schema: dict | None = None,
    *, task: str | None = None,
    # [Safety] The bounded safety adjudicator picks its own provider chain,
    # model and timeout (agents/safety.py), because a red-flag review must not
    # inherit whatever tier the router would have chosen for a generic task.
    provider_order: list[str] | None = None,
    model_override: str | None = None,
    timeout_override: float | None = None,
    # [Agentic] Difficulty hints for model escalation (see choose_tier). None =
    # the task's base tier, exactly as before; callers need not pass it.
    difficulty: dict | None = None,
) -> str:
    """Try each configured provider in order; return the first usable answer.

    Raises LLMUnavailableError if every provider fails — callers then fall back
    to deterministic logic. Only provider names/outcomes are logged, never the
    prompt or response content (healthcare data).
    """
    # [AI-Security] ASI10 kill switch: when engaged, no LLM provider is invoked
    # at all — the pipeline is forced onto its deterministic, auditable rules
    # (agents.py fallbacks). This is the "revoke agent autonomy" control.
    if config.KILL_SWITCH:
        raise LLMUnavailableError("kill switch engaged: LLM providers disabled (deterministic-only mode)")
    call_started = time.monotonic()

    # [Microservices] Every container except llm-gateway reaches a model only
    # through it: one place holds the provider keys, the breakers, the cache and
    # the cost accounting.
    if config.LLM_GATEWAY_URL:
        return await _complete_via_gateway(system, prompt, json_mode, json_schema, task, difficulty)

    # [AI-Security] Per-call input guard BEFORE the cache: the key is built from
    # the redacted prompt, and a blocked prompt never reaches cache or provider.
    prompt = _guard_input(task, prompt)

    route = ROUTES.get(task) if task else None
    if task and route is None:
        logger.info("llm router: unknown task %r routed to the default tier, uncached", task)
    tier, reason = choose_tier(route.tier, difficulty) if route is not None else (None, "unrouted")
    if model_override:
        reason = "override"
    lookup = None
    if route is not None:
        _record_route(task, tier)
        if route.cacheable and _cache_ttl() > 0:
            lookup = _Lookup(task, tier, system, prompt, json_mode, json_schema)
            cached = _cache_get(lookup)
            if cached is not None:
                first = next(iter(_ordered(provider_order)), None)
                _log_call(task, tier, reason, _model_for(first.name, tier) if first else None, call_started, cached=True)
                logger.info("llm.complete served from cache task=%s", task)
                return cached

    # Only ever a `_safe_detail` string, so neither the log line below nor the
    # exception raised to the caller can carry prompt/response content.
    last: str | None = None
    served = False
    for provider in _ordered(provider_order):
        breaker = _breaker(provider.name)
        # [ASI08] Skip a provider that has already proved it is down, rather than
        # paying its timeout again for every worker on every case in flight.
        if breaker.is_open(time.monotonic()):
            last = f"{provider.name}: circuit breaker open"
            logger.info("llm provider=%s skipped: circuit breaker open", provider.name)
            continue
        model = None
        # [MLOps] Timed from BEFORE the call and observed on every exit path,
        # including the failing ones. A provider that times out is the single
        # biggest contributor to a slow triage, and recording only successes would
        # make the p95 improve as the chain degrades.
        started = time.monotonic()
        try:
            if not await provider.available():
                continue
            # An explicit model_override wins over the router's tier choice: the
            # caller that passes one has already decided which model is fit for
            # the job (see the safety adjudicator).
            model = model_override or (_model_for(provider.name, tier) if route is not None else None)
            # Only the options actually in play are passed on: a provider is any
            # object with `complete(system, prompt, json_mode, json_schema)`, and
            # sending it keyword arguments it never asked for is what turns a
            # default call into a TypeError.
            options: dict = {}
            if model:
                options["model"] = model
            if timeout_override is not None:
                options["timeout_override"] = timeout_override
            _count_route(task, _metric_model(provider, model), reason)
            span = tracing.generation_start(task, _metric_model(provider, model), system, prompt)
            try:
                result = await provider.complete(system, prompt, json_mode, json_schema, **options)
            except Exception as exc:
                tracing.end(span, error=type(exc).__name__)
                raise
            tracing.end(span, {"content": result})
            breaker.record_success()
            metrics.observe_llm_latency(_metric_model(provider, model), time.monotonic() - started, "ok")
            _meter(provider, model, system, prompt, result)
            logger.info("llm.complete served by provider=%s task=%s", provider.name, task or "-")
            served = True
            _log_call(task, tier, reason, _metric_model(provider, model), call_started)
            break
        except LLMUnavailableError as exc:
            last = _safe_detail(exc)
            tripped = breaker.record_failure(time.monotonic())
            # "timed out" is separated from every other unavailability because the
            # two have opposite shapes: a timeout spends the full budget, a missing
            # API key costs nothing. Averaged together they describe neither.
            outcome = "timeout" if "timed out" in last.lower() else "error"
            metrics.observe_llm_latency(_metric_model(provider, model), time.monotonic() - started, outcome)
            logger.warning(
                "llm provider=%s unavailable: %s%s", provider.name, last,
                " (circuit breaker OPENED)" if tripped else "",
            )
        except Exception as exc:  # defensive: a provider bug must not crash triage  # noqa: BLE001 - any provider fault must advance the chain, never propagate
            last = _safe_detail(exc)
            tripped = breaker.record_failure(time.monotonic())
            metrics.observe_llm_latency(_metric_model(provider, model), time.monotonic() - started, "error")
            logger.warning(
                "llm provider=%s errored: %s%s", provider.name, last,
                " (circuit breaker OPENED)" if tripped else "",
            )
    if not served:
        raise LLMUnavailableError(
            f"no LLM provider available (order={provider_order or config.LLM_PROVIDER_ORDER}): {last}"
        )
    # [AI-Security] Output guard OUTSIDE the provider try-block: a suppressed
    # answer is a content problem, not a provider fault, and must not trip the
    # breaker. Flagged output is never cached.
    if _guard_output(task, result) and lookup is not None:
        _cache_put(lookup, result)
    return result


async def _complete_via_gateway(system: str, prompt: str, json_mode: bool, json_schema: dict | None,
                                task: str | None, difficulty: dict | None = None) -> str:
    """Forward one completion to llm-gateway. Failure modes collapse to
    LLMUnavailableError, which every caller already handles by falling back to
    its deterministic path — a down gateway degrades quality, never safety.
    Exception messages carry status/exception type only, never prompt or reply
    text (healthcare data), matching `complete`'s own `_safe_detail` rule."""
    headers = {"X-Correlation-Id": correlation.get(), "X-Case-Id": correlation.get_case(),
               "X-Internal-Token": config.INTERNAL_TOKEN}
    body = {"system": system, "prompt": prompt, "json_mode": json_mode, "json_schema": json_schema, "task": task,
            "difficulty": difficulty}
    try:
        async with httpx.AsyncClient(base_url=config.LLM_GATEWAY_URL, timeout=config.AGENT_TIMEOUT_SECONDS,
                                     transport=_GATEWAY_TRANSPORT) as client:
            response = await client.post("/v1/complete", json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise LLMUnavailableError(f"llm-gateway unreachable: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise LLMUnavailableError(f"llm-gateway HTTP {response.status_code}")
    try:
        payload = response.json()
        text = str(payload["text"])
    except (KeyError, TypeError, ValueError):
        raise LLMUnavailableError("llm-gateway returned an unusable body") from None
    call = payload.get("call") if isinstance(payload, dict) else None
    log = _CALL_LOG.get()
    if log is not None and isinstance(call, dict):
        log.append(call)   # the gateway resolved tier and model; record them here
    return text


async def health() -> dict:
    """Report per-provider availability and the active provider (for /api/health)."""
    providers: dict = {}
    active: str | None = None
    for provider in _ordered():
        try:
            ok = bool(await provider.available())
        except Exception:  # noqa: BLE001 - any provider fault must advance the chain, never propagate
            ok = False
        providers[provider.name] = ok
        if ok and active is None:
            active = provider.label()
    # [ASI08] Surface the breakers. A provider can be reachable (`available`) and
    # still be skipped because it kept failing, and an operator who cannot see
    # that reads the skip as an outage of the whole chain.
    return {
        "available": active is not None,
        "active": active,
        "providers": providers,
        "breakers": breaker_state(),
    }
