"""Each worker <-> the orchestrator, one agent at a time. Owner: Sham Goh.

Run with:  pytest -m comms -k orchestrator

WHAT THIS PROVES
----------------
That every agent can hold a two-way conversation with the orchestrator ON ITS
OWN — no other worker running, no full pipeline. If it fails, the failure names
one agent, not "the pipeline broke".

`test_comms.py` checks the messaging primitives and `test_intake_handoff.py`
checks worker-to-worker handoff. Neither drives the round trip through the
orchestrator's own code. This does, using the Supervisor's REAL methods rather
than a reimplementation:

    Supervisor._deliver(bus, agent)    ->  agent gets its filtered inbox
    agent.run(state)                   ->  the worker acts, alone
    agent.emit(state)                  ->  the worker replies
    Supervisor._publish(...)           ->  bus + state.messages + audit trail

Both directions matter. Publishing alone is a broadcast, not a conversation: if
the orchestrator never delivered an inbox, `subscribes` would be decoration, and
if it never recorded the reply, the audit trail would not match what actually
happened.

A NOTE ON NAMING. The orchestrator is now `SymptomIntakeAgent` — it is both the
first worker and the agent that sequences the other five. `Supervisor` survives
only as a deprecated alias for it, which is why this file still imports that
name. `capability.ORCHESTRATOR_SLUG` is the single source of truth for who holds
the role, and the tests below assert the COUNT (exactly one) rather than a
hard-coded name, so they survive the role moving again.

SCOPE. Every assertion here is about the PROTOCOL — was the message delivered,
declared, addressed, recorded, and free of patient text. Nothing asserts what an
agent decided; that belongs to each owner's own evaluation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.agents import CaseState, MessageBus, Supervisor

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app.agents.capability import ORCHESTRATOR_SLUG  # noqa: E402
from intake_handoff import PIPELINE, agent_for, no_llm, stage_case, _run  # noqa: E402

pytestmark = [pytest.mark.comms, pytest.mark.intake]


class _AuditRecorder:
    """Stands in for the Supervisor's hash-chained audit sink."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def __call__(self, **kwargs) -> None:
        self.entries.append(kwargs)

    def details(self) -> str:
        return " ".join(str(e.get("detail", "")) for e in self.entries)


def _round_trip(slug: str, scenario: str = "english-emergency"):
    """Run ONE worker's full exchange with the orchestrator, in isolation.

    Returns (message, bus, state, audit). Raises NotImplementedError untouched so
    callers can skip a worker whose owner has not finished it."""
    setup = stage_case(slug, scenario)
    orchestrator, agent = Supervisor(), agent_for(slug)
    audit = _AuditRecorder()

    # Rebuild the staged conversation on a bus the orchestrator owns, so
    # `_deliver` filters a real history rather than a hand-made list.
    bus = MessageBus()
    for recorded in setup.bus.history():
        bus.publish(_message_from(recorded))

    state: CaseState = setup.state
    inbox = orchestrator.deliver_inbox(bus, agent)      # orchestrator -> agent
    with no_llm():
        _run(agent, state)                       # the agent acts, alone
    message = agent.emit(state)                  # agent -> orchestrator
    orchestrator.publish(bus, state, message, audit)
    return message, bus, state, audit, inbox


def _message_from(recorded: dict):
    from app.agents import AgentMessage
    return AgentMessage(
        sender=recorded["sender"], recipient=recorded["recipient"],
        intent=recorded["intent"], payload=recorded["payload"],
    )


def _skip_if_template(slug: str):
    try:
        return _round_trip(slug)
    except NotImplementedError as exc:
        pytest.skip(f"{slug} is still an implementation template: {exc}")


WORKERS = list(PIPELINE)


@pytest.mark.parametrize("slug", WORKERS)
def test_orchestrator_delivers_only_what_the_agent_subscribed_to(slug: str):
    """[Least privilege, receive side] The orchestrator must hand an agent every
    intent it declared — and nothing it did not. A worker that starts reading a
    message it never declared should fail loudly, not silently widen its inputs."""
    _msg, _bus, _state, _audit, inbox = _skip_if_template(slug)
    declared = agent_for(slug).COMMS.subscribes
    undeclared = {m.intent for m in inbox} - set(declared)
    assert not undeclared, (
        f"orchestrator delivered {sorted(undeclared)} to {slug}, which declares "
        f"subscribes={sorted(declared)}"
    )


