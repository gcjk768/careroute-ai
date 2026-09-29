"""[RAG upgrade path] Onyx (formerly Danswer) retrieval backend.

A drop-in for the in-process TF-IDF retriever in `rag.py`: when an Onyx instance
is configured (env), `retrieve()` queries it for grounding clinical-guidance
chunks and returns them in the SAME shape rag.retrieve() produces
({title, snippet, source}); otherwise it signals "fall back" (returns None) and
rag.py uses TF-IDF. Nothing about the agents or the tool allow-list changes.

Why Onyx: it is a FREE, MIT-licensed (Community Edition), self-hostable RAG
platform (real embeddings + hybrid search + 40+ connectors), so it keeps PHI
in-house — the production-grade successor to the demo TF-IDF corpus.

Enable by setting two environment variables:
    ONYX_BASE_URL = https://onyx.internal.example      # your self-hosted host
    ONYX_API_KEY  = onyx_pat_...                        # Admin Panel -> API keys

Confirm the endpoint path + request/response schema for YOUR Onyx version in its
OpenAPI explorer at  <ONYX_BASE_URL>/api/docs  — the only things to adjust are
_SEARCH_PATH, the request payload, and _map_hit() below (all isolated on purpose).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request  # stdlib — no new runtime dependency

logger = logging.getLogger("careroute.rag.onyx")

# Verified against Onyx CE v2.11.4 (/api/openapi.json, 2026-09-25): the old
# Danswer /api/query/document-search is gone. /api/admin/search is the only
# retrieval endpoint that needs no LLM; it is Vespa keyword (BM25) ranking,
# deduplicated per document, and needs an admin/curator key when auth is on.
# (/api/search in v4.x and chat search always run an LLM selection step.)
_SEARCH_PATH = "/api/admin/search"
_TIMEOUT_S = 4.0  # keep triage latency bounded; fall back if Onyx is slow


def _configured() -> tuple[str, str] | None:
    base = os.environ.get("ONYX_BASE_URL", "").rstrip("/")
    key = os.environ.get("ONYX_API_KEY", "")
    # Only an http(s) endpoint is a valid Onyx host — reject file:/ and other
    # schemes so a mis-set ONYX_BASE_URL can never redirect the request.
    if not base.startswith(("http://", "https://")):
        return None
    return (base, key) if base and key else None


def is_enabled() -> bool:
    """True when an Onyx endpoint + key are configured (for /health surfacing)."""
    return _configured() is not None


def _map_hit(hit: dict) -> dict:
    """Map one Onyx result chunk -> CareRoute's {title, snippet, source} contract."""
    return {
        "title": hit.get("semantic_identifier") or hit.get("document_id") or "Guidance",
        "snippet": hit.get("blurb") or hit.get("content") or "",
        "source": hit.get("source_type") or hit.get("link") or "Onyx",
    }


def onyx_retrieve(query_text: str, top_k: int = 2) -> list[dict] | None:
    """Return top_k grounding chunks from Onyx, or None to signal 'fall back'."""
    cfg = _configured()
    if not cfg or not (query_text and query_text.strip()):
        return None  # not configured / empty query -> TF-IDF path
    base, key = cfg

    payload = {
        "query": query_text,
        # Required by the schema. Optionally scope to a curated document set:
        # {"document_set": ["clinical-guidance"]}. Hit count is server-fixed,
        # so top_k is applied to the response below.
        "filters": {},
    }
    req = urllib.request.Request(  # noqa: S310 - scheme validated to http(s) in _configured()
        f"{base}{_SEARCH_PATH}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        # `base` is validated to be an http(s) Onyx endpoint in _configured(),
        # so this is not an SSRF / file:// sink.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected, bandit.B310-1
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - network/key/instance fault degrades to TF-IDF, never fatal
        # Network error / bad key / slow instance -> degrade to TF-IDF, never fatal.
        logger.warning("Onyx retrieval unavailable; falling back to TF-IDF", exc_info=False)
        return None

    hits = data.get("documents") or []  # AdminSearchResponse.documents: [SearchDoc]
    mapped = [_map_hit(h) for h in hits[:top_k] if h.get("blurb") or h.get("content")]
    return mapped or None  # empty -> let TF-IDF answer so we never lose citations
