"""[Microservices] llm-gateway and rag-service, from both ends."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, llm, rag
from app.llm import complete as REAL_COMPLETE  # captured before conftest disables the LLM
from app.microservices.llm_app import build_llm_app
from app.microservices.rag_app import build_rag_app


@pytest.fixture(autouse=True)
def _no_transports(monkeypatch):
    monkeypatch.setattr(llm, "_GATEWAY_TRANSPORT", None)
    monkeypatch.setattr(rag, "_SERVICE_TRANSPORT", None)


def test_complete_goes_through_the_gateway_when_configured(monkeypatch):
    seen = {}

    def gateway(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"text": "ok from gateway"})

    monkeypatch.setattr(config, "LLM_GATEWAY_URL", "http://llm")
    monkeypatch.setattr(llm, "_GATEWAY_TRANSPORT", httpx.MockTransport(gateway))
    assert asyncio.run(REAL_COMPLETE("sys", "prompt", task=None)) == "ok from gateway"
    assert b'"prompt"' in seen["body"]


@pytest.mark.parametrize("reply", [httpx.Response(503), httpx.Response(500)])
def test_gateway_failure_is_llm_unavailable(monkeypatch, reply):
    monkeypatch.setattr(config, "LLM_GATEWAY_URL", "http://llm")
    monkeypatch.setattr(llm, "_GATEWAY_TRANSPORT", httpx.MockTransport(lambda _r: reply))
    with pytest.raises(llm.LLMUnavailableError):
        asyncio.run(REAL_COMPLETE("sys", "prompt"))


def test_llm_app_serves_complete(monkeypatch):
    async def fake(*_a, **_k):
        return "hello"

    monkeypatch.setattr(llm, "complete", fake)
    reply = TestClient(build_llm_app()).post("/v1/complete", json={"system": "s", "prompt": "p"})
    assert reply.json() == {"text": "hello", "call": None}


def test_llm_app_is_503_with_no_provider():
    # conftest's autouse fixture makes llm.complete raise LLMUnavailableError.
    reply = TestClient(build_llm_app()).post("/v1/complete", json={"system": "s", "prompt": "p"})
    assert reply.status_code == 503


def test_llm_app_refuses_to_point_at_itself(monkeypatch):
    monkeypatch.setattr(config, "LLM_GATEWAY_URL", "http://llm")
    with pytest.raises(RuntimeError):
        build_llm_app()


def test_retrieve_uses_the_service_when_configured(monkeypatch):
    hit = {"title": "T", "snippet": "S", "source": "src"}
    monkeypatch.setattr(config, "RAG_SERVICE_URL", "http://rag")
    monkeypatch.setattr(rag, "_SERVICE_TRANSPORT", httpx.MockTransport(
        lambda _r: httpx.Response(200, json={"hits": [hit], "mode": "hybrid", "embedder": "e"})))
    assert rag.retrieve_detailed("chest pain") == {"hits": [hit], "mode": "hybrid", "embedder": "e"}


def test_a_dead_rag_service_falls_back_to_local_retrieval(monkeypatch):
    monkeypatch.setattr(config, "RAG_SERVICE_URL", "http://rag")
    monkeypatch.setattr(rag, "_SERVICE_TRANSPORT", httpx.MockTransport(lambda _r: httpx.Response(500)))
    result = rag.retrieve_detailed("chest pain")
    assert result["hits"], "a dead rag-service must never cost the case its citations"


def test_rag_app_serves_retrieve():
    reply = TestClient(build_rag_app()).post("/v1/retrieve", json={"query": "chest pain", "top_k": 2})
    assert reply.status_code == 200
    assert reply.json()["hits"]
