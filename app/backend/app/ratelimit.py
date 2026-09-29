"""[AI-Security] OWASP LLM10 — Unbounded Consumption / DoS control.

A tiny, dependency-free fixed-window rate limiter. Each caller (keyed by client
IP, falling back to a session id) gets at most `limit` requests per 60-second
window against the expensive triage path; the next request is rejected with a
429 before it can reach the LLM / ML model.

`slowapi` is the production-grade drop-in (shared Redis backend for multi-replica
deployments); this in-process limiter keeps the prototype dependency-free while
demonstrating the control and is unit-testable via `allow()` directly.
"""
from __future__ import annotations

import time
from threading import Lock


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = Lock()
        self._last_prune: float | None = None

    def _prune(self, ts: float) -> None:
        """Drop buckets with nothing left inside the current window.

        `allow()` only ever trimmed the bucket it was asked about, so every key
        that was seen once stayed in the dict forever — an unbounded-memory leak
        that is itself an LLM10 denial-of-service, and one an attacker could
        drive deliberately by varying the key. Sweeping at most once per window
        keeps the cost amortised to O(keys) per window rather than per request.
        Caller must hold `self._lock`.
        """
        window_start = ts - self.window
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] < window_start]:
            del self._hits[key]
        self._last_prune = ts

    def allow(self, key: str, now: float | None = None) -> bool:
        """Return True if `key` is under its limit for the current window."""
        if self.limit <= 0:  # 0/negative disables limiting
            return True
        ts = time.monotonic() if now is None else now
        with self._lock:
            if self._last_prune is None or ts - self._last_prune >= self.window:
                self._prune(ts)
            window_start = ts - self.window
            recent = [t for t in self._hits.get(key, []) if t >= window_start]
            if len(recent) >= self.limit:
                self._hits[key] = recent
                return False
            recent.append(ts)
            self._hits[key] = recent
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._last_prune = None


class AbuseMonitor:
    """[AI-Security] Behavioural anomaly control: quarantine a client that keeps
    tripping the guardrail with ATTACKS (LLM10 monitoring & anomaly detection;
    GenAI security policy template §8.4).

    The request-rate limiter above cannot see intent: one jailbreak attempt
    every few seconds is well under 30 requests a minute. This counts guardrail
    blocks in the injection / structure categories per client inside a sliding
    `window_seconds`; reaching `threshold` quarantines the client for
    `cooldown_seconds`, during which every request is refused before any work
    runs. `threshold <= 0` disables the monitor. Idle clients are swept at most
    once per window, so memory cannot be grown by varying the key."""

    def __init__(self, threshold: int, window_seconds: float, cooldown_seconds: float) -> None:
        self.threshold = threshold
        self.window = window_seconds
        self.cooldown = cooldown_seconds
        self._blocks: dict[str, list[float]] = {}
        self._quarantined_until: dict[str, float] = {}
        self._lock = Lock()
        self._last_prune: float | None = None

    def _prune(self, ts: float) -> None:
        horizon = ts - self.window
        for key in [k for k, hits in self._blocks.items() if not hits or hits[-1] < horizon]:
            del self._blocks[key]
        for key in [k for k, until in self._quarantined_until.items() if until <= ts]:
            del self._quarantined_until[key]
        self._last_prune = ts

    def record_block(self, key: str, now: float | None = None) -> bool:
        """Record one attack-category block. Returns True only when this block
        STARTS a new quarantine (so the caller audits it once)."""
        if self.threshold <= 0:
            return False
        ts = time.monotonic() if now is None else now
        with self._lock:
            if self._last_prune is None or ts - self._last_prune >= self.window:
                self._prune(ts)
            if self._quarantined_until.get(key, 0.0) > ts:
                return False
            recent = [t for t in self._blocks.get(key, []) if t >= ts - self.window]
            recent.append(ts)
            self._blocks[key] = recent
            if len(recent) >= self.threshold:
                self._quarantined_until[key] = ts + self.cooldown
                self._blocks.pop(key, None)
                return True
            return False

    def is_blocked(self, key: str, now: float | None = None) -> bool:
        if self.threshold <= 0:
            return False
        ts = time.monotonic() if now is None else now
        with self._lock:
            return self._quarantined_until.get(key, 0.0) > ts

    def tracked_clients(self) -> int:
        with self._lock:
            return len(set(self._blocks) | set(self._quarantined_until))

    def reset(self) -> None:
        with self._lock:
            self._blocks.clear()
            self._quarantined_until.clear()
            self._last_prune = None
