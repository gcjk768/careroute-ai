"""See exactly what YOUR agent receives from upstream, and what it does with it.

    cd backend
    python scripts/try_agent.py                       # every agent, one line each
    python scripts/try_agent.py routing               # one agent, in full
    python scripts/try_agent.py safety --scenario zero-keyword-untranslated
    python scripts/try_agent.py intake "chest pain for two days"

For James, Aaron, Marcus and Heriz. No pytest, no Supervisor, no full pipeline —
this replays the real chain from intake up to your agent, prints the CaseState
you inherit and the A2A inbox you are entitled to, then runs your agent and
shows what changed.

The LLM is disabled throughout, so what you see is reproducible and is also the
shape you get when the ASI10 kill switch is engaged.

Owner: Sham Goh. Backed by tests/agents/intake_handoff.py.
"""
from __future__ import annotations

import argparse
import copy
import sys
from dataclasses import fields
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(_BACKEND / "tests" / "agents"))

# Agent docstrings and message payloads contain non-ASCII (em-dashes, Tamil); a
# default cp1252 console would mangle or raise on them.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass

from app.agents import CaseState  # noqa: E402
from intake_handoff import (  # noqa: E402
    MOCK_INTAKE_OUTPUTS,
    PIPELINE,
    agent_for,
    case_from_real_intake,
    no_llm,
    stage_case,
    _run,
)

RULE = "-" * 72


def _changed(before: CaseState, after: CaseState) -> list[tuple[str, object, object]]:
    return [
        (f.name, getattr(before, f.name), getattr(after, f.name))
        for f in fields(after)
        if f.name != "messages" and getattr(before, f.name) != getattr(after, f.name)
    ]


def show_one(slug: str, scenario: str) -> int:
    """Print the full picture for one agent. Returns a process exit code."""
    setup = stage_case(slug, scenario)
    agent = agent_for(slug)

    print(RULE)
    print(f"AGENT      {slug}   (scenario: {scenario})")
    print(RULE)

    if setup.skipped:
        print(f"! upstream agents still unimplemented on this branch: {setup.skipped}")
        print("  the case below is missing whatever they would have written.\n")

    print("WHAT YOU INHERIT (CaseState, upstream fields only)")
    if not setup.writes_by_stage:
        print("  (nothing — you are first in the chain)")
    for stage, written in setup.writes_by_stage.items():
        print(f"  from {stage:11} {', '.join(written) or '(nothing)'}")
    print()
    for name in ("normalised_symptoms", "detected_language", "intake_keywords",
                 "acuity_code", "confidence", "safety_triggered", "safety_rule",
                 "care_tier", "clinic", "escalated"):
        print(f"  {name:22} = {getattr(setup.state, name)!r}")
    print()

    print(f"YOUR A2A INBOX  (COMMS.subscribes = {sorted(agent.COMMS.subscribes)})")
    if not setup.inbox:
        print("  (empty)")
    for message in setup.inbox:
        print(f"  [{message.seq}] {message.sender} -> {message.recipient}  {message.intent}")
        print(f"      {message.payload}")
    print()

    print("RUNNING YOUR AGENT")
    setup.deliver(agent)
    before = copy.deepcopy(setup.state)
    try:
        with no_llm():
            result = _run(agent, setup.state)
    except NotImplementedError as exc:
        print(f"  TEMPLATE — run() not implemented yet: {exc}")
        print(RULE)
        return 1
    except Exception as exc:
        print(f"  ERROR — {type(exc).__name__}: {exc}")
        print(RULE)
        return 1

    print(f"  result keys   {sorted(result)}")
    missing = set(agent.CONTRACT.returns) - set(result)
    print(f"  declared      {sorted(agent.CONTRACT.returns)}"
          + (f"   MISSING {sorted(missing)}" if missing else "   (all present)"))
    print("  state changes")
    changes = _changed(before, setup.state)
    if not changes:
        print("    (none)")
    for name, was, now in changes:
        lane = "" if name in agent.CONTRACT.writes else "   <-- OUTSIDE YOUR DECLARED LANE"
        print(f"    {name:22} {was!r} -> {now!r}{lane}")

    try:
        message = agent.emit(setup.state)
        print(f"  you publish   {message.intent} -> {message.recipient}")
        print(f"                {message.payload}")
    except NotImplementedError:
        print("  you publish   (emit() not implemented yet)")
    print(RULE)
    return 0 if not missing else 1


def show_all(scenario: str) -> int:
    """One line per agent — the dry-run readiness board."""
    print(RULE)
    print(f"READINESS BOARD   (scenario: {scenario})")
    print(RULE)
    worst = 0
    for slug in PIPELINE[1:]:
        try:
            setup = stage_case(slug, scenario)
            agent = agent_for(slug)
            setup.deliver(agent)
            with no_llm():
                result = _run(agent, setup.state)
            missing = set(agent.CONTRACT.returns) - set(result)
            if missing:
                print(f"  BROKEN    {slug:11} missing declared keys {sorted(missing)}")
                worst = 1
            else:
                print(f"  READY     {slug:11} inbox={[m.intent for m in setup.inbox]}")
        except NotImplementedError:
            print(f"  TEMPLATE  {slug:11} owner has not implemented run() yet")
        except Exception as exc:
            print(f"  ERROR     {slug:11} {type(exc).__name__}: {exc}")
            worst = 1
    print(RULE)
    print("Detail for one agent:  python scripts/try_agent.py <agent>")
    return worst


def show_intake(text: str) -> int:
    """Run MY agent on arbitrary text — what does intake make of this sentence?"""
    state = case_from_real_intake(text)
    print(RULE)
    print("SYMPTOM-INTAKE (deterministic path, LLM disabled)")
    print(RULE)
    print(f"  raw_text             = {state.raw_text!r}")
    print(f"  normalised_symptoms  = {state.normalised_symptoms!r}")
    print(f"  detected_language    = {state.detected_language!r}")
    print(f"  intake_keywords      = {state.intake_keywords!r}")
    message = agent_for("intake").emit(state)
    print(f"  publishes            = {message.intent} -> {message.recipient}")
    print(f"                         {message.payload}")
    if not state.intake_keywords:
        print("\n  NOTE: zero keywords. This is the case most likely to break you —")
        print("        see section 2 of the Intake Handoff Guide.")
    print(RULE)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="See what your agent receives from upstream and what it does with it.",
        epilog="With no arguments, prints the readiness board for every agent.",
    )
    parser.add_argument("agent", nargs="?", choices=sorted(PIPELINE),
                        help="the agent to inspect; omit for all")
    parser.add_argument("text", nargs="?",
                        help="only with 'intake': run my agent on this sentence")
    parser.add_argument("--scenario", default="english-emergency",
                        choices=sorted(MOCK_INTAKE_OUTPUTS),
                        help="which intake output to start the chain from")
    args = parser.parse_args()

    if args.agent == "intake":
        return show_intake(args.text or MOCK_INTAKE_OUTPUTS[args.scenario]["raw_text"])
    if args.agent:
        return show_one(args.agent, args.scenario)
    return show_all(args.scenario)


if __name__ == "__main__":
    raise SystemExit(main())
