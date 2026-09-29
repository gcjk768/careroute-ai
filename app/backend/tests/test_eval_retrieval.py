"""[Agentic][RAG] E10 — retrieval evaluation + the dense/hybrid plumbing.

Two layers:

1. PLUMBING, with a deterministic fake embedder, so it runs everywhere
   including CI (which has no `fastembed`): chunking, rank fusion, the dense
   floor, the fallback citation, the three-key `retrieve()` contract, and that a
   dense-path failure degrades to lexical instead of failing the case.

2. QUALITY, with the real local model when it is installed: the E10 acceptance
   criteria from app/evals/plan.py. Skipped — visibly — when no embedder exists.
"""
from __future__ import annotations

import numpy as np
import pytest

import app.rag as rag
from app import rag_embed
from app.evals import retrieval


@pytest.fixture(autouse=True)
def _isolate_embedder(monkeypatch):
    monkeypatch.delenv("ONYX_BASE_URL", raising=False)
    monkeypatch.delenv("ONYX_API_KEY", raising=False)
    rag._dense_cache.clear()
    yield
    rag_embed.reset_embedder()
    rag._dense_cache.clear()


class _ConceptEmbedder:
    """Maps words to a handful of clinical CONCEPTS, so two texts with no word in
    common can still be near each other — the property dense retrieval adds."""

    name = "fake:concepts"
    _CONCEPTS = {
        "cardiac": {"chest", "heart", "cardiac", "breastbone", "sternum", "squeezing", "ecg"},
        "airway": {"breath", "breathless", "airway", "suffocating", "wheezing", "gasping", "air"},
        "fever": {"fever", "temperature", "chills", "shivering", "burning", "rigors"},
        "selfcare": {"mild", "cold", "rash", "headache", "sniffles", "minor"},
        # Since the corpus grew to 51 categories it holds a SECOND cardiac
        # document ("Palpitations and Suspected Arrhythmia") whose sentences also
        # say chest pain and ECG. With only the concepts above it embedded onto
        # the same unit vector as the chest-pain guidance, so a pure-cardiac
        # query tied and the tie-break (not the embedding) picked the answer.
        # A rhythm concept keeps the two documents apart, as a real model would.
        "rhythm": {"palpitations", "arrhythmia", "racing", "irregular", "pulse", "fluttering"},
    }

    def _vec(self, text: str) -> np.ndarray:
        words = set(text.lower().replace(".", " ").replace(",", " ").split())
        counts = [len(words & terms) for terms in self._CONCEPTS.values()]
        # One extra dimension for everything that is NOT a concept word, so a
        # sentence that mentions a concept once in passing (a thrombosis note
        # saying "chest pain or breathlessness suggests PE") is not embedded
        # onto the same unit vector as a sentence that is ABOUT that concept.
        # Without it every incidental "chest" tied the true answer at cosine
        # 1.0 and the tie-break, not the embedding, chose the citation.
        other = len(words) - sum(counts)
        v = np.array([*counts, 0.3 * np.sqrt(max(other, 0))], dtype=np.float32) + 0.01
        return v / np.linalg.norm(v)

    def embed_queries(self, texts):
        return np.stack([self._vec(t) for t in texts])

    def embed_passages(self, texts):
        return np.stack([self._vec(t) for t in texts])


# --- plumbing (always runs) ------------------------------------------------------
def test_corpus_is_chunked_by_sentence_with_the_title_prefixed():
    chunks = rag.chunk_corpus()
    assert len(chunks) > len(rag.CORPUS)          # at least one document splits
    for doc_index, text in chunks:
        assert text.startswith(rag.CORPUS[doc_index].title + ". ")


def test_a_document_is_also_indexed_in_the_patients_own_words():
    """The guidance is written for clinicians ("diaphoresis", "syncope"); the
    query is written by a patient ("sweating buckets", "blacked out"). With 32
    documents, the small embedder no longer bridges that gap on its own (E10
    recall@2 fell from 0.94 to 0.63 after the corpus widening), so each document
    carries a `patient_phrases` line that is embedded as its own chunk - and
    never shown as the citation snippet, which stays the guidance text."""
    doc = next(d for d in rag.CORPUS if d.title.startswith("Chest Pain"))
    assert doc.patient_phrases, "the chest-pain guidance has no patient phrases"
    chunks = [text for index, text in rag.chunk_corpus() if rag.CORPUS[index] is doc]
    assert any(all(p in text for p in doc.patient_phrases) for text in chunks)
    assert rag._as_hit(doc)["snippet"] == doc.text


