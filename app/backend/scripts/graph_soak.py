"""Run the 100 soak scenarios through the LangGraph adapter AND the supervisor,
and report whether they agree.

    cd backend && .venv/Scripts/python scripts/graph_soak.py [--out results.json]

Same 100 cases as the Playwright soak (frontend/tests/e2e/fixtures/soak_scenarios.json),
first turn only. Each case goes through the API's own front door first — guardrail
screen, then PII masking — so a blocked injection is blocked for both paths.

The LLM is OFF (deterministic), so any disagreement is a real divergence between
app/agents/graph.py and orchestration.py, not model noise. With LANGFUSE_* set (and
`langchain` installed) every graph run is also traced to Langfuse through its
LangGraph callback, grouped in one session.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.llm as llm  # noqa: E402


async def _llm_off(*_a, **_k):
    raise llm.LLMUnavailableError("graph_soak: LLM off for a deterministic comparison")


llm.complete = _llm_off

from app import guardrail, redact  # noqa: E402
from app.agents import graph, planner  # noqa: E402
from app.agents.base import CaseState  # noqa: E402
from app.main import orchestrator  # noqa: E402

SCENARIOS = ROOT.parent / "frontend" / "tests" / "e2e" / "fixtures" / "soak_scenarios.json"


async def _supervisor(state: CaseState) -> list[str]:
    session = orchestrator.new_session()

    async def _delay():
        return None

    async for _ in session.orchestrate(state, audit=lambda *a, **k: None, log=lambda *a, **k: None, delay=_delay):
        pass
    steps = [s["step"] for s in session.plan["steps"]]
    return steps[:steps.index(planner.HITL) + 1] if state.clarification_asked else steps


def _callbacks(session_id: str) -> dict | None:
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    from langfuse.langchain import CallbackHandler
    return {"callbacks": [CallbackHandler()],
            "metadata": {"langfuse_session_id": session_id, "langfuse_tags": ["langgraph", "soak"]}}


async def main(out: Path) -> int:
    rows = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    session_id = f"langgraph-soak-{time.strftime('%Y%m%d-%H%M%S')}"
    results = []
    for i, row in enumerate(rows, 1):
        if guardrail.screen(row["text"]).status == "blocked":
            results.append({"i": i, **row, "outcome": "blocked", "agree": True})
            continue
        text, _ = redact.redact(row["text"])
        sup_state = CaseState(case_id=f"sup-{i:03d}", raw_text=text)
        want = await _supervisor(sup_state)
        g_state = CaseState(case_id=f"graph-{i:03d}", raw_text=text)
        config = _callbacks(session_id)
        if config:
            config["run_name"] = f"langgraph:{row['kind']}:{i:03d}"
        started = time.perf_counter()
        out_state = await graph.run_case(orchestrator.new_session(), g_state, config)
        got = out_state["visited"][len(graph.PREFIX) + 1:]
        agree = got == want and g_state.acuity_code == sup_state.acuity_code and g_state.escalated == sup_state.escalated
        results.append({
            "i": i, **row, "outcome": "decided", "agree": agree, "path": out_state["visited"],
            "supervisorSteps": want, "acuity": g_state.acuity_code, "supervisorAcuity": sup_state.acuity_code,
            "escalated": g_state.escalated, "asked": g_state.clarification_asked,
            "graphMs": round((time.perf_counter() - started) * 1000, 1),
        })
        print(f"{i:3d} {row['kind']:11} {'OK ' if agree else 'DIFF'} {g_state.acuity_code or '-':17} {' > '.join(got)}")
    if config := _callbacks(session_id):
        from langfuse import get_client
        get_client().flush()
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    decided = [r for r in results if r["outcome"] == "decided"]
    agree = sum(r["agree"] for r in results)
    print(f"\n{agree}/{len(results)} agree ({len(results) - len(decided)} blocked at the guardrail); "
          f"session {session_id}; wrote {out}")
    return 0 if agree == len(results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "graph_soak.json")
    raise SystemExit(asyncio.run(main(ap.parse_args().out)))
