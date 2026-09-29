"""[Microservices] rag-service — the corpus index and the embedder, loaded once
and shared by every agent that retrieves (classifier, handoff, the gateway)."""
from __future__ import annotations

from fastapi import FastAPI, Header
from pydantic import BaseModel, Field

from .. import config, rag
from .common import add_common_routes, check_token


class RetrieveRequest(BaseModel):
    query: str
    top_k: int = Field(default=2, ge=1, le=10)


def build_rag_app() -> FastAPI:
    if config.RAG_SERVICE_URL:
        raise RuntimeError("rag-service must retrieve itself; unset CAREROUTE_RAG_SERVICE_URL here")
    app = FastAPI(title="careroute-rag-service")
    add_common_routes(app, "rag")

    # A plain `def`: FastAPI runs it on its thread pool, which is where every
    # retrieval already runs (the embedder is warmed on the main thread first).
    @app.post("/v1/retrieve")
    def retrieve(req: RetrieveRequest, x_internal_token: str | None = Header(default=None)) -> dict:
        check_token(x_internal_token)
        return rag.retrieve_detailed(req.query, req.top_k)

    return app
