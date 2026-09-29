"""[Agentic][HITL] Precedent memory — semantic long-term memory of clinician rulings.

Every LABELLED escalation decision (the clinician supplied a final acuity)
becomes one precedent row:

    {"ts", "v": [0/1 symptom flags], "modelAcuity", "clinicianAcuity", "agreed"}

De-identified by construction: `v` is the binary symptom-category encoding
from `ml/features.py` — no free text, no case/session id, no clinician name,
no note. Demographic columns are deliberately EXCLUDED from `v`: a precedent
is "a similar presentation", and letting age/sex steer similarity would both
re-identify more easily and let one subgroup's rulings leak onto another.

Retrieval is a brute-force cosine k-NN in numpy over at most MAX_ROWS rows —
microseconds at this size, so no vector database is warranted.

SAFETY (monotone): `consult()` only ever reports `escalate=True` when a quorum
of similar past cases were ruled MORE urgent by clinicians than the current
case's acuity. HITL treats that as one more escalation floor; nothing here can
clear an escalation. Worst case of a poisoned memory is therefore extra
clinician reviews, never a missed one — and rows can only be written by the
staff-authenticated decision endpoint.

Storage: Redis list `careroute:precedents` when CAREROUTE_REDIS_URL is set
(the same durability as the escalation queue), else a size-capped JSONL at
$CAREROUTE_PRECEDENT_LOG (default backend/monitoring/precedents.jsonl — point
it at the telemetry volume on AWS), same pattern as ml/inference_log.py.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter

from .. import persistence
from ..models import acuity_rank

logger = logging.getLogger("careroute.memory.precedents")

KEY = persistence.KEY_PRECEDENTS
MAX_ROWS = 5000                     # newest kept (Redis) — memory, not an archive
_MAX_BYTES = 5 * 1024 * 1024        # file mode: stop appending past ~5 MB
K = 5                               # neighbours consulted
MIN_SIMILARITY = 0.75               # cosine floor for "similar presentation"
MIN_UPTRIAGE = 3                    # quorum: never escalate on one or two anecdotes
_DEFAULT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "monitoring", "precedents.jsonl")
)
_lock = threading.Lock()


def log_path() -> str:
    return os.environ.get("CAREROUTE_PRECEDENT_LOG", _DEFAULT_PATH)


def vector(text: str) -> list[int] | None:
    """Symptom-flag vector for `text`, or None when nothing is recognised (a
    vague case has no meaningful neighbours) or the ML layer is unavailable."""
    try:
        from ..ml.features import N_SYMPTOM_FEATURES, extract_features

        flags = [int(v > 0.5) for v in extract_features(text or "")[:N_SYMPTOM_FEATURES]]
    except Exception:  # noqa: BLE001 - memory is advisory; triage must not fail on it
        logger.debug("precedent vector unavailable", exc_info=False)
        return None
    return flags if any(flags) else None


class PrecedentMemory:
    def __init__(self, redis_client=None, path: str | None = None) -> None:
        self._redis = redis_client
        self._path = path
        self._rows: list[dict] | None = None     # Redis mode: memory is the truth while running
        self._file_cache: tuple[tuple, list[dict]] | None = None

    def _file(self) -> str:
        return self._path or log_path()

    def add(self, row: dict) -> None:
        line = json.dumps(row)
        if self._redis is not None:
            self.rows().append(row)
            del self._rows[:-MAX_ROWS]
            persistence.write(self._redis, "rpush", KEY, line)
            persistence.write(self._redis, "ltrim", KEY, -MAX_ROWS, -1)
            return
        try:
            path = self._file()
            with _lock:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                if os.path.exists(path) and os.path.getsize(path) > _MAX_BYTES:
                    return  # size cap reached — same policy as the inference log
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            logger.warning("precedent append failed at %s", self._file(), exc_info=False)

    def rows(self) -> list[dict]:
        if self._redis is not None:
            if self._rows is None:
                try:
                    self._rows = [json.loads(x) for x in self._redis.lrange(KEY, -MAX_ROWS, -1)]
                except Exception:  # noqa: BLE001 - Redis down: serve without memory, retry next start
                    logger.warning("precedent memory: redis unavailable", exc_info=False)
                    self._rows = []
            return self._rows
        # File mode: re-read only when the file changed (path, mtime, size), so a
        # re-pointed env var or another writer is picked up without a restart.
        path = self._file()
        try:
            st = os.stat(path)
        except OSError:
            return []
        key = (path, st.st_mtime_ns, st.st_size)
        if self._file_cache is None or self._file_cache[0] != key:
            rows = []
            with open(path, encoding="utf-8") as fh:
                for line in fh.readlines()[-MAX_ROWS:]:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # torn row from a concurrent append
            self._file_cache = (key, rows)
        return self._file_cache[1]

    def nearest(self, vec: list[int] | None, k: int = K,
                min_similarity: float = MIN_SIMILARITY) -> list[tuple[float, dict]]:
        """k nearest precedents by cosine similarity, most similar first."""
        if not vec:
            return []
        import numpy as np

        rows = [r for r in self.rows() if len(r.get("v") or []) == len(vec)]  # schema-safe
        if not rows:
            return []
        m = np.asarray([r["v"] for r in rows], dtype=float)
        q = np.asarray(vec, dtype=float)
        norms = np.linalg.norm(m, axis=1) * np.linalg.norm(q)
        sims = np.divide(m @ q, norms, out=np.zeros(len(rows)), where=norms > 0)
        order = np.argsort(-sims, kind="stable")[:k]
        return [(float(sims[i]), rows[i]) for i in order if sims[i] >= min_similarity]


MEMORY = PrecedentMemory(persistence.connect())


def record_decision(escalation, memory: PrecedentMemory | None = None) -> dict | None:
    """Store a decided, LABELLED escalation as a precedent. Best-effort."""
    if not escalation.finalAcuity:
        return None
    vec = vector(escalation.normalisedSymptoms)
    if vec is None:
        return None
    row = {
        "ts": escalation.decidedAt,
        "v": vec,
        "modelAcuity": escalation.acuity.code,
        "clinicianAcuity": escalation.finalAcuity,
        "agreed": bool(escalation.modelAgreed),
    }
    (memory or MEMORY).add(row)
    return row


def consult(text: str, current_acuity: str, memory: PrecedentMemory | None = None) -> dict | None:
    """Summarise the k nearest precedents for a new case, or None if there are none.

    `escalate` is True only when at least MIN_UPTRIAGE neighbours — and a
    majority of them — were ruled more urgent by a clinician than
    `current_acuity`. It is never a reason to de-escalate.
    """
    hits = (memory or MEMORY).nearest(vector(text))
    if not hits:
        return None
    choices = Counter(r["clinicianAcuity"] for _s, r in hits)
    choice, n = choices.most_common(1)[0]
    k = len(hits)
    up = sum(acuity_rank(r["clinicianAcuity"]) < acuity_rank(current_acuity) for _s, r in hits)
    return {
        "k": k,
        "clinicianChoice": choice,
        "n": n,
        "upTriaged": up,
        "escalate": up >= MIN_UPTRIAGE and up * 2 > k,
        "text": f"Similar past cases: clinician chose {choice} in {n}/{k}.",
    }
