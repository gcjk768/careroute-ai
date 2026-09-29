"""[Agentic][RAG] Text EMBEDDINGS for dense retrieval (platform-owned).

ArchAAS Day 1 builds retrieval on embeddings: text is mapped to a dense vector
and relevance is cosine similarity, so "squeezing behind my breastbone" can
retrieve chest-pain guidance without sharing a single word with it. The
lexical TF-IDF retriever in `rag.py` cannot do that — it only matches terms.

BACKENDS, chosen by CAREROUTE_RAG_EMBEDDINGS
--------------------------------------------
  auto      (default) the local ONNX model if `fastembed` is installed, else none
  fastembed local `BAAI/bge-small-en-v1.5` (384-d) via onnxruntime — no torch,
            and no text ever leaves the host. Chosen as the default because the
            query is a patient's (already redacted) symptom description.
  openai    `text-embedding-3-small`, the model the course's embeddings demo
            uses. EXPLICIT opt-in only: it is a new egress path for patient text,
            so it is never selected automatically, and it needs OPENAI_API_KEY.
  off       no embeddings; `rag.py` stays purely lexical.

With no backend the retriever degrades to TF-IDF, exactly as before this module
existed — that is how CI runs, since `fastembed` lives in the optional
`requirements-agentic.txt`.

Every embedder returns L2-NORMALISED float32 rows, so cosine similarity is a dot
product. Queries and passages are embedded by separate methods because
retrieval-tuned models (bge) prefix queries with an instruction.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Protocol

import numpy as np

logger = logging.getLogger("careroute.rag.embed")

FASTEMBED_MODEL = os.environ.get("CAREROUTE_RAG_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
OPENAI_EMBED_MODEL = os.environ.get("CAREROUTE_RAG_OPENAI_EMBED_MODEL", "text-embedding-3-small")


class Embedder(Protocol):
    name: str

    def embed_queries(self, texts: list[str]) -> np.ndarray: ...

    def embed_passages(self, texts: list[str]) -> np.ndarray: ...


def _normalise(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class FastEmbedEmbedder:
    """Local ONNX sentence embeddings (fastembed). Loads the model lazily."""

    def __init__(self, model: str = FASTEMBED_MODEL) -> None:
        self.model_name = model
        self.name = f"fastembed:{model}"
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
                    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
                    from fastembed import TextEmbedding

                    self._model = TextEmbedding(self.model_name)
        return self._model

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return _normalise(np.stack(list(self._load().query_embed(texts))))

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return _normalise(np.stack(list(self._load().passage_embed(texts))))


class OpenAIEmbedder:
    """OpenAI embeddings API — explicit opt-in (patient-text egress)."""

    def __init__(self, model: str = OPENAI_EMBED_MODEL) -> None:
        self.model_name = model
        self.name = f"openai:{model}"

    def _embed(self, texts: list[str]) -> np.ndarray:
        import httpx

        from . import config

        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        base = (os.environ.get("OPENAI_BASE_URL") or getattr(config, "OPENAI_BASE_URL", "") or
                "https://api.openai.com/v1").rstrip("/")
        response = httpx.post(
            f"{base}/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": self.model_name, "input": texts},
            timeout=10.0,
        )
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda r: r["index"])
        return _normalise(np.asarray([r["embedding"] for r in rows]))

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts)

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts)


_override: Embedder | None = None
_override_set = False
_cached: dict[str, Embedder | None] = {}
_cache_lock = threading.Lock()


def set_embedder(embedder: Embedder | None) -> None:
    """Pin the embedder (tests, or an operator forcing lexical-only with None)."""
    global _override, _override_set
    _override, _override_set = embedder, True


def reset_embedder() -> None:
    global _override, _override_set
    _override, _override_set = None, False
    with _cache_lock:
        _cached.clear()


def backend_setting() -> str:
    return os.environ.get("CAREROUTE_RAG_EMBEDDINGS", "auto").strip().lower() or "auto"


def _first_native_import_would_be_off_thread() -> bool:
    """True when loading fastembed here would be the FIRST onnxruntime import
    in this process AND we are not on the main thread — the exact combination
    that takes the interpreter down rather than raising. Once anything has
    imported it, later threads are fine."""
    return (
        "fastembed" not in sys.modules
        and "onnxruntime" not in sys.modules
        and threading.current_thread() is not threading.main_thread()
    )


def warm() -> Embedder | None:
    """Load the embedding backend **on the calling thread**, best-effort.

    Not an optimisation — a crash guard. Every retrieval call site runs
    `rag.retrieve` on a worker thread (`asyncio.to_thread`), because retrieval
    is synchronous and would otherwise block the event loop. If that worker
    thread is where onnxruntime is imported for the FIRST time in a process
    that has already loaded the rest of the app, the interpreter dies on
    Windows with an access violation — a SIGSEGV, not an exception, so the
    careful `except Exception: fall back to lexical` around every retrieval
    cannot catch it and the process is simply gone.

    Reproduced 2026-09-17 with fastembed 0.8.0 + onnxruntime on Python 3.12:
    `pytest tests/agents/test_classifier.py` dies during the first threaded
    `rag.retrieve`, and passes if anything imported onnxruntime on the main
    thread first. Startup and the test session therefore warm it here, where
    a failure is an ordinary ImportError and degrades to lexical retrieval.
    """
    try:
        embedder = get_embedder()
        if embedder is not None:
            embedder.embed_queries(["warm"])     # forces the native session too
        return embedder
    except Exception:  # noqa: BLE001 - warming is best-effort; lexical retrieval is the fallback
        logger.warning("embedding warm-up failed; retrieval stays lexical", exc_info=False)
        # Pin "no embedder" so retrieval does not retry a broken model on every
        # request and /api/health reports what retrieval will actually do. A
        # local model that failed to load stays failed; the OpenAI API may just
        # be briefly unreachable, so it keeps retrying per call as before.
        setting = backend_setting()
        if setting != "openai":
            with _cache_lock:
                _cached[setting] = None
        return None


def retrieval_mode() -> str:
    """"hybrid" if a dense embedder is ready, else "tfidf" (for /api/health).

    A fastembed model that has not been loaded yet counts as not ready: after
    `warm()` it is either loaded or pinned to None, so this is the truth."""
    embedder = get_embedder()
    if embedder is None or getattr(embedder, "_model", True) is None:
        return "tfidf"
    return "hybrid"


def get_embedder() -> Embedder | None:
    """The embedder to use for dense retrieval, or None for lexical-only."""
    if _override_set:
        return _override
    setting = backend_setting()
    with _cache_lock:
        if setting in _cached:
            return _cached[setting]
        embedder: Embedder | None = None
        if setting == "openai":
            embedder = OpenAIEmbedder() if os.environ.get("OPENAI_API_KEY", "").strip() else None
        elif setting in ("auto", "fastembed"):
            if _first_native_import_would_be_off_thread():
                # Degrade to lexical INSTEAD OF CRASHING. Importing onnxruntime
                # for the first time on a worker thread is an access violation
                # on Windows, not an exception, so every `except` around
                # retrieval is useless and the process is gone. `warm()` at
                # startup is the fix; this is what happens when nobody called
                # it — a caller that never warmed gets slightly worse retrieval
                # rather than a dead interpreter. NOT cached: the next call from
                # the main thread (or after a warm-up) must still get a real
                # embedder.
                logger.warning(
                    "dense retrieval skipped on this thread: onnxruntime has not been loaded on "
                    "the main thread yet — call rag_embed.warm() at startup",
                )
                return None
            try:
                import fastembed  # noqa: F401 - availability probe only

                embedder = FastEmbedEmbedder()
            except ImportError:
                if setting == "fastembed":
                    logger.warning("CAREROUTE_RAG_EMBEDDINGS=fastembed but fastembed is not installed")
                embedder = None
        _cached[setting] = embedder
        return embedder