def test_hybrid_finds_a_paraphrase_that_lexical_misses():
    query = "squeezing behind my breastbone"
    assert "Chest Pain — Emergency Assessment" not in rag.rank(query, "lexical")
    assert rag.rank(query, "hybrid", embedder=_ConceptEmbedder())[0] == "Chest Pain — Emergency Assessment"


def test_retrieve_reports_hybrid_mode_and_keeps_its_three_key_contract():
    rag_embed.set_embedder(_ConceptEmbedder())
    detail = rag.retrieve_detailed("shivering and burning up", top_k=2)
    assert detail["mode"] == "hybrid" and detail["embedder"] == "fake:concepts"
    assert detail["hits"][0]["title"] == "Fever Management in Adults"
    assert all(set(h) == {"title", "snippet", "source"} for h in rag.retrieve("shivering and burning up"))


def test_without_an_embedder_retrieval_is_lexical_as_before():
    rag_embed.set_embedder(None)
    detail = rag.retrieve_detailed("crushing chest pain radiating to the arm")
    assert detail["mode"] == "lexical" and detail["embedder"] is None
    assert detail["hits"][0]["title"] == "Chest Pain — Emergency Assessment"


def test_nothing_relevant_still_returns_one_fallback_citation():
    rag_embed.set_embedder(None)
    detail = rag.retrieve_detailed("zzqx")
    assert detail["mode"] == "fallback"
    assert detail["hits"] == [rag._as_hit(rag.CORPUS[-1])]


def test_a_failing_embedder_degrades_to_lexical_instead_of_failing():
    class _Broken:
        name = "fake:broken"

        def embed_queries(self, texts):
            raise RuntimeError("model file corrupt")

        def embed_passages(self, texts):
            raise RuntimeError("model file corrupt")

    rag_embed.set_embedder(_Broken())
    detail = rag.retrieve_detailed("crushing chest pain")
    assert detail["mode"] == "lexical"
    assert detail["hits"][0]["title"] == "Chest Pain — Emergency Assessment"


def test_dense_floor_excludes_weak_matches():
    class _Flat:
        name = "fake:flat"

        def embed_queries(self, texts):
            return np.tile(np.array([[1.0, 0.0]], dtype=np.float32), (len(texts), 1))

        def embed_passages(self, texts):
            v = np.array([np.cos(1.3), np.sin(1.3)], dtype=np.float32)   # cosine 0.27 < floor
            return np.tile(v, (len(texts), 1))

    assert rag._dense_ranking("anything", _Flat()) == []


def test_openai_embeddings_are_never_selected_without_explicit_opt_in(monkeypatch):
    rag_embed.reset_embedder()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("CAREROUTE_RAG_EMBEDDINGS", "auto")
    embedder = rag_embed.get_embedder()
    assert embedder is None or embedder.name.startswith("fastembed:")
    rag_embed.reset_embedder()
    monkeypatch.setenv("CAREROUTE_RAG_EMBEDDINGS", "off")
    assert rag_embed.get_embedder() is None


def test_lexical_scores_perfectly_on_shared_terms_and_lags_on_paraphrases():
    """Pins the finding that motivated dense retrieval, so it stays visible.
    Lexical scored ZERO on the paraphrases when the index held only the
    clinician-facing guidance; the patient-phrase line each document now
    carries lifts that (it is in the lexical index too, and production may run
    without an embedder), but paraphrases still lag the shared-term queries and
    dense retrieval is what closes the rest of the gap."""
    result = retrieval.evaluate("lexical")
    assert result["lexical"]["recall@2"] == 1.0
    assert result["paraphrase"]["recall@2"] < result["lexical"]["recall@2"]


# --- quality with the real local model (skipped without it) ----------------------------
def _real_embedder():
    rag_embed.reset_embedder()
    embedder = rag_embed.get_embedder()
    if embedder is None:
        pytest.skip("no embedding backend installed (pip install -r requirements-agentic.txt)")
    return embedder


def test_e10_hybrid_meets_the_acceptance_criteria():
    embedder = _real_embedder()
    hybrid = retrieval.evaluate("hybrid", embedder=embedder)
    lexical = retrieval.evaluate("lexical")
    assert hybrid["all"]["recall@2"] >= 0.90
    assert hybrid["paraphrase"]["recall@2"] >= 0.85
    assert hybrid["lexical"]["recall@2"] == 1.0              # dense must not cost exact-term recall
    assert hybrid["all"]["mrr"] > lexical["all"]["mrr"]


def test_e10_chunked_dense_beats_lexical_on_paraphrases():
    embedder = _real_embedder()
    dense = retrieval.evaluate("dense", embedder=embedder)
    assert dense["paraphrase"]["recall@2"] >= 0.85
