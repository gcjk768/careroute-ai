"""/api/health reports which Safety-NLP and retrieval backends are ACTIVE.

Review finding B3: the deployed monolith silently ran Safety-NLP's rules and
TF-IDF-only retrieval because the image had neither the model deps nor the
artifacts. Both degrade quietly by design, so the only way to verify a
deployment from outside is for /api/health to say what actually loaded.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app import config, rag_embed
from app.safety_nlp import (
    SafetyNlpRuntime,
    load_safety_model_manifest,
    select_minimum_prototype_model_set,
)

BACKEND = Path(__file__).resolve().parents[1]
MANIFEST = BACKEND / "models" / "safety" / "manifest.json"
REPO = BACKEND.parent

ALL_LOADED = {"translation": "success", "ner": "success", "assertion": "success",
              "context": "success", "category": "success"}


@pytest.fixture(autouse=True)
def _restore():
    prev_runtime = getattr(main_module.app.state, "safety_nlp_runtime", None)
    yield
    main_module.app.state.safety_nlp_runtime = prev_runtime
    rag_embed.reset_embedder()


class _Embedder:
    name = "fake"

    def embed_queries(self, texts):
        raise RuntimeError("model file missing")

    embed_passages = embed_queries


# ---- Safety-NLP ------------------------------------------------------------

def test_health_reports_rules_when_no_safety_runtime():
    main_module.app.state.safety_nlp_runtime = None
    rag_embed.set_embedder(None)

    body = TestClient(main_module.app).get("/api/health").json()

    assert body["safetyNlp"] == "rules"
    assert body["retrieval"] == "tfidf"


def test_health_reports_model_only_when_every_model_stage_loaded():
    main_module.app.state.safety_nlp_runtime = SimpleNamespace(stage_statuses=dict(ALL_LOADED))
    assert TestClient(main_module.app).get("/api/health").json()["safetyNlp"] == "model"

    partial = dict(ALL_LOADED, category="unavailable")
    main_module.app.state.safety_nlp_runtime = SimpleNamespace(stage_statuses=partial)
    assert TestClient(main_module.app).get("/api/health").json()["safetyNlp"] == "rules"


@pytest.mark.asyncio
async def test_safety_nlp_enabled_without_artifacts_degrades_to_rules(monkeypatch, tmp_path):
    """Artifacts absent (as in CI): startup must not fail, and /api/health
    must say rules, not model."""
    monkeypatch.setattr(config, "SAFETY_NLP_ENABLED", True)
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    # The real manifest, pointed at artifacts that do not exist, so this holds
    # on a machine that HAS run the prefetch too.
    manifest = load_safety_model_manifest(MANIFEST)
    for section, key in (("ner", "biomedical"), ("assertion", "clinical"), ("similarity", "biolord")):
        manifest[section][key]["artifact_path"] = f"models/safety/missing-{key}"
    missing = tmp_path / "manifest.json"
    missing.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(config, "SAFETY_NLP_MODEL_MANIFEST", str(missing))
    monkeypatch.setattr(config, "SAFETY_NLP_MODEL_CACHE", str(BACKEND / "no-such-cache"))
    prev_adapters = main_module.orchestrator.safety.nlp_adapters
    try:
        await main_module._configure_safety_nlp_runtime(main_module.app)
        assert main_module.safety_nlp_mode() == "rules"
    finally:
        main_module.orchestrator.safety.set_nlp_adapters(prev_adapters)


# ---- retrieval -------------------------------------------------------------

def test_health_reports_hybrid_when_an_embedder_is_active():
    rag_embed.set_embedder(SimpleNamespace(name="openai:x"))  # no lazy model to load
    assert TestClient(main_module.app).get("/api/health").json()["retrieval"] == "hybrid"


def test_unloaded_fastembed_model_is_not_reported_as_hybrid():
    rag_embed.set_embedder(rag_embed.FastEmbedEmbedder())
    assert rag_embed.retrieval_mode() == "tfidf"


def test_failed_warmup_degrades_to_tfidf_and_stays_there(monkeypatch):
    monkeypatch.setenv("CAREROUTE_RAG_EMBEDDINGS", "auto")
    monkeypatch.setitem(sys.modules, "fastembed", types.ModuleType("fastembed"))
    monkeypatch.setattr(rag_embed, "FastEmbedEmbedder", _Embedder)
    rag_embed.reset_embedder()

    assert rag_embed.warm() is None
    # Cached as "no embedder": retrieval does not retry a broken model on every
    # request, and health agrees with what retrieval will actually do.
    assert rag_embed.get_embedder() is None
    assert rag_embed.retrieval_mode() == "tfidf"


# ---- the pinned manifest the image bakes -----------------------------------

def test_checked_in_manifest_pins_every_runtime_model():
    from app.safety_nlp import prefetch

    manifest = load_safety_model_manifest(MANIFEST)

    assert manifest["runtimeDownloadsAllowed"] is False
    for section, key in prefetch.RUNTIME_MODELS + (prefetch.DIRECT_NLI_MODEL,):
        entry = manifest[section][key]
        assert len(entry["revision"]) == 40, (section, key)
        assert len(entry["artifactSha256"]) == 64, (section, key)
        assert entry["artifact_path"].startswith("models/safety/")
    select_minimum_prototype_model_set(manifest)
    SafetyNlpRuntime.from_manifest_file(MANIFEST, enabled=True, project_root=BACKEND)


@pytest.mark.parametrize(("nlp", "nli", "fetches_nli"), [
    ("0", "0", False), ("1", "0", False), ("1", "1", True),
    ("0", "1", False),  # direct NLI alone is meaningless without the NLP stack under it
])
def test_prefetch_fetches_mdeberta_only_when_direct_nli_is_built(monkeypatch, nlp, nli, fetches_nli):
    from app.safety_nlp import prefetch

    monkeypatch.setenv("WITH_SAFETY_NLP", nlp)
    monkeypatch.setenv("WITH_SAFETY_NLP_DIRECT_NLI", nli)
    assert (prefetch.DIRECT_NLI_MODEL in prefetch.runtime_models()) is fetches_nli
    assert prefetch.runtime_models()[:3] == prefetch.RUNTIME_MODELS


def test_prefetch_fails_the_build_on_a_hash_mismatch(monkeypatch, tmp_path):
    from app.safety_nlp import prefetch

    payload = b"weights"

    def fake_snapshot_download(repo_id, revision, local_dir, allow_patterns):
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        (Path(local_dir) / "model.safetensors").write_bytes(payload)

    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        SimpleNamespace(snapshot_download=fake_snapshot_download))
    good = hashlib.sha256(payload).hexdigest()
    manifest = {"ner": {"biomedical": {"model": "m", "revision": "r",
                                       "artifact_path": "models/safety/x", "artifactSha256": good}}}

    prefetch.download(manifest, tmp_path, models=(("ner", "biomedical"),))

    manifest["ner"]["biomedical"]["artifactSha256"] = "0" * 64
    with pytest.raises(SystemExit, match="sha256"):
        prefetch.download(manifest, tmp_path, models=(("ner", "biomedical"),))


# ---- the CI guard script ---------------------------------------------------

def _guard():
    spec = importlib.util.spec_from_file_location("check_active_backends",
                                                  REPO / "scripts" / "check_active_backends.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ci_guard_accepts_only_model_and_hybrid():
    guard = _guard()
    strict = {"require_safety_nlp": True}
    assert guard.problems({"safetyNlp": "model", "retrieval": "hybrid"}, **strict) == []
    assert len(guard.problems({"safetyNlp": "rules", "retrieval": "tfidf"}, **strict)) == 2
    assert guard.problems({}) != []


# ---- model calls reuse one worker thread ------------------------------------

def _adapter_classes():
    from app.safety_nlp.assertion import ClinicalAssertionAdapter
    from app.safety_nlp.ner import BiomedicalNerAdapter
    from app.safety_nlp.similarity import BioLordSimilarityAdapter, MDebertaNliAdapter
    from app.safety_nlp.translation import MadladTranslationAdapter, NllbTranslationAdapter

    return (ClinicalAssertionAdapter, BiomedicalNerAdapter, BioLordSimilarityAdapter,
            MDebertaNliAdapter, MadladTranslationAdapter, NllbTranslationAdapter)


def test_bounded_model_calls_reuse_one_worker_thread():
    """A fresh thread per model call leaked torch's per-thread intra-op pool:
    measured 22 -> 754 threads and 1.1 -> 2.3 GB RSS over 30 triages. Every
    adapter must run its bounded call on the same long-lived worker."""
    import threading

    fake = SimpleNamespace(config=SimpleNamespace(timeout_ms=1000))
    idents = {cls._with_timeout(fake, threading.get_ident) for cls in _adapter_classes()
              for _ in range(2)}

    assert len(idents) == 1
    assert threading.get_ident() not in idents


# ---- Safety-NLP models are opt-in at build time (WITH_SAFETY_NLP) ----------

def test_default_build_verify_needs_only_the_embedder(monkeypatch):
    """WITH_SAFETY_NLP unset: the build must pass with hybrid retrieval alone
    and never try to load torch models."""
    from app.safety_nlp import prefetch, runtime

    monkeypatch.delenv("WITH_SAFETY_NLP", raising=False)
    monkeypatch.setattr(rag_embed, "warm", lambda: None)
    monkeypatch.setattr(rag_embed, "retrieval_mode", lambda: "hybrid")
    monkeypatch.setattr(runtime.SafetyNlpRuntime, "from_manifest_file",
                        classmethod(lambda *a, **k: pytest.fail("torch models loaded")))
    prefetch.verify()

    monkeypatch.setattr(rag_embed, "retrieval_mode", lambda: "tfidf")
    with pytest.raises(SystemExit, match="tfidf"):
        prefetch.verify()


def test_default_build_prefetch_skips_safety_models(monkeypatch):
    from app.safety_nlp import prefetch

    monkeypatch.delenv("WITH_SAFETY_NLP", raising=False)
    monkeypatch.setattr(prefetch, "download", lambda *a, **k: pytest.fail("safety models downloaded"))
    monkeypatch.setattr(rag_embed, "warm", lambda: object())
    prefetch.main([])


def test_ci_guard_requires_model_only_when_built_with_safety_nlp():
    guard = _guard()
    rules = {"safetyNlp": "rules", "retrieval": "hybrid"}
    assert guard.problems(rules, require_safety_nlp=False) == []
    assert len(guard.problems(rules, require_safety_nlp=True)) == 1
    assert len(guard.problems({"safetyNlp": "model", "retrieval": "tfidf"}, require_safety_nlp=False)) == 1
