"""[Microservices] llm-gateway — the ONLY container given provider keys.

Every other container calls this instead of a provider (llm.complete sees
CAREROUTE_LLM_GATEWAY_URL and forwards). Here llm.complete runs the real
provider chain, with its breakers, router, cache and cost accounting, so the
cost and latency of every LLM call in the system land on one /metrics.
"""
from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .. import config, correlation, llm
from .common import add_common_routes, check_token


class CompleteRequest(BaseModel):
    system: str
    prompt: str
    json_mode: bool = False
    json_schema: dict | None = None
    task: str | None = None
    difficulty: dict | None = None


def build_llm_app() -> FastAPI:
    if config.LLM_GATEWAY_URL:
        raise RuntimeError("llm-gateway must call providers itself; unset CAREROUTE_LLM_GATEWAY_URL here")
    app = FastAPI(title="careroute-llm-gateway")
    add_common_routes(app, "llm")

    @app.post("/v1/complete")
    async def complete(
        req: CompleteRequest,
        x_internal_token: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None),
        x_case_id: str | None = Header(default=None),
    ) -> dict:
        check_token(x_internal_token)
        token = correlation.bind(correlation.sanitise(x_correlation_id) or correlation.new_correlation_id())
        # The case id groups this call's trace generation (app/tracing.py) with
        # the orchestrator's agent spans of the same case.
        case = correlation.sanitise(x_case_id)
        case_token = correlation.bind_case(case) if case else None
        calls = llm.begin_call_log()
        try:
            text = await llm.complete(req.system, req.prompt, req.json_mode, req.json_schema, task=req.task,
                                    difficulty=req.difficulty)
        except llm.LLMUnavailableError:
            raise HTTPException(status_code=503, detail="no LLM provider available") from None
        finally:
            if case_token is not None:
                correlation.reset_case(case_token)
            correlation.reset(token)
        # Which tier and model served it, for the caller's "AI models used" panel.
        return {"text": text, "call": calls[-1] if calls else None}

    return app
