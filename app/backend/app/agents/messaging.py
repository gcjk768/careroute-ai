"""[Platform] Agent-to-agent (A2A) communication substrate.

Today's agents share information *implicitly* by mutating a common `CaseState`.
This module adds an *explicit* communication layer on top of that: every worker
publishes a typed, addressed `AgentMessage` describing its handoff, onto an
in-process `MessageBus`. The bus history becomes an ordered, auditable record of
the agent-to-agent conversation.

Three concepts, mirroring the tool allow-list / AgentContract pattern:

  - `AgentMessage`  one typed handoff (sender, recipient, intent, payload, seq).
  - `AgentComms`    each agent's declared comms interface — the message intents
                    it may PUBLISH and the ones it SUBSCRIBES to. This is
                    least-privilege applied to messaging: an agent cannot emit an
                    intent it did not declare (`enforce_comms`).
  - `MessageBus`    a synchronous, deterministic, ordered pub/sub log. `inbox()`
                    delivers to an agent exactly the messages it subscribed to.

The bus is intentionally in-process and synchronous so the pipeline stays fully
deterministic (a hard requirement for the safety-gated triage order). A
production system could swap `MessageBus` for a real broker (NATS / Redis /
Kafka) without changing any agent's `COMMS` declaration or `emit()` method.
"""
from __future__ import annotations

from dataclasses import dataclass, field

BROADCAST = "broadcast"


class CommsAccessError(RuntimeError):
    """Raised when an agent tries to publish an intent outside its COMMS.publishes."""


@dataclass(frozen=True)
class AgentComms:
    """An agent's declared communication interface (least-privilege for messages).

    `publishes`  = message intents this agent is permitted to SEND.
    `subscribes` = message intents this agent will RECEIVE via `MessageBus.inbox`.
    """

    publishes: frozenset[str]
    subscribes: frozenset[str]


@dataclass
class AgentMessage:
    """One typed agent-to-agent handoff."""

    sender: str
    recipient: str          # an agent SLUG, or BROADCAST for all subscribers
    intent: str             # the message type, e.g. "symptoms.normalised"
    payload: dict = field(default_factory=dict)
    seq: int = 0            # ordered position on the bus (set by MessageBus.publish)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "sender": self.sender,
            "recipient": self.recipient,
            "intent": self.intent,
            "payload": self.payload,
        }


def enforce_comms(agent: object, intent: str) -> None:
    """[AI-Security] Least-privilege enforcement point for messaging.

    Raises `CommsAccessError` if `intent` is not in `agent.COMMS.publishes`, so an
    agent that is refactored to emit an undeclared message type fails loudly at
    runtime instead of silently broadening the conversation.
    """
    comms: AgentComms | None = getattr(agent, "COMMS", None)
    if comms is None or intent not in comms.publishes:
        declared = sorted(comms.publishes) if comms else []
        raise CommsAccessError(
            f"{type(agent).__name__} attempted to publish intent '{intent}', which is "
            f"not in its declared COMMS.publishes {declared!r}."
        )


class MessageBus:
    """In-process, synchronous, ordered pub/sub log for one case.

    Deterministic by construction: messages are appended in publish order and
    numbered with a monotonic `seq`, so the recorded conversation is reproducible
    (no wall-clock, no async races).
    """

    def __init__(self) -> None:
        self._log: list[AgentMessage] = []

    def publish(self, message: AgentMessage) -> AgentMessage:
        message.seq = len(self._log)
        self._log.append(message)
        return message

    def inbox(self, agent: object) -> list[AgentMessage]:
        """Messages delivered to `agent`: addressed to it (or broadcast) AND whose
        intent it declared in `COMMS.subscribes`."""
        comms: AgentComms | None = getattr(agent, "COMMS", None)
        slug = getattr(agent, "SLUG", None)
        if comms is None or slug is None:
            return []
        return [
            m for m in self._log
            if m.intent in comms.subscribes and m.recipient in (slug, BROADCAST)
        ]

    def messages_for_intent(self, intent: str) -> list[AgentMessage]:
        return [m for m in self._log if m.intent == intent]

    def history(self) -> list[dict]:
        """The full ordered conversation as plain dicts (for SSE / audit / tests)."""
        return [m.to_dict() for m in self._log]

    def __len__(self) -> int:
        return len(self._log)


# ---------------------------------------------------------------------------
# [A2A] The RECEIVE side of the protocol.
#
# Publishing alone is a broadcast, not a conversation: if no agent ever acts on
# a message it received, the bus is telemetry and the pipeline would behave
# identically without it. `ConsumesMessages` is the counterpart to `emit()` —
# the Supervisor hands every worker its filtered inbox before calling `run()`,
# so an agent can reason about what its peers ASSERTED, not merely about the
# shared CaseState they happened to leave behind.
#
# Why that distinction matters: CaseState holds only the LATEST value of a
# field. The bus holds what each agent claimed at the moment it acted. A
# downstream agent silently overwriting an upstream decision is invisible in
# the state and visible in the conversation — see
# ReflectionAgent.verify_announcements.
# ---------------------------------------------------------------------------


def require_subscription(agent: object, intent: str) -> None:
    """[AI-Security] Least-privilege on the RECEIVE side, mirroring enforce_comms.

    An agent may only read an intent it declared in `COMMS.subscribes`. Without
    this, `subscribes` would be documentation; with it, an agent that starts
    depending on a message it never declared fails loudly.
    """
    comms: AgentComms | None = getattr(agent, "COMMS", None)
    if comms is None or intent not in comms.subscribes:
        declared = sorted(comms.subscribes) if comms else []
        raise CommsAccessError(
            f"{type(agent).__name__} attempted to read intent '{intent}', which is "
            f"not in its declared COMMS.subscribes {declared!r}."
        )


class ConsumesMessages:
    """Mixin giving every worker the receive half of the A2A protocol.

    The default `consume()` records the inbox and acts on nothing — safe for an
    agent that legitimately has no upstream (Symptom-Intake is first in the
    chain). OVERRIDE it, or read `received_payload()` inside `run()`, to make
    your agent an actual participant.

    `_consumed` distinguishes "the Supervisor delivered an inbox" from "run()
    was called standalone in a unit test", so bus-dependent checks stay off
    when there is no bus.
    """

    def consume(self, inbox: list[AgentMessage]) -> None:
        self.received = list(inbox)
        self._consumed = True

    def received_payload(self, intent: str) -> dict | None:
        """Payload of the most recent received message with `intent`, or None.

        Enforces the subscription declaration, so reading is least-privileged
        in the same way publishing is.
        """
        require_subscription(self, intent)
        for message in reversed(getattr(self, "received", [])):
            if message.intent == intent:
                return message.payload
        return None
