"""[Agentic][RAG] E12 — context precision + the adaptive context-assembly policy.

Same two layers as E10: plumbing with a deterministic fake embedder (runs
everywhere, CI included), then the E12 acceptance criteria with the real local
model, visibly skipped when it is not installed.
"""
from __future__ import annotations

import numpy as np
import pytest

import app.rag as rag
from app import rag_embed
from app.evals import context


@pytest.fixture(autouse=True)
def _isolate_embedder(monkeypatch):
    monkeypatch.delenv("ONYX_BASE_URL", raising=False)
    monkeypatch.delenv("ONYX_API_KEY", raising=False)
    rag._dense_cache.clear()
    yield
    rag_embed.reset_embedder()
    rag._dense_cache.clear()


# --- policy plumbing (always runs) -----------------------------------------------
def test_a_dominated_runner_up_is_dropped():
    ranking = [(0, 0.033), (1, 0.016)]
    assert rag.assemble_context(ranking, {0: 0.80, 1: 0.60}, 2, margin=0.05) == [0]


def test_a_close_runner_up_survives():
    ranking = [(0, 0.033), (1, 0.016)]
    assert rag.assemble_context(ranking, {0: 0.80, 1: 0.78}, 2, margin=0.05) == [0, 1]


def test_the_top_document_is_never_dropped_however_weak():
    """Zero context is the fallback path's decision, never this one's."""
    assert rag.assemble_context([(3, 0.016)], {3: 0.10}, 2, margin=0.0) == [3]


def test_without_dense_scores_the_policy_is_exactly_the_old_slice():
    ranking = [(0, 0.9), (1, 0.5), (2, 0.4)]
    assert rag.assemble_context(ranking, None, 2) == [0, 1]
    assert rag.assemble_context(ranking, {}, 3) == [0, 1, 2]


def test_a_runner_up_the_dense_signal_prefers_is_kept():
    """Fusion ranked doc 0 first, dense disagrees. Disagreement keeps both."""
    ranking = [(0, 0.032), (1, 0.016)]
    assert rag.assemble_context(ranking, {0: 0.52, 1: 0.63}, 2, margin=0.05) == [0, 1]


def test_top_k_is_still_a_ceiling():
    ranking = [(0, 0.9), (1, 0.89), (2, 0.88)]
    kept = rag.assemble_context(ranking, {0: 0.8, 1: 0.79, 2: 0.78}, 2, margin=0.5)
    assert kept == [0, 1]


class _TwoDocEmbedder:
    """Query matches the chest-pain document strongly and everything else
    weakly, so the adaptive policy has something unambiguous to trim.

    Keyed on "radiat" (the query says "radiating", the chest-pain guidance says
    "radiation"), NOT on "chest": since the corpus grew to 51 categories six
    documents mention the chest (leg swelling, palpitations, asthma, BLS, panic),
    and a "chest" key gave them all the same dense score as the true answer, so
    the runner-up was no longer dominated and the trim had nothing to do."""

    name = "fake:twodoc"

    def _vec(self, text: str) -> np.ndarray:
        weight = 1.0 if "radiat" in text.lower() else 0.2
        v = np.array([weight, 1.0 - weight], dtype=np.float32)
        return v / np.linalg.norm(v)

    def embed_queries(self, texts):
        return np.stack([self._vec(t) for t in texts])

    def embed_passages(self, texts):
        return np.stack([self._vec(t) for t in texts])


def test_retrieve_returns_one_document_when_the_top_hit_dominates():
    rag_embed.set_embedder(_TwoDocEmbedder())
    detail = rag.retrieve_detailed("crushing chest pain radiating to the arm", top_k=2)
    assert detail["mode"] == "hybrid"
    assert [h["title"] for h in detail["hits"]] == ["Chest Pain — Emergency Assessment"]


def test_the_lexical_path_applies_only_its_own_relative_floor():
    """No dense scores means the dense-margin policy has nothing to trim on.
    What the lexical path applies instead is its own RELATIVE floor (added with
    the corpus widening): a runner-up survives only while it scores at least
    `_RELATIVE_SCORE_FLOOR` of the best hit, and `top_k` stays a ceiling."""
    rag_embed.set_embedder(None)
    query = "crushing chest pain radiating to the arm"
    ranking = rag._lexical_ranking(query)
    floor = ranking[0][1] * rag._RELATIVE_SCORE_FLOOR
    expected = [rag.CORPUS[doc].title for doc, sim in ranking if sim >= floor][:2]

    detail = rag.retrieve_detailed(query, top_k=2)

    assert detail["mode"] == "lexical"
    assert [h["title"] for h in detail["hits"]] == expected
    assert 1 <= len(detail["hits"]) <= 2
    # The chest-pain guidance dominates this query lexically, so the floor is
    # exercised: the second-best document sits below it and is not cited.
    assert ranking[1][1] < floor
    assert len(detail["hits"]) == 1


def test_scoring_a_context_of_one_relevant_document_is_perfect_precision():
    queries = [{"kind": "lexical", "query": "mild headache from a cold", "expected": "Minor Ailments — Self-Care Guidance"}]
    result = context.evaluate("lexical", "fixed", queries, top_k=1)
    assert result["context_precision"] == 1.0
    assert result["context_recall"] == 1.0
    assert result["context_size"] == 1.0


def test_padding_the_context_with_an_irrelevant_document_halves_precision():
    queries = [{"kind": "lexical", "query": "mild headache and a bleeding cut", "expected": "Bleeding Control — Severity Assessment"}]
    assert context.evaluate("lexical", "fixed", queries, top_k=1)["context_precision"] == 1.0
    result = context.evaluate("lexical", "fixed", queries, top_k=2)
    assert result["context_precision"] == 0.5     # recall is unchanged; precision pays
    assert result["context_recall"] == 1.0


# --- quality with the real local model (skipped without it) ----------------------------
def _real_embedder():
    rag_embed.reset_embedder()
    embedder = rag_embed.get_embedder()
    if embedder is None:
        pytest.skip("no embedding backend installed (pip install -r requirements-agentic.txt)")
    return embedder


def test_e12_meets_the_acceptance_criteria():
    embedder = _real_embedder()
    adaptive = context.evaluate("hybrid", "adaptive", embedder=embedder)
    assert adaptive["context_precision"] >= 0.70
    assert adaptive["context_recall"] >= 0.95


def test_e12_the_trim_costs_no_recall():
    """The whole argument for the adaptive policy, pinned: more precision, same recall."""
    embedder = _real_embedder()
    fixed = context.evaluate("hybrid", "fixed", embedder=embedder)
    adaptive = context.evaluate("hybrid", "adaptive", embedder=embedder)
    assert adaptive["context_precision"] > fixed["context_precision"]
    assert adaptive["context_recall"] == fixed["context_recall"]
    assert adaptive["context_size"] < fixed["context_size"]


def test_e12_a_wider_margin_never_scores_better_than_the_chosen_one():
    """CONTEXT_MARGIN is the widest trim that costs no recall — if a larger
    margin ever matched it on precision, the constant would be stale."""
    embedder = _real_embedder()
    chosen = context.evaluate("hybrid", "adaptive", embedder=embedder)
    loose = context.evaluate("hybrid", "adaptive", margin=1.0, embedder=embedder)
    assert loose["context_recall"] == chosen["context_recall"]
    assert loose["context_precision"] < chosen["context_precision"]
