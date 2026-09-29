"""[RAG upgrade path] Tests for the Onyx retrieval backend + its fallback.

The key guarantee: with Onyx UNCONFIGURED (the default), retrieval transparently
uses the in-process TF-IDF corpus and the {title, snippet, source} contract is
unchanged. With Onyx configured, we map its chunk schema into the same contract.
"""
from __future__ import annotations

import app.rag as rag
import app.rag_onyx as rag_onyx


def test_onyx_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ONYX_BASE_URL", raising=False)
    monkeypatch.delenv("ONYX_API_KEY", raising=False)
    assert rag_onyx.is_enabled() is False
    # onyx_retrieve signals "fall back" (None) when not configured.
    assert rag_onyx.onyx_retrieve("chest pain", 2) is None


def test_retrieve_falls_back_to_tfidf(monkeypatch):
    monkeypatch.delenv("ONYX_BASE_URL", raising=False)
    monkeypatch.delenv("ONYX_API_KEY", raising=False)
    hits = rag.retrieve("crushing chest pain radiating to the arm", top_k=2)
    assert hits and len(hits) <= 2
    for h in hits:  # contract preserved
        assert set(h.keys()) == {"title", "snippet", "source"}


def test_onyx_used_when_configured(monkeypatch):
    monkeypatch.setenv("ONYX_BASE_URL", "https://onyx.test")
    monkeypatch.setenv("ONYX_API_KEY", "onyx_pat_dummy")
    assert rag_onyx.is_enabled() is True

    # Stub Onyx's HTTP response (AdminSearchResponse, Onyx CE v2.11.4) so no
    # real network call is made.
    fake = {"documents": [
        {"semantic_identifier": "Chest Pain Protocol",
         "blurb": "Refer chest pain with arm radiation for immediate ECG.",
         "source_type": "MOH Guideline"},
    ]}
    sent = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import json
            return json.dumps(fake).encode()

    def _urlopen(req, *a, **k):
        import json
        sent["url"], sent["body"] = req.full_url, json.loads(req.data)
        return _Resp()

    monkeypatch.setattr(rag_onyx.urllib.request, "urlopen", _urlopen)
    hits = rag.retrieve("chest pain radiating to the arm", top_k=2)
    assert sent["url"] == "https://onyx.test/api/admin/search"
    assert sent["body"] == {"query": "chest pain radiating to the arm", "filters": {}}
    assert hits[0]["title"] == "Chest Pain Protocol"
    assert hits[0]["source"] == "MOH Guideline"
    assert set(hits[0].keys()) == {"title", "snippet", "source"}


def test_onyx_network_error_falls_back(monkeypatch):
    monkeypatch.setenv("ONYX_BASE_URL", "https://onyx.test")
    monkeypatch.setenv("ONYX_API_KEY", "onyx_pat_dummy")

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(rag_onyx.urllib.request, "urlopen", _boom)
    # Onyx errors -> None -> rag.retrieve uses TF-IDF, never loses citations.
    assert rag_onyx.onyx_retrieve("chest pain", 2) is None
    hits = rag.retrieve("chest pain", top_k=2)
    assert hits and all(set(h) == {"title", "snippet", "source"} for h in hits)
