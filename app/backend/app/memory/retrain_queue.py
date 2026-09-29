"""[MLOps][HITL] Learning loop — clinician disagreements queued for retraining.

When a clinician's final acuity differs from the model's, the case is a
labelled counter-example. One JSONL row per disagreement:

    {"ts", "features": [...full FEATURE_NAMES vector...], "label": <acuity index>,
     "clinicianAcuity", "modelAcuity", "ageProvided", "featureSource"}

`features` + `label` are exactly the (X row, y) shape `ml/data.generate_dataset`
returns (label indexes `ml/data.ACUITY_INDEX_TO_CODE`), so a loader is
`np.vstack([X, q_features]), np.concatenate([y, q_labels])`. The vector is the
one actually SERVED (joined from the inference log on caseId, real
demographics) when available; otherwise it is re-derived from the case text
with defaulted demographics and `ageProvided=false` so the trainer can filter
or down-weight it. De-identified: no text, ids, clinician or note.

Location: $CAREROUTE_RETRAIN_QUEUE (default backend/monitoring/retrain_queue.jsonl),
size-capped like the inference log.
"""
from __future__ import annotations

import json
import logging
import os
import threading

logger = logging.getLogger("careroute.memory.retrain_queue")

_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "monitoring", "retrain_queue.jsonl")
)
_lock = threading.Lock()


def queue_path() -> str:
    return os.environ.get("CAREROUTE_RETRAIN_QUEUE", _DEFAULT_PATH)


def _served_vector(case_id: str) -> tuple[list[float], bool] | None:
    """The feature vector the model actually served for `case_id`, if logged."""
    from ..ml.inference_log import log_path

    path = log_path()
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    for line in reversed(lines):
        if case_id not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("caseId") == case_id:
            return [float(v) for v in row["features"]], bool(row.get("ageProvided", False))
    return None


def enqueue(escalation) -> dict | None:
    """Append a disagreement (modelAgreed is False). Best-effort, never raises."""
    if escalation.modelAgreed is not False:
        return None
    try:
        from ..ml.data import ACUITY_INDEX_TO_CODE
        from ..ml.features import extract_features

        served = _served_vector(escalation.caseId)
        if served is not None:
            features, age_provided, source = served[0], served[1], "inference_log"
        else:
            features = [round(float(v), 4) for v in extract_features(escalation.normalisedSymptoms)]
            age_provided, source = False, "text"
        row = {
            "ts": escalation.decidedAt,
            "features": features,
            "label": ACUITY_INDEX_TO_CODE.index(escalation.finalAcuity),
            "clinicianAcuity": escalation.finalAcuity,
            "modelAcuity": escalation.acuity.code,
            "ageProvided": age_provided,
            "featureSource": source,
        }
        path = queue_path()
        with _lock:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > _MAX_BYTES:
                return None
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        from .. import metrics

        metrics.inc(metrics.RETRAIN_QUEUE)
        return row
    except Exception:  # noqa: BLE001 - the clinician's decision must never fail on telemetry
        logger.warning("retrain-queue append failed at %s", queue_path(), exc_info=False)
        return None


def size() -> int:
    try:
        with open(queue_path(), encoding="utf-8") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0
