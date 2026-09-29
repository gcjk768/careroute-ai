"""[Microservices] Container entrypoint: `python -m app.microservices.serve <service>`."""
from __future__ import annotations

import argparse

import uvicorn
from fastapi import FastAPI

from .. import correlation
from .common import mark_ready

PORTS: dict[str, int] = {
    "classifier": 8101, "safety": 8102, "routing": 8103,
    "reflection": 8104, "hitl": 8105, "handoff": 8106,
    "llm": 8107, "rag": 8108,
}


def build(service: str) -> FastAPI:
    if service == "llm":
        from .llm_app import build_llm_app

        return build_llm_app()
    if service == "rag":
        from .rag_app import build_rag_app

        return build_rag_app()
    from .agent_app import build_agent_app

    return build_agent_app(service)


def warm(service: str) -> None:
    """Pay the first request's start-up cost now, on the main thread."""
    if service == "classifier":
        from ..ml.model import get_model

        get_model()
    elif service == "rag":
        from .. import rag_embed

        rag_embed.warm()  # MUST be the main thread: see rag_embed.warm
    mark_ready(service)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one CareRoute service container.")
    parser.add_argument("service", choices=sorted(PORTS))
    # A container listens on its own (network-namespaced) interface; exposure is
    # decided by the compose/k8s port mapping, not here.
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104  # nosec B104
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    correlation.install()
    app = build(args.service)
    warm(args.service)
    uvicorn.run(app, host=args.host, port=args.port or PORTS[args.service])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
