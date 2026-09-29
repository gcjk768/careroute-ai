"""Two patients at once — per-request worker isolation.  Run: pytest -m comms

WHAT THIS PROVES
----------------
`orchestrate()` is an async generator: it suspends at every `yield` and every
`await`, so two triages in flight on one event loop INTERLEAVE step by step.
The workers hold per-case mutable state on `self` — `ConsumesMessages.received`
/ `_consumed`, and Safety's `_last_result` / `_last_protocol_issues`, which is
precisely what `emit()` reads to build the `safety.override` message. With ONE
shared worker set (a process-wide `SymptomIntakeAgent`, as `main.py` used to
hold) the interleaving lets patient A's message be built from patient B's run,
and the wrong channel / correlation / review flag then travels into A's audit
trail, SSE stream and API response.

The tests below drive two real orchestrations alternately, one step at a time,
and assert each case's published messages describe only that case.

Written against the construction `main._triage_event_stream` actually uses:
one process-wide template holding the expensive shared dependencies, and
`new_session()` per request. Run against a single shared orchestrator instead,
the first test fails: the cardiac case's `safety.override` reports
`channel="none"` — the benign case's value, because `emit()` read the shared
worker's `_last_result`.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents import CaseState, SymptomIntakeAgent

pytestmark = [pytest.mark.comms, pytest.mark.intake]


def _audit(**_kwargs) -> None:
    """Stands in for the hash-chained audit sink."""


def _log(*_args, **_kwargs) -> None:
    """Stands in for the operator log."""


async def _no_delay() -> None:
    """No UI pacing: these tests assert ordering and attribution, not timing."""


def _cardiac() -> CaseState:
    state = CaseState(raw_text="crushing chest pain spreading to my left arm, cold sweat")
    state.normalised_symptoms = "crushing chest pain radiating to the left arm with sweating"
    return state


def _benign() -> CaseState:
    state = CaseState(raw_text="itchy mild rash on my elbow for two days")
    state.normalised_symptoms = "mild itchy rash on the elbow"
    return state


def _payloads(state: CaseState, intent: str) -> list[dict]:
    return [m["payload"] for m in state.messages if m["intent"] == intent]


def _seq_of(state: CaseState, intent: str) -> int:
    return [m["seq"] for m in state.messages if m["intent"] == intent][-1]


async def _step(generator) -> bool:
    """Advance one orchestration by a single event. False once it is finished."""
    try:
        await generator.__anext__()
    except StopAsyncIteration:
        return False
    return True


async def _interleave(*generators) -> None:
    """Round-robin the generators one event each, the way two concurrent
    requests actually interleave at every suspension point."""
    live = list(generators)
    while live:
        live = [g for g in live if await _step(g)]


def _drive_two_cases() -> tuple[CaseState, CaseState]:
    """Two triages through one process-wide template, interleaved."""
    template = SymptomIntakeAgent()          # what main.py holds, built once
    cardiac, benign = _cardiac(), _benign()

    async def _run() -> None:
        await _interleave(
            template.new_session().orchestrate(cardiac, audit=_audit, log=_log, delay=_no_delay),
            template.new_session().orchestrate(benign, audit=_audit, log=_log, delay=_no_delay),
        )

    asyncio.run(_run())
    return cardiac, benign


def test_interleaved_cases_publish_their_own_safety_override():
    """The message Safety emits must describe the case it was emitted for."""
    cardiac, benign = _drive_two_cases()

    (cardiac_override,) = _payloads(cardiac, "safety.override")
    (benign_override,) = _payloads(benign, "safety.override")

    # The red flag belongs to the cardiac case and to nothing else.
    assert cardiac_override["triggered"] is True
    assert cardiac_override["rule"] is not None
    assert cardiac_override["channel"] == "deterministic"
    assert cardiac_override["requiresHumanReview"] is True

    assert benign_override["triggered"] is False
    assert benign_override["rule"] is None
    assert benign_override["channel"] == "none"
    assert benign_override["requiresHumanReview"] is False


def test_interleaved_cases_correlate_to_their_own_safety_request():
    """`requestSeq` is the whole point of the Phase 2 exchange: it proves the
    response answers THIS case's request, and the route gate fails closed when
    it does not. Safety reads it from `self.received`, so a shared worker
    answers whichever request was delivered most recently.

    Honest about its own limits: two cases that take the same path through the
    pipeline produce identically-numbered buses, so this assertion pins the
    invariant rather than catching that particular bleed. The `channel` check in
    the test above is the one that fails against a shared worker set.
    """
    cardiac, benign = _drive_two_cases()

    for state in (cardiac, benign):
        (override,) = _payloads(state, "safety.override")
        assert override["requestSeq"] == _seq_of(state, "safety.assessment.requested")


def test_interleaved_cases_route_on_their_own_acuity():
    """Care-Routing decides the tier from the `safety.override` it was handed,
    so the same bleed sends a rash to the Emergency Department (or, worse, a
    cardiac case to a GP)."""
    cardiac, benign = _drive_two_cases()

    assert cardiac.care_tier == "Emergency Department"
    assert _payloads(cardiac, "care.routed")[-1]["care_tier"] == "Emergency Department"

    assert benign.care_tier != "Emergency Department"
    assert _payloads(benign, "care.routed")[-1]["care_tier"] == benign.care_tier
    assert benign.safety_triggered is False
    assert benign.acuity_code != "P1_RESUSCITATION"


def test_a_session_rebuilds_the_workers_but_shares_the_expensive_dependencies():
    """The other half of the fix: isolation must not cost a CHAS dataset
    download, an hours-snapshot parse and a fresh OneMap authentication on every
    request. Only the per-case state is rebuilt."""
    template = SymptomIntakeAgent()
    session = template.new_session()

    # Per-case state: rebuilt.
    assert session is not template
    for worker in ("classifier", "safety", "routing", "hitl", "reflection", "handoff"):
        assert getattr(session, worker) is not getattr(template, worker)
    # The engine still treats intake as itself, not as the template.
    assert session.intake is session

    # Expensive, case-independent: shared.
    assert session.routing.lookup is template.routing.lookup
    assert session.routing.hours is template.routing.hours
    assert session.routing.maps is template.routing.maps
    assert session.routing._route_cache is template.routing._route_cache
    assert session.safety.semantic is template.safety.semantic


def test_a_reflection_rerun_does_not_accuse_itself_of_an_unannounced_reroute():
    """[A2A] The re-run re-runs Care-Routing. Reflection then verifies the tier
    against what Care-Routing ANNOUNCED — so a re-route that never republished
    `care.routed` was reported as "changed downstream without being announced":
    the critic raising an issue the loop itself had just created, on a case
    whose only real problem was thin evidence."""
    template = SymptomIntakeAgent()
    # Thin evidence with no keyword match is what triggers the rerun.
    state = CaseState(raw_text="i feel a bit off today, hard to describe")
    state.normalised_symptoms = "feeling generally unwell, no specific symptom"
    # The interview budget is already spent, so HITL cannot ask and the
    # thin-evidence re-run fires (an ask is a terminal state for the turn).
    from app.agents.base import INTERVIEW_MAX_QUESTIONS

    state.clarifications = [
        {"feature": f"d{i}", "question": f"q{i}?", "answer": "not sure", "source": "fallback"}
        for i in range(INTERVIEW_MAX_QUESTIONS)
    ]

    async def _run() -> None:
        async for _event in template.new_session().orchestrate(
            state, audit=_audit, log=_log, delay=_no_delay,
        ):
            pass

    asyncio.run(_run())

    assert state.reflection["loop"]["iterations"] >= 1, "the re-run never happened"
    # The re-run raised the acuity, so it re-routed — and said so on the bus.
    assert len(_payloads(state, "care.routed")) == 2, "the re-route was never announced"
    assert _payloads(state, "care.routed")[-1]["care_tier"] == state.care_tier
    for issue in state.reflection["issues"]:
        assert "never announced" not in issue, issue
        assert "changed downstream" not in issue, issue
