"""[MLOps] Per-case LLM tracing to Langfuse and/or LangSmith.

Prometheus/Grafana count and time LLM calls; neither stores WHAT was sent or
returned. This module adds that per-case view (docs/design/specs/2026-09-26-
llm-observability-and-langgraph-idea.md):

  agent span   one per worker run, from PipelineOrchestrator._run_worker
  generation   one per served LLM call, from llm.complete (redacted prompt in,
               model output out, model name and latency)

Both sinks are OFF unless their keys are set, and each SDK is imported only
then, so tests, CI and the default stack never touch either. Every call is
wrapped so tracing can never fail a triage.

Trace identity is the case id (correlation.get_case): all spans and
generations of one case share a Langfuse trace id seeded from it, and a
LangSmith `thread_id`, so a trace, an audit entry and a log line share one key.
Spans are created with that explicit id rather than an ambient "current span":
the triage stream is an async generator run through a heartbeat task, and an
ambient context does not survive being carried across its yields.

ponytail: flat per case (agents and generations are siblings under the case,
not nested). Upgrade path: pass the agent observation id as the generation's
parent_span_id via a ContextVar set in _run_worker.

Privacy: llm.complete hands over the prompt AFTER its input guard, i.e. with
identifiers already masked; the raw complaint never reaches a sink from here.
"""
from __future__ import annotations

import contextlib
import logging
from functools import cache

from . import config, correlation

logger = logging.getLogger(__name__)


@cache
def _langfuse():
    if not (config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY):
        return None
    try:
        from langfuse import Langfuse
    except ImportError:
        logger.warning("tracing: LANGFUSE_* set but the langfuse package is not installed")
        return None
    return Langfuse(public_key=config.LANGFUSE_PUBLIC_KEY, secret_key=config.LANGFUSE_SECRET_KEY,
                    host=config.LANGFUSE_HOST)


@cache
def _langsmith():
    if not config.LANGSMITH_API_KEY:
        return None
    try:
        from langsmith import Client
        from langsmith.run_trees import RunTree
    except ImportError:
        logger.warning("tracing: LANGSMITH_API_KEY set but the langsmith package is not installed")
        return None
    return Client(api_key=config.LANGSMITH_API_KEY, api_url=config.LANGSMITH_ENDPOINT), RunTree


def enabled() -> bool:
    return bool(_langfuse() or _langsmith())


def _case() -> str | None:
    case = correlation.get_case()
    return None if case == correlation.UNSET else case


class Span:
    """One open observation in each configured sink; close it with `end`."""

    def __init__(self, lf_obs, ls_run):
        self.lf_obs, self.ls_run = lf_obs, ls_run


def start(name: str, kind: str, inputs: dict | None = None, model: str | None = None) -> Span | None:
    """Open an observation NOW, so the sinks record its real duration. None when tracing is off."""
    if not enabled():
        return None
    case = _case()
    lf_obs = ls_run = None
    lf = _langfuse()
    if lf is not None:
        with contextlib.suppress(Exception):
            lf_obs = lf.start_observation(
                trace_context={"trace_id": lf.create_trace_id(seed=case)} if case else None,
                name=name, as_type=kind, input=inputs, model=model, metadata={"caseId": case},
            )
    ls = _langsmith()
    if ls is not None:
        client, run_tree = ls
        with contextlib.suppress(Exception):
            ls_run = run_tree(
                name=name, run_type="llm" if kind == "generation" else "chain", inputs=inputs or {},
                project_name=config.LANGSMITH_PROJECT, ls_client=client,
                extra={"metadata": {"thread_id": case, "caseId": case, "ls_model_name": model}},
            )
            ls_run.post()
    return Span(lf_obs, ls_run)


def end(span: Span | None, outputs: dict | None = None, error: str | None = None) -> None:
    """Close what `start` opened. Safe on None and on a sink that failed to open."""
    if span is None:
        return
    if span.lf_obs is not None:
        with contextlib.suppress(Exception):
            span.lf_obs.update(output=outputs, level="ERROR" if error else None, status_message=error)
            span.lf_obs.end()
    if span.ls_run is not None:
        with contextlib.suppress(Exception):
            span.ls_run.end(outputs=outputs or {}, error=error)
            span.ls_run.patch()


def generation_start(task: str | None, model: str | None, system: str, prompt: str) -> Span | None:
    """One LLM call. `prompt` must already be redacted (llm.complete's input guard)."""
    return start(f"llm:{task or 'untasked'}", "generation",
                 {"messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
                 model=model)


def agent_start(slug: str) -> Span | None:
    """One worker run. Carries no patient text: timing and outcome only."""
    return start(f"agent:{slug}", "agent")
