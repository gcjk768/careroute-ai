"""[Microservices] What every service container shares: the internal-token
check, the readiness flag and the three probe endpoints."""
from __future__ import annotations

import hmac

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from .. import config, metrics

_READY: set[str] = set()


def mark_ready(name: str) -> None:
    """Called by serve.warm() once the expensive start-up work is done."""
    _READY.add(name)


def check_token(token: str | None) -> None:
    expected = config.INTERNAL_TOKEN
    if expected and not hmac.compare_digest(token or "", expected):
        raise HTTPException(status_code=401, detail="internal token required")


def add_common_routes(app: FastAPI, name: str) -> None:
    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": name}

    @app.get("/ready")
    async def ready() -> dict:
        if name not in _READY:
            raise HTTPException(status_code=503, detail="warming up")
        return {"ready": True, "service": name}

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)
