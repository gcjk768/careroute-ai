"""[Microservices] Durable backing for the case store, escalation queue and
audit trail.

The gateway is a single replica, so memory stays the truth while it runs and
every mutation is ALSO written to Redis; a restarted gateway reloads from
Redis. Writes are best effort: a Redis outage is logged and never fails a
triage, because the record is still in memory and the patient still needs
their answer. With CAREROUTE_REDIS_URL unset, nothing here runs.
"""
from __future__ import annotations

import logging

from . import config

logger = logging.getLogger("careroute.persistence")

KEY_CASES = "careroute:cases"
KEY_ESCALATIONS = "careroute:escalations"
KEY_SESSIONS = "careroute:sessions"
KEY_DIGESTS = "careroute:digests"
KEY_PRECEDENTS = "careroute:precedents"  # app/memory/precedents.py
AUDIT_PREFIX = "careroute:audit:"


def connect(url: str | None = None):
    url = config.REDIS_URL if url is None else url
    if not url:
        return None
    import redis

    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)


def write(client, command: str, *args) -> None:
    if client is None:
        return
    try:
        getattr(client, command)(*args)
    except Exception:  # noqa: BLE001 - a Redis outage must not fail a triage; memory still holds the record
        logger.warning("redis %s failed; record kept in memory only", command, exc_info=False)