@pytest.mark.parametrize("slug", WORKERS)
def test_agent_reply_reaches_the_orchestrator(slug: str):
    """The reply must land in all three places the orchestrator records it: the
    bus (ordering), `state.messages` (the response payload) and the audit trail.
    Missing any one of them means the conversation is not reconstructible after
    the fact, which is the entire point of recording it."""
    message, bus, state, audit, _inbox = _skip_if_template(slug)

    assert message.to_dict() in bus.history(), f"{slug}'s reply never reached the bus"
    assert message.to_dict() in state.messages, f"{slug}'s reply is missing from state.messages"
    assert any(e.get("actor") == slug and e.get("action") == "message" for e in audit.entries), (
        f"{slug}'s reply was not written to the audit trail"
    )


@pytest.mark.parametrize("slug", WORKERS)
def test_agent_only_publishes_an_intent_it_declared(slug: str):
    """[Least privilege, send side] `enforce_comms` should make an undeclared
    intent impossible, so this pins that the declaration matches reality."""
    message, _bus, _state, _audit, _inbox = _skip_if_template(slug)
    agent = agent_for(slug)
    assert message.sender == agent.SLUG
    assert message.intent in agent.COMMS.publishes, (
        f"{slug} published {message.intent!r}; declared {sorted(agent.COMMS.publishes)}"
    )


@pytest.mark.parametrize("slug", WORKERS)
def test_the_exchange_carries_no_patient_text(slug: str):
    """[Privacy] Everything here is hash-chain audited and streamed to the
    browser over SSE, so patient text on the bus is an exposure rather than
    untidiness.

    `symptoms.normalised` is the one exemption, and it is exempt from BOTH
    markers rather than just the normalised one. Intake DERIVES
    `normalised_symptoms` from `raw_text`, so a marker planted in the raw text
    necessarily reappears in the normalised sentence — that is the agent working,
    not leaking. Carrying the patient's cleaned words to the classifier is the
    entire purpose of that message. Every other agent is held to both markers."""
    marker_raw, marker_norm = "zzrawmarker", "zznormmarker"
    setup = stage_case(slug)
    setup.state.raw_text = f"crushing chest pain {marker_raw}"
    setup.state.normalised_symptoms = f"crushing chest pain {marker_norm}."

    orchestrator, agent = Supervisor(), agent_for(slug)
    audit = _AuditRecorder()
    bus = MessageBus()
    for recorded in setup.bus.history():
        bus.publish(_message_from(recorded))

    try:
        orchestrator.deliver_inbox(bus, agent)
        with no_llm():
            _run(agent, setup.state)
        message = agent.emit(setup.state)
    except NotImplementedError as exc:
        pytest.skip(f"{slug} is still an implementation template: {exc}")
    orchestrator.publish(bus, setup.state, message, audit)

    if message.intent == "symptoms.normalised":
        pytest.skip("symptoms.normalised carries the patient's cleaned words by design")

    blob = (str(message.payload) + audit.details()).lower()
    assert marker_raw not in blob, f"{slug} put RAW patient text on the bus/audit"
    assert marker_norm not in blob, f"{slug} put NORMALISED patient text on the bus/audit"


@pytest.mark.parametrize("slug", WORKERS)
def test_agent_survives_the_orchestrator_sending_nothing(slug: str):
    """FAIL-SAFE. A worker must still run when the orchestrator delivers an EMPTY
    inbox — a dropped message, a broker hiccup, or simply being driven standalone
    in someone's unit test.

    An agent that assumes its inbox is populated (`received_payload(...)["x"]`)
    crashes here, and would crash in production the first time a message is lost.
    The fix for a failure is in that agent's file, not in the orchestrator."""
    orchestrator, agent = Supervisor(), agent_for(slug)
    state = stage_case(slug).state
    empty = MessageBus()

    orchestrator.deliver_inbox(empty, agent)   # delivers [] — nothing was published
    try:
        with no_llm():
            result = _run(agent, state)
    except NotImplementedError as exc:
        pytest.skip(f"{slug} is still an implementation template: {exc}")
    assert isinstance(result, dict), f"{slug} did not survive an empty inbox"


