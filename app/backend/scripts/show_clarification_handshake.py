"""Show the classifier -> HITL clarifying-question handshake, live.

    python scripts/show_clarification_handshake.py

Companion to `show_a2a.py`, narrowed to the one handoff that is currently
half-finished: the Severity-Classifier (James) proposes ONE clarifying question,
and the Human-in-the-Loop agent (Heriz) receives it and does not yet act on it.

The point of running this rather than reading about it: it separates "the message
never arrived" from "the message arrived and nothing read it". Those look
identical from the outside and need completely different fixes. Today it is the
second — the proposal is in HITL's inbox and the case escalates anyway.

See docs/vault/HITL Clarification Handshake.md.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import HumanInTheLoopAgent, SeverityClassifierAgent  # noqa: E402
from app.agents.base import CONFIDENCE_THRESHOLD, CaseState  # noqa: E402
from app.agents.messaging import MessageBus  # noqa: E402

# Deliberately vague, and deliberately not a red flag: this is the case class the
# whole feature exists for. A confident case has nothing to ask about, and a red
# flag must never be asked about — so neither would show anything here.
COMPLAINT = "i feel unwell and a bit off today"


def main() -> None:
    state = CaseState(raw_text=COMPLAINT)
    state.normalised_symptoms = "feeling unwell, slightly off"
    state.intake_keywords = ["unwell"]

    classifier = SeverityClassifierAgent()
    asyncio.run(classifier.run(state))
    print(f"classifier -> {state.acuity_code} {state.confidence:.3f} | threshold {CONFIDENCE_THRESHOLD}")
    print(f"proposal   -> {state.clarification}")

    # Exactly the two calls the Supervisor makes for every worker; see
    # supervisor._deliver. Nothing here is a test-only shortcut.
    bus = MessageBus()
    bus.publish(classifier.emit(state))
    hitl = HumanInTheLoopAgent()
    hitl.consume(bus.inbox(hitl))

    received = hitl.received_payload("acuity.classified")
    visible = received is not None and received.get("clarification") is not None
    print(f"hitl inbox -> {len(hitl.received)} msg; clarification visible = {visible}")
    print(f"hitl action-> {hitl.run(state)}")

    if visible and state.escalated:
        print(
            "\nGAP: the question was delivered and ignored. HITL's action space is "
            f"{HumanInTheLoopAgent.CAPABILITY.action_space} and must become "
            "escalate / ask / proceed. Owner: Heriz."
        )


if __name__ == "__main__":
    main()
