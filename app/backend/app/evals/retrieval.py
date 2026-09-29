"""[Agentic][RAG] E10 — retrieval quality, measured per retriever.

Scores a retriever against `tests/fixtures/rag_queries.json`: 32 symptom
descriptions, each labelled with the ONE guidance document a clinician would
expect to be cited, split into `lexical` queries (share words with the
document) and `paraphrase` queries (share none). The split is the point: a
single blended number would hide that term matching scores perfectly on one
half and zero on the other.

Metrics per mode and per query kind:
  recall@1, recall@k   is the expected document ranked first / in the top k
  MRR                  mean reciprocal rank of the expected document (0 if absent)

Run it:  python -m app.evals.retrieval
"""
from __future__ import annotations

import json
import os
import sys

_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tests", "fixtures", "rag_queries.json",
)


def load_queries(path: str = _FIXTURE) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["queries"]


def evaluate(mode: str, queries: list[dict] | None = None, *, k: int = 2, embedder=None) -> dict:
    """Metrics for one retrieval mode (lexical | dense | hybrid), overall and by kind."""
    from .. import rag

    queries = queries if queries is not None else load_queries()
    rows = []
    for q in queries:
        order = rag.rank(q["query"], mode, embedder=embedder)
        position = order.index(q["expected"]) + 1 if q["expected"] in order else None
        rows.append((q["kind"], position))

    def _summary(selected: list) -> dict:
        n = len(selected)
        if not n:
            return {"n": 0, "recall@1": 0.0, f"recall@{k}": 0.0, "mrr": 0.0}
        return {
            "n": n,
            "recall@1": round(sum(1 for _, p in selected if p == 1) / n, 4),
            f"recall@{k}": round(sum(1 for _, p in selected if p is not None and p <= k) / n, 4),
            "mrr": round(sum((1.0 / p) if p else 0.0 for _, p in selected) / n, 4),
        }

    return {
        "mode": mode,
        "k": k,
        "all": _summary(rows),
        "lexical": _summary([r for r in rows if r[0] == "lexical"]),
        "paraphrase": _summary([r for r in rows if r[0] == "paraphrase"]),
    }


def main() -> int:
    from .. import rag_embed

    embedder = rag_embed.get_embedder()
    modes = ["lexical"] + (["dense", "hybrid"] if embedder is not None else [])
    report = {"embedder": getattr(embedder, "name", None), "results": [evaluate(m) for m in modes]}
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
