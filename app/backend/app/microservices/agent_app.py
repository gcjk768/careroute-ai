"""[Microservices] The HTTP face of ONE agent.

`python -m app.microservices.serve <slug>` runs this for one worker. The worker
class is the SAME class the in-process pipeline uses; this module only moves
CaseState across the network and back. It never logs the state: it is patient
data.
"""
from __future__ import annotations

import inspect
import time

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from .. import config, correlation, llm
from ..agents import registry
from . import wire
from .common import add_common_routes, check_token
from .workers import CARRY, OPS, worker_factory


class InvokeRequest(BaseModel):
    op: str
    case_id: str | None = None
    state: dict
    inbox: list[dict] = Field(default_factory=list)
    consumed: bool = False
    carry: dict = Field(default_factory=dict)


def build_agent_app(slug: str) -> FastAPI:
    make_worker = worker_factory(slug)
    ops = OPS[slug]
    carry_attrs = CARRY.get(slug, ())
    app = FastAPI(title=f"careroute-{slug}-agent")
    add_common_routes(app, slug)

    @app.get("/v1/capability")
    async def capability() -> dict:
        return registry.describe(slug)

    # [Agentic][A2A] The discovery path an A2A client reads first. Unauthenticated
    # on purpose, like the probes: the card is generated metadata, carries no
    # case data, and a client needs it to learn HOW to authenticate.
    @app.get(registry.WELL_KNOWN_PATH)
    async def agent_card() -> dict:
        return registry.agent_card(slug)

    @app.post("/v1/invoke")
    async def invoke(
        req: InvokeRequest,
        x_internal_token: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None),
    ) -> dict:
        check_token(x_internal_token)
        if req.op not in ops:
            raise HTTPException(status_code=404, detail=f"{slug} has no op {req.op!r}")
        try:
            state = wire.state_from_wire(req.state)
            inbox = [wire.message_from_wire(m) for m in req.inbox]
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        cid_token = correlation.bind(correlation.sanitise(x_correlation_id) or correlation.new_correlation_id())
        case_token = correlation.bind_case(req.case_id) if req.case_id else None
        try:
            before = wire.state_to_wire(state)
            calls = llm.begin_call_log()  # returned so the gateway's "AI models used" panel sees them
            worker = make_worker()
            for attr in carry_attrs:
                if attr in req.carry:
                    setattr(worker, attr, req.carry[attr])
            if req.consumed:
                worker.consume(inbox)
            start = time.perf_counter()
            result, messages = None, []
            if req.op == "prescreen":
                result = worker.prescreen(state.raw_text)
            elif req.op == "shadow_nlp":
                # Observation only: the signals live on in the state patch as
                # telemetry, so like areason there is no `result` to return. The
                # CONTAINER decides whether the NLP stack runs, because it is the
                # container that holds the models.
                worker.shadow_nlp(state, enabled=config.SAFETY_NLP_ENABLED and not config.KILL_SWITCH)
            elif req.op == "emit":
                messages = [wire.message_to_wire(worker.emit(state))]
            else:
                out = getattr(worker, req.op)(state)
                if inspect.isawaitable(out):
                    out = await out
                # areason's return value is internal to the worker; its effect is the patch.
                result = None if req.op == "areason" else out
            carry = {attr: getattr(worker, attr) for attr in carry_attrs} if req.op == "run" else {}
            return jsonable_encoder({
                "result": result,
                "state_patch": wire.state_patch(before, state),
                "messages": messages,
                "carry": carry,
                "llm_calls": calls,
                "timing_ms": round((time.perf_counter() - start) * 1000, 2),
            })
        finally:
            if case_token is not None:
                correlation.reset_case(case_token)
            correlation.reset(cid_token)

    return app
