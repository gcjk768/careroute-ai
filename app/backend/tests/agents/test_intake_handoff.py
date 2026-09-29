"""Symptom-Intake -> downstream handoff harness. Owner: Sham Goh.

Run with:  pytest -m intake

WHAT THIS IS FOR
----------------
`test_intake.py` proves intake produces the right fields. This file proves those
fields are USABLE — it hands intake's output to each downstream worker
INDEPENDENTLY, with mock data, so a failure names one agent instead of "the
pipeline broke somewhere".

THE HELPERS LIVE NEXT DOOR. Import them from `tests/agents/intake_handoff.py`,
which is a plain module — importing it collects and runs nothing:

    from tests.agents.intake_handoff import case_from_intake, stage_case, drive

This file is the half that ASSERTS. It exists to keep those helpers honest: that
the mocks still match what my agent really emits, that the replayed Supervisor
messages still match what the Supervisor really sends, and that every downstream
worker survives the handoff — including the empty-keyword case that is most
likely to break someone.

Three things it gives you that a full end-to-end run does not:

1. ISOLATION. Each downstream agent is driven on its own. You can test against
   the Severity-Classifier without the Safety-Override, Care-Routing, HITL or
   Reflection workers existing at all.
2. IT WORKS MID-BUILD. Any agent still raising NotImplementedError is reported
   as SKIPPED WITH A REASON rather than failing, so this harness is useful
   before your teammates finish — and it starts asserting the moment they do,
   with no edit here.
3. A READINESS BOARD. `test_report_downstream_readiness` prints which agents are
   implemented, so before a dry run you can see in one command what will
   actually work.

SCOPE — WHY THIS IS NOT REACHING INTO ANOTHER OWNER'S LANE
----------------------------------------------------------
This file tests THIS agent's OUTPUT CONTRACT, not other agents' logic. It never
asserts that the classifier picked the right acuity or that routing chose a good
clinic — those belong to E4 and E3, owned by James and Marcus. It asserts only
that a downstream worker, given a realistic intake result, RUNS and stays inside
its own declared lane. The question being answered is "is my handoff good?",
which is squarely an intake question.

It is also read-only with respect to other agents: no other agent's file, tests
or fixtures are touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.agents import CaseState, SeverityClassifierAgent, Supervisor, SymptomIntakeAgent

# `conftest.py` already puts this directory on the path for pytest runs. Repeat it
# here so `from tests.agents.test_intake_handoff import case_from_intake` — the
# import an earlier version of the handoff guide told the team to use — still
# resolves when imported OUTSIDE pytest, from a script or the REPL.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from intake_handoff import (  # noqa: E402, F401 — re-exported for backwards compatibility
    DOWNSTREAM_AGENTS,
    MOCK_INTAKE_OUTPUTS,
    PIPELINE,
    StageSetup,
    agent_for,
    case_from_intake,
    case_from_real_intake,
    drive,
    message_from_intake,
    no_llm,
    stage_case,
    _run,
)

pytestmark = pytest.mark.intake


def _run_or_skip(agent, state: CaseState, slug: str):
    """Run a downstream agent, SKIPPING if its owner has not implemented it yet.

    This is what lets the harness be useful before the team finishes. It skips
    ONLY on NotImplementedError — any other exception is a genuine failure of the
    handoff and is allowed to propagate."""
    try:
        return _run(agent, state)
    except NotImplementedError as exc:
        pytest.skip(f"{slug} is still an implementation template: {exc}")


# ---------------------------------------------------------------------------
# The handoff assertions.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("slug", sorted(DOWNSTREAM_AGENTS))
@pytest.mark.parametrize("scenario", sorted(MOCK_INTAKE_OUTPUTS))
def test_downstream_agent_accepts_intake_output(slug: str, scenario: str):
    """Every downstream worker must RUN on intake's output and return its own
    declared keys. This is the handoff contract, agent by agent."""
    agent = DOWNSTREAM_AGENTS[slug]()
    state = case_from_intake(scenario)
    result = _run_or_skip(agent, state, slug)

    assert isinstance(result, dict), f"{slug}.run() returned {type(result).__name__}, expected dict"
    missing = set(agent.CONTRACT.returns) - set(result)
    assert not missing, f"{slug}.run() omitted declared keys {sorted(missing)} on scenario {scenario!r}"


@pytest.mark.parametrize("slug", sorted(DOWNSTREAM_AGENTS))
def test_downstream_agent_survives_empty_keywords(slug: str):
    """THE CASE MOST LIKELY TO BREAK SOMEONE.

    When the LLM is unavailable and the input is not English, intake emits an
    EMPTY keyword list and an untranslated sentence — by design, and the E1
    evaluation measures exactly that. A downstream agent that assumes
    `intake_keywords` is non-empty (e.g. `state.intake_keywords[0]`) will crash
    in production the first time the kill switch is engaged.

    If this fails for your agent, the fix is in your file, not in intake."""
    agent = DOWNSTREAM_AGENTS[slug]()
    state = case_from_intake("zero-keyword-untranslated")
    result = _run_or_skip(agent, state, slug)
    assert isinstance(result, dict)


def test_intake_message_payload_matches_what_classifier_subscribes_to():
    """[A2A] The declared handoff: intake publishes `symptoms.normalised` and the
    classifier is the subscriber. Checks the wiring lines up, without running the
    classifier — so it holds while that agent is still a template."""
    intake = SymptomIntakeAgent()
    state = case_from_real_intake("I have crushing chest pain and I cannot breathe.")
    message = intake.emit(state)

    assert message.intent in SeverityClassifierAgent.COMMS.subscribes, (
        f"intake publishes {message.intent!r} but the classifier subscribes to "
        f"{sorted(SeverityClassifierAgent.COMMS.subscribes)} — the handoff is not wired up."
    )
    assert message.recipient == SeverityClassifierAgent.SLUG
    assert set(message.payload) == {"normalised_symptoms", "detected_language", "keywords"}


def test_message_from_intake_matches_a_real_emit():
    """`message_from_intake()` is the shortcut James imports. It must produce the
    same thing as running my agent and calling emit() by hand."""
    shortcut = message_from_intake("english-emergency")
    by_hand = SymptomIntakeAgent().emit(case_from_intake("english-emergency"))
    assert shortcut.intent == by_hand.intent
    assert shortcut.recipient == by_hand.recipient
    assert shortcut.payload == by_hand.payload


def test_mock_data_has_not_drifted_from_real_intake():
    """Mocks rot. This re-runs the real agent on the same text and checks the
    mock still matches, so a change to intake cannot silently invalidate every
    handoff test in this file."""
    mock = MOCK_INTAKE_OUTPUTS["english-emergency"]
    state = case_from_real_intake(mock["raw_text"])
    assert state.detected_language == mock["detected_language"]
    assert set(state.intake_keywords) == set(mock["intake_keywords"]), (
        f"real intake now emits {state.intake_keywords}, but the mock says "
        f"{mock['intake_keywords']}. Update MOCK_INTAKE_OUTPUTS."
    )


def test_intake_text_does_not_leak_into_downstream_message_payloads():
    """[Privacy] My normalised sentence must not travel further than my own message.

    Requested by the Safety-Override Phase 2 handoff plan
    (`docs/plans/safety-override-supervisor-phase-2-handoff.md`), which requires
    that no A2A payload or audit entry carries raw or normalised patient text.

    The intake-side half of that guarantee is mine, because `normalised_symptoms`
    is my field: I am the one who decides what goes into it, so I am the one who
    should notice if it starts appearing where it should not. The Safety-side
    half stays in `test_safety.py` where Aaron owns it.

    `symptoms.normalised` is exempt — carrying the normalised sentence to the
    classifier is the entire purpose of that message.
    """
    marker_raw = "zzmarkerraw"
    marker_norm = "zzmarkernorm"
    state = case_from_intake("english-emergency")
    state.raw_text = f"I have crushing chest pain {marker_raw}"
    state.normalised_symptoms = f"I have crushing chest pain {marker_norm}."

    leaks: list[str] = []
    for slug, cls in DOWNSTREAM_AGENTS.items():
        agent = cls()
        try:
            _run(agent, state)
            payload = str(agent.emit(state).payload).lower()
        except NotImplementedError:
            continue  # still a template — nothing to leak yet
        for name, marker in (("raw_text", marker_raw), ("normalised_symptoms", marker_norm)):
            if marker in payload:
                leaks.append(f"{slug} published {name}")

    assert not leaks, (
        f"patient text reached a downstream A2A payload: {leaks}. Message payloads are written "
        f"to the audit trail and streamed to the browser, so this is a PHI exposure, not a "
        f"tidiness issue."
    )


# ---------------------------------------------------------------------------
# stage_case() — the part Aaron, Marcus and Heriz depend on.
#
# `case_from_intake()` only populates MY three fields, which is honest for the
# classifier and misleading for anyone further down: they would be testing
# against `acuity_code="P3_URGENT"` and `confidence=0.5`, the dataclass
# defaults, not a classified case. These tests guard the builder that replays
# the real upstream chain instead.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("slug", [s for s in PIPELINE if s != "intake"])
def test_stage_case_really_ran_every_upstream_stage(slug: str):
    """The case handed to an agent must have been PROCESSED by every stage
    before it, not left at CaseState defaults.

    Proved from `writes_by_stage`, which records what each replayed stage
    actually changed. Comparing against defaults would not work — a field can
    legitimately equal its default (`detected_language == "en"`), and that is a
    real value, not a missing handoff."""
    setup = stage_case(slug)
    if setup.skipped:
        pytest.skip(f"upstream still a template on this branch: {setup.skipped}")

    for earlier in PIPELINE[:PIPELINE.index(slug)]:
        assert setup.writes_by_stage.get(earlier), (
            f"stage_case({slug!r}) ran {earlier} but it changed nothing, so the case "
            f"handed to {slug} is not a real handoff. Recorded: {setup.writes_by_stage}"
        )


@pytest.mark.parametrize("slug", [s for s in PIPELINE if s != "intake"])
def test_stage_case_upstream_stays_inside_its_own_lane(slug: str):
    """No replayed stage may write outside its declared CONTRACT.writes.

    `test_contracts.py` already enforces this per agent; the point here is
    narrower — that MY staged case is a faithful reproduction of the pipeline
    and not something my replay corrupted along the way."""
    setup = stage_case(slug)
    if setup.skipped:
        pytest.skip(f"upstream still a template on this branch: {setup.skipped}")

    for earlier, written in setup.writes_by_stage.items():
        illegal = set(written) - set(agent_for(earlier).CONTRACT.writes)
        assert not illegal, (
            f"replaying {earlier} wrote {sorted(illegal)}, outside its declared lane. "
            f"Either that agent broke its contract or stage_case is staging it wrong."
        )


@pytest.mark.parametrize("slug", [s for s in PIPELINE if s != "intake"])
def test_stage_case_delivers_the_inbox_the_agent_subscribed_to(slug: str):
    """[A2A] An agent that reads `received_payload()` has that path untested
    without an inbox. `stage_case` must deliver every intent the agent declared
    in COMMS.subscribes and that the upstream chain actually produces."""
    setup = stage_case(slug)
    if setup.skipped:
        pytest.skip(f"upstream still a template on this branch: {setup.skipped}")

    agent = agent_for(slug)
    delivered = set(setup.intents())
    assert delivered, f"{slug} got an empty inbox; it subscribes to {sorted(agent.COMMS.subscribes)}"

    undeclared = delivered - set(agent.COMMS.subscribes)
    assert not undeclared, (
        f"{slug} was delivered {sorted(undeclared)}, which it never declared in "
        f"COMMS.subscribes — MessageBus.inbox() should have filtered those out."
    )


@pytest.mark.parametrize("slug", [s for s in PIPELINE if s != "intake"])
@pytest.mark.parametrize("scenario", sorted(MOCK_INTAKE_OUTPUTS))
def test_drive_runs_each_agent_on_a_realistic_case(slug: str, scenario: str):
    """The one-call entry point teammates use. Every agent must run to completion
    on a fully-populated upstream case, with its inbox delivered, and return its
    declared keys — on all four scenarios including the empty-keyword one."""
    setup = stage_case(slug, scenario)
    if setup.skipped:
        pytest.skip(f"upstream still a template on this branch: {setup.skipped}")

    try:
        result, _ = drive(slug, scenario)
    except NotImplementedError as exc:
        pytest.skip(f"{slug} is still an implementation template: {exc}")

    missing = set(agent_for(slug).CONTRACT.returns) - set(result)
    assert not missing, f"{slug}.run() omitted declared keys {sorted(missing)} on {scenario!r}"


def test_replayed_supervisor_messages_match_the_real_supervisor():
    """DRIFT GUARD. `stage_case` replays `case.opened` and
    `safety.assessment.requested` by hand rather than importing the Supervisor,
    so it keeps working on a branch without Phase 2. That copy can rot — if
    James changes either payload, this fails and I update the replay."""
    setup = stage_case("safety")
    replayed = {
        intent: setup.bus.messages_for_intent(intent)[-1]
        for intent in ("case.opened", "safety.assessment.requested")
    }

    supervisor = Supervisor()
    real_open = supervisor._open_message(setup.state)
    assert set(replayed["case.opened"].payload) == set(real_open.payload), (
        "Supervisor._open_message payload changed; update _publish_supervisor_message."
    )
    assert replayed["case.opened"].recipient == real_open.recipient

    real_request = supervisor._safety_request(setup.state, classifier_seq=0)
    assert set(replayed["safety.assessment.requested"].payload) == set(real_request.payload), (
        "Supervisor._safety_request payload changed; update _publish_supervisor_message."
    )
    assert replayed["safety.assessment.requested"].recipient == real_request.recipient


def test_safety_request_correlates_to_the_real_classifier_message():
    """[Phase 2] Aaron's agent checks that `classifierMessageSeq` points at the
    `acuity.classified` it actually saw. A replay with a wrong seq would make his
    tests pass for the wrong reason, so pin the correlation here."""
    setup = stage_case("safety")
    if setup.skipped:
        pytest.skip(f"upstream still a template on this branch: {setup.skipped}")

    request = next(m for m in setup.inbox if m.intent == "safety.assessment.requested")
    classified = next(m for m in setup.inbox if m.intent == "acuity.classified")
    assert request.payload["classifierMessageSeq"] == classified.seq
    assert request.payload["classifierAcuity"] == classified.payload["acuity_code"]


def test_stage_case_carries_no_patient_text_in_supervisor_messages():
    """[Privacy] The replayed Supervisor messages must be as clean as the real
    ones. If the replay leaked raw text, every downstream privacy test built on
    this harness would be checking a payload that was never realistic."""
    setup = stage_case("reflection", "english-emergency")
    raw = setup.state.raw_text.lower()
    norm = setup.state.normalised_symptoms.lower()

    # Match on INTENT, not sender. Filtering by sender == "supervisor" used to
    # work and silently stopped: orchestration moved onto intake, so no message
    # has that sender any more and this loop would have run zero times while
    # still reporting green. A privacy test that checks nothing is worse than no
    # privacy test, so the count below is asserted too.
    checked = 0
    for message in setup.bus.history():
        if message["intent"] not in {"case.opened", "safety.assessment.requested"}:
            continue
        checked += 1
        payload = str(message["payload"]).lower()
        assert raw not in payload and norm not in payload, (
            f"replayed orchestrator message {message['intent']!r} carries patient text"
        )
    assert checked == 2, (
        f"expected to screen both orchestrator messages, screened {checked}. "
        f"If an intent was renamed, this test stopped covering it."
    )


def test_stage_case_rejects_an_unknown_agent():
    """A typo should name the pipeline, not fail somewhere confusing later."""
    with pytest.raises(KeyError, match="unknown agent"):
        stage_case("saftey")  # deliberate typo


def test_report_downstream_readiness(capsys):
    """THE DRY-RUN READINESS BOARD.

    Prints which downstream agents are implemented AND whether each one survives
    a realistic staged case with its inbox delivered. Never fails — it is a
    status report, not a gate; whether a teammate has finished is not an intake
    defect.

    See it with:  pytest -m intake -k readiness -s
    """
    lines = []
    for slug in PIPELINE[1:]:
        try:
            setup = stage_case(slug)
            drive(slug)
            note = f"inbox={setup.intents()}"
            if setup.skipped:
                note += f"  upstream-skipped={setup.skipped}"
            lines.append(f"  READY     {slug:11} {note}")
        except NotImplementedError:
            lines.append(f"  TEMPLATE  {slug:11} (owner has not implemented run() yet)")
        except Exception as exc:  # a real defect in their agent, not our concern
            lines.append(f"  ERROR     {slug:11} ({type(exc).__name__}: {exc})")

    with capsys.disabled():
        print("\nDownstream readiness for the intake handoff:")
        print("\n".join(lines))
    assert lines
