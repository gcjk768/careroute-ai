"""[Agentic][RAG] E12 — CONTEXT quality, not ranking quality.

E10 (`app/evals/retrieval.py`) asks whether the right document is *ranked*.
This asks what fraction of the context actually *handed to the model* is
relevant — the LLMSecOps deck's retrieval-precision gate, and the half of it
E10 cannot see. The two come apart immediately: a retriever with a perfect
ranking still scores 0.50 on precision if it always returns two documents and
only one of them is relevant.

Metrics, over the same 32-query gold set:
  context precision   mean over queries of  |relevant ∩ returned| / |returned|
  context recall      fraction of queries whose gold document is in the context
  context size        mean documents returned (the cost precision is bought with)

RELEVANCE IS THE STRICT READING: exactly the one document `rag_queries.json`
labels. The corpus carries one document per clinical presentation, so that is
usually the whole truth — but where two genuinely overlap (a bee sting is
anaphylaxis *and* an airway risk) it under-counts a returned document that a
clinician would have accepted. The gold set was written before any retriever
was measured on it, and the strict reading is the one that cannot be argued
upward after the fact; the lenient one would need a second labelling pass whose
labels are chosen with the scores already visible.

Two policies are scored side by side so the change is legible:
  fixed       top_k documents, always — what ran until 2026-09-17
  adaptive    `rag.assemble_context` drops a dominated runner-up

Run it:  python -m app.evals.context          (sweeps the margin as well)
"""
from __future__ import annotations

import json
import sys

from .retrieval import load_queries

POLICIES = ("fixed", "adaptive")


def _context_titles(
    query: str, mode: str, policy: str, *, top_k: int, margin: float, embedder
) -> list[str]:
    """The document titles a policy would hand to the model for one query."""
    from .. import rag

    if mode == "lexical":
        ranking, dense_scores = rag._lexical_ranking(query), None
    else:
        dense = rag._dense_ranking(query, embedder)
        ranking = rag._fuse([rag._lexical_ranking(query), dense])
        dense_scores = dict(dense)
    if policy == "fixed":
        kept = [doc for doc, _ in ranking][:top_k]
    else:
        kept = rag.assemble_context(ranking, dense_scores, top_k, margin=margin)
    return [rag.CORPUS[doc].title for doc in kept]


def evaluate(
    mode: str = "hybrid",
    policy: str = "adaptive",
    queries: list[dict] | None = None,
    *,
    top_k: int = 2,
    margin: float | None = None,
    embedder=None,
) -> dict:
    """Context precision / recall / size for one retrieval mode and policy."""
    from .. import rag

    margin = rag.CONTEXT_MARGIN if margin is None else margin
    queries = queries if queries is not None else load_queries()
    precisions, recalls, sizes = [], [], []
    for q in queries:
        returned = _context_titles(
            q["query"], mode, policy, top_k=top_k, margin=margin, embedder=embedder
        )
        relevant = {q["expected"]}
        hits = sum(1 for title in returned if title in relevant)
        precisions.append(hits / len(returned) if returned else 0.0)
        recalls.append(1.0 if hits else 0.0)
        sizes.append(len(returned))

    n = len(queries) or 1
    return {
        "mode": mode,
        "policy": policy,
        "margin": round(margin, 4) if policy == "adaptive" else None,
        "n": len(queries),
        "context_precision": round(sum(precisions) / n, 4),
        "context_recall": round(sum(recalls) / n, 4),
        "context_size": round(sum(sizes) / n, 4),
    }


def sweep(
    margins: tuple[float, ...] = (0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.12, 1.0),
    *,
    mode: str = "hybrid",
    embedder=None,
) -> list[dict]:
    """Precision/recall against the margin — the evidence for CONTEXT_MARGIN.

    Read it as a trade curve, and pick the widest trim that costs no recall: a
    margin of 1.0 is the fixed policy (nothing is ever dominated), 0.0 keeps
    only documents tied with the best.
    """
    return [evaluate(mode, "adaptive", margin=m, embedder=embedder) for m in margins]


def main() -> int:
    from .. import rag_embed

    embedder = rag_embed.get_embedder()
    mode = "hybrid" if embedder is not None else "lexical"
    report = {
        "embedder": getattr(embedder, "name", None),
        "mode": mode,
        "policies": [evaluate(mode, p, embedder=embedder) for p in POLICIES],
        "margin_sweep": sweep(mode=mode, embedder=embedder) if embedder is not None else [],
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