def test_exactly_one_agent_orchestrates():
    """The orchestrator role is singular by declaration. If a second agent ever
    claims it, the conversation has two conductors and the safety-gated order
    stops being guaranteed.

    The role now sits on Symptom-Intake rather than a separate Supervisor, so
    this asserts the COUNT and the declared holder, not a hard-coded name."""
    from app.agents.capability import ORCHESTRATOR

    claimants = [
        slug for slug in WORKERS
        if getattr(getattr(agent_for(slug), "CAPABILITY", None), "classification", None) == ORCHESTRATOR
    ]
    assert claimants == [ORCHESTRATOR_SLUG], (
        f"expected exactly one orchestrator ({ORCHESTRATOR_SLUG!r}), found {claimants}"
    )
    assert Supervisor is agent_for(ORCHESTRATOR_SLUG).__class__, (
        "supervisor.Supervisor should be the deprecated alias for the orchestrating agent"
    )


def test_the_orchestrator_is_also_a_worker_in_its_own_pipeline():
    """The consequence of the role moving, pinned so it stays deliberate.

    Symptom-Intake both sequences the pipeline and runs as step 1 of it. That is
    unusual enough to state in a test: `self.intake` must be the orchestrator
    itself, not a second instance — otherwise the object holding the role would
    be running a DIFFERENT intake agent than the one that does the work, and the
    two could drift apart without anything noticing."""
    orchestrator = agent_for(ORCHESTRATOR_SLUG)
    assert orchestrator.intake is orchestrator
    assert orchestrator.SLUG in PIPELINE
    # It still honours its worker contract; orchestrating is a separate method.
    assert set(orchestrator.CONTRACT.writes) == {
        "normalised_symptoms", "detected_language", "intake_keywords",
    }



def test_report_orchestrator_conversation(capsys):
    """READINESS BOARD — who can hold a conversation with the orchestrator.

    See it with:  pytest -m comms -k report_orchestrator -s
    """
    lines = []
    for slug in WORKERS:
        try:
            message, _bus, _state, audit, inbox = _round_trip(slug)
            lines.append(
                f"  OK    {slug:11} in=[{', '.join(m.intent for m in inbox) or '-'}]"
                f"  out={message.intent} -> {message.recipient}  audited={len(audit.entries)}"
            )
        except NotImplementedError:
            lines.append(f"  TEMPLATE  {slug:11} owner has not implemented run() yet")
        except Exception as exc:
            lines.append(f"  ERROR {slug:11} {type(exc).__name__}: {exc}")

    with capsys.disabled():
        print("\nEach agent's conversation with the orchestrator:")
        print("\n".join(lines))
    assert lines


# ---------------------------------------------------------------------------
# The public API contract.
#
# Twelve underscore-prefixed methods were being called from four teammates' test
# files: private by naming convention, public in practice. The six most-used are
# now public names. These two tests make that promise machine-checked rather
# than a docstring nobody re-reads.
# ---------------------------------------------------------------------------
PUBLIC_API = (
    "orchestrate",
    "deliver_inbox",
    "publish",
    "open_message",
    "safety_request",
    "verify_safety_response",
    "apply_safety_failsafe",
    "build_rationale",
    "build_citations",
)

#: old underscore spelling -> supported public name
LEGACY_ALIASES = {
    "_deliver": "deliver_inbox",
    "_publish": "publish",
    "_open_message": "open_message",
    "_safety_request": "safety_request",
    "_verify_safety_response": "verify_safety_response",
    "_apply_safety_failsafe": "apply_safety_failsafe",
}


@pytest.mark.parametrize("name", PUBLIC_API)
def test_public_orchestration_api_exists(name: str):
    """Every name documented as public in orchestration.py's docstring must
    actually be there. If you rename one, this fails — which is the point: four
    people's tests call these."""
    orchestrator = agent_for(ORCHESTRATOR_SLUG)
    assert callable(getattr(orchestrator, name, None)), (
        f"{name}() is documented as public API in orchestration.py but is missing or "
        f"not callable. Renaming it silently breaks teammates' tests."
    )


@pytest.mark.parametrize("old,new", sorted(LEGACY_ALIASES.items()))
def test_legacy_underscore_names_still_work(old: str, new: str):
    """The old private spellings must keep working until the team has migrated.

    They are aliases, not copies — asserting identity means a future edit cannot
    fix one and leave the other behind, which would be worse than either name
    being wrong."""
    orchestrator = agent_for(ORCHESTRATOR_SLUG)
    assert hasattr(orchestrator, old), f"{old}() was removed; teammates' code still calls it"
    assert getattr(type(orchestrator), old) is getattr(type(orchestrator), new), (
        f"{old} should be an alias of {new}, not a separate function — otherwise the two can drift"
    )
