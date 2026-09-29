"""[MLOps] Audit trail -- FR-13 traceability.

Records, per case, an ordered list of every agent's reasoning, tool
evidence, confidence, and decision, so the full triage pipeline for any
case can be reconstructed and reviewed after the fact. This is both a
clinical/regulatory traceability requirement (every agent's decision must
be explainable and reviewable) and a standard MLOps observability control
(structured, queryable step-by-step execution log per request).

This is deliberately a small in-memory structure -- like `app/store.py`,
a real deployment would back this with durable, append-only storage (e.g.
a write-once log table or event stream), but the shape of the API
(`record` / `for_case`) would stay the same.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import count
from threading import Lock

from . import persistence

logger = logging.getLogger("careroute.audit")

_GENESIS = "0" * 64  # prev_hash of the first entry in every case's chain


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _entry_hash(seq: int, ts: str, actor: str, action: str, detail: str, prev_hash: str) -> str:
    """SHA-256 over the entry's content + the previous entry's hash. Chaining
    the hashes makes the log tamper-EVIDENT: altering or dropping any past
    entry breaks every subsequent hash, which `verify_chain` detects."""
    payload = f"{seq}|{ts}|{actor}|{action}|{detail}|{prev_hash}".encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass
class AuditEntry:
    seq: int
    ts: str
    actor: str
    action: str
    detail: str
    confidence: float | None = None
    acuity: str | None = None
    prev_hash: str = _GENESIS
    entry_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "detail": self.detail,
            "confidence": self.confidence,
            "acuity": self.acuity,
            "prevHash": self.prev_hash,
            "entryHash": self.entry_hash,
        }


class AuditLog:
    """Thread-safe, in-memory, per-case ordered audit trail.

    `record(...)` appends one entry per pipeline step (guardrail decision,
    each agent's result, the safety-override decision, and the final
    aggregation). `for_case(case_id)` returns the ordered list for display
    or export, e.g. via `GET /api/cases/{case_id}/audit` in main.py.
    """

    def __init__(self, redis_client=None) -> None:
        self._entries: dict[str, list[AuditEntry]] = {}
        self._counters: dict[str, count[int]] = {}
        self._lock = Lock()
        # [Microservices] Append-only copy in Redis; reloaded by a new process.
        self._redis = redis_client
        if redis_client is not None:
            self._load()

    def record(
        self,
        case_id: str,
        actor: str,
        action: str,
        detail: str,
        confidence: float | None = None,
        acuity: str | None = None,
    ) -> AuditEntry:
        with self._lock:
            if case_id not in self._entries:
                self._entries[case_id] = []
                self._counters[case_id] = count(1)
            seq = next(self._counters[case_id])
            prev_hash = self._entries[case_id][-1].entry_hash if self._entries[case_id] else _GENESIS
            ts = _now_iso()
            entry = AuditEntry(
                seq=seq,
                ts=ts,
                actor=actor,
                action=action,
                detail=detail,
                confidence=confidence,
                acuity=acuity,
                prev_hash=prev_hash,
                entry_hash=_entry_hash(seq, ts, actor, action, detail, prev_hash),
            )
            self._entries[case_id].append(entry)
            persistence.write(self._redis, "rpush", persistence.AUDIT_PREFIX + case_id, json.dumps(asdict(entry)))
            return entry

    def for_case(self, case_id: str) -> list[dict]:
        return [e.to_dict() for e in self._entries.get(case_id, [])]

    def verify_chain(self, case_id: str) -> bool:
        """[AI-Security] Recompute the hash chain for a case and confirm it is
        intact — returns False if any entry was altered, inserted, or removed."""
        prev = _GENESIS
        for e in self._entries.get(case_id, []):
            expected = _entry_hash(e.seq, e.ts, e.actor, e.action, e.detail, prev)
            if e.entry_hash != expected or e.prev_hash != prev:
                return False
            prev = e.entry_hash
        return True

    def has_case(self, case_id: str) -> bool:
        return case_id in self._entries

    def _load(self) -> None:
        prefix = persistence.AUDIT_PREFIX
        try:
            for key in self._redis.scan_iter(match=prefix + "*"):
                entries = [AuditEntry(**json.loads(row)) for row in self._redis.lrange(key, 0, -1)]
                case_id = key[len(prefix):]
                self._entries[case_id] = entries
                self._counters[case_id] = count(len(entries) + 1)
        except Exception:  # noqa: BLE001 - an unreadable audit store must not stop the API starting
            logger.warning("audit: redis unavailable at start-up; trail starts empty", exc_info=False)


# Module-level singleton, mirroring `store` in app/store.py.
audit_log = AuditLog(redis_client=persistence.connect())
