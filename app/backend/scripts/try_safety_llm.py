"""Run Safety's bounded LLM adjudicator on a synthetic uncertain case.

    cd backend
    python scripts/try_safety_llm.py

Requires a repository-root .env with OPENAI_API_KEY and
CAREROUTE_SAFETY_LLM=1. Phase 5 stays shadow-only unless the separate Phase 6
activation flag is explicitly enabled.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from pprint import pprint

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(_BACKEND / "tests" / "agents"))

from app.agents import SafetyOverrideAgent  # noqa: E402
from intake_handoff import stage_case  # noqa: E402


async def adjudicate(agent: SafetyOverrideAgent, setup) -> None:
    """Call the asynchronous bounded LLM after the synchronous handoff replay."""
    signal = await agent.areason(setup.state)

    print("\nLLM adjudication")
    pprint(signal.to_dict() if signal else {"result": "No adjudication requested"})

    print("\nSanitized semantic flags")
    pprint(setup.state.semantic_flags)

    result = agent.run(setup.state)
    print("\nSafety result")
    pprint(result)
    print("\nFinal CaseState acuity:", setup.state.acuity_code)


def main() -> None:
    """Exercise a synthetic phrase that needs semantic rather than regex matching."""
    setup = stage_case("safety", "mild-case")
    test_text = (
        "About an hour ago, the left side of my smile suddenly became uneven "
        "and my words started coming out strangely."
    )
    setup.state.raw_text = test_text
    setup.state.normalised_symptoms = test_text
    setup.state.safety_nlp_telemetry = {
        "summary": {"uncertainSignals": 1},
        "signals": [],
    }

    agent = SafetyOverrideAgent()
    setup.deliver(agent)
    asyncio.run(adjudicate(agent, setup))


if __name__ == "__main__":
    main()
