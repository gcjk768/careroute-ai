"""Write the LangGraph adapter's graph (app/agents/graph.py) as Mermaid source.

    cd backend && .venv/Scripts/python scripts/show_graph.py
    node scripts/render-diagrams.mjs        # from the repo root: .mmd -> PNG

The graph is compiled from planner.ALLOWED_TRANSITIONS, so the picture cannot
drift from the code (tests/agents/test_graph.py pins both).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import graph  # noqa: E402
from app.main import orchestrator  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "diagrams" / "src" / "langgraph-pipeline.mmd"

if __name__ == "__main__":
    OUT.write_text(graph.build(orchestrator.new_session()).get_graph().draw_mermaid(), encoding="utf-8")
    print(f"wrote {OUT}")
