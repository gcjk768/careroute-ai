"""Show the agent-to-agent conversation for one case.

    cd backend && python scripts/show_a2a.py
    cd backend && python scripts/show_a2a.py "sore throat for two days"

Prints, for a single triage case:
  1. what each agent RECEIVED from its peers (MessageBus.inbox -> consume())
  2. the ordered conversation on the bus (who told whom what)
  3. the Reflection critic's verdict, including its A2A attribution check

This is the quickest way to see that the agents actually talk to each other
rather than merely sharing a CaseState. Needs a branch where the five member
agents are implemented; on `uat` it will tell you which one is still a template.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Agent docstrings and TODO messages contain em-dashes; a default cp1252 console
# would mangle or raise on them. Ask for UTF-8 and degrade rather than crash.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass

from app.agents.base import CaseState                  # noqa: E402
from app.agents.intake import SymptomIntakeAgent        # noqa: E402

DEFAULT_CASE = "crushing chest pain radiating to my left arm, sweating"
# `intake` appears twice over: it is the first worker AND the agent holding the
# orchestrator role, so `orchestrator.intake is orchestrator`.
ORDER = ("intake", "classifier", "safety", "routing", "hitl", "reflection")


async def _drive(orchestrator: SymptomIntakeAgent, state: CaseState) -> None:
    async def no_delay() -> None:
        return None

    async for _ in orchestrator.orchestrate(
        state, audit=lambda **_kw: None, log=lambda *_a, **_kw: None, delay=no_delay
    ):
        pass


def main() -> int:
    text = " ".join(sys.argv[1:]).strip() or DEFAULT_CASE
    orchestrator = SymptomIntakeAgent()
    state = CaseState(raw_text=text)

    try:
        asyncio.run(_drive(orchestrator, state))
    except NotImplementedError as exc:
        print(f"\n  An agent is still an implementation template:\n    {exc}\n")
        print("  Run this on a branch where the five member agents are implemented,")
        print("  or implement your own agent first. See README -> Agent ownership.\n")
        return 1

    print(f'\ncase: "{text}"')

    print("\n=== 1. what each agent RECEIVED from its peers ===")
    for name in ORDER:
        agent = getattr(orchestrator, name, None)
        if agent is None:
            continue
        intents = [m.intent for m in getattr(agent, "received", [])]
        print(f"  {name:<11} {', '.join(intents) if intents else '(nothing)'}")

    print("\n=== 2. the conversation on the bus ===")
    for message in state.messages:
        print(f"  seq{message['seq']}  {message['sender']:>10} -> "
              f"{message['recipient']:<11} {message['intent']}")

    verdict = state.reflection or {}
    print("\n=== 3. Reflection critic ===")
    print(f"  passed      : {verdict.get('passed')}")
    print(f"  issues      : {verdict.get('issues') or 'none'}")
    print(f"  corrections : {verdict.get('corrections') or 'none'}")
    print("\n  (the A2A attribution check lives in "
          "ReflectionAgent.verify_announcements)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
