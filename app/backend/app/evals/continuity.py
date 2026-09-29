"""[Agentic] E14 — MULTI-TURN CONTINUITY of the clarification handshake.

The CI gate table asks for continuity ≥ 0.90 across turns, and CareRoute has
exactly one multi-turn interaction: the Severity-Classifier proposes ONE
clarifying question, the HITL policy node decides to `ask`, and the patient's
answer comes back on the next request (`CaseState.clarifications` — carried on
the request, not in server state, so a resumed case is a stateless replay).
It has never been scored.

Four properties, each scored over every case; the continuity score is their
mean. They are separate on purpose — three of them can hold while the fourth
fails, and it is the fourth that decides whether the conversation meant
anything:

  no_repeat_question   no question is asked twice across the interview. The
                       transcript on the request is the loop-safety property.
  context_retained     turn 2 still reasons about the ORIGINAL complaint. A
                       resumed turn that classifies the answer alone has lost
                       the case.
  terminates           the interview reaches escalate or proceed within
                       INTERVIEW_MAX_QUESTIONS turns (2026-09-26: turn 2 may
                       ask a further question; later turns are answered "not
                       sure" so the budget, not the evidence, must end it). An
                       interview that can ask forever is not an interview.
  answer_used          the answer's clinical content CHANGES the outcome. Scored
                       against a control turn carrying the same handshake with
                       an EMPTY answer: if "yes, I have a fever and I'm
                       struggling to breathe" produces the same decision as
                       saying nothing, the system carried the fact that a
                       question was answered and threw away the answer.

The control run is what makes `answer_used` falsifiable rather than a matter of
opinion: same case, same replay, same one-round cap, and the only difference is
whether the patient said anything.

Run it:  python -m app.evals.continuity
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tests", "fixtures", "continuity_cases.json",
)

PROPERTIES = ("no_repeat_question", "context_retained", "terminates", "answer_used")


def load_cases(path: str = _FIXTURE) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["cases"]


async def _no_delay() -> None:
    return None


async def run_turn(text: str, clarifications: list[dict] | None = None) -> dict:
    """One full pipeline run; returns the observable outcome of that turn."""
    from ..agents import CaseState, SymptomIntakeAgent

    orchestrator = SymptomIntakeAgent()
    state = CaseState(raw_text=text)
    if clarifications:
        state.clarifications = list(clarifications)

    results: dict[str, object] = {}
    async for event in orchestrator.orchestrate(
        state, audit=lambda **_kw: None, log=lambda *_a, **_kw: None, delay=_no_delay,
    ):
        if event.get("event") == "agent_result":
            results[event["agent"]] = event.get("data")

    hitl = results.get("hitl") or {}
    return {
        "acuity": state.acuity_code,
        "confidence": round(float(state.confidence), 4),
        "escalated": bool(state.escalated),
        "action": hitl.get("action") if isinstance(hitl, dict) else None,
        "question": (hitl.get("question") if isinstance(hitl, dict) else None),
        "clarification": (hitl.get("clarification") if isinstance(hitl, dict) else None),
        "normalised": state.normalised_symptoms or state.raw_text,
        "evidence": list(state.evidence),
        "care_tier": state.care_tier,
    }


def _decision(turn: dict) -> tuple:
    """What a patient would actually experience, for comparing two turns."""
    return (turn["acuity"], turn["escalated"], turn["action"], turn["confidence"], turn["care_tier"])


def _overlap(text: str, complaint: str) -> bool:
    """Does the turn still carry the original complaint's content words?"""
    stop = {"a", "an", "the", "and", "or", "my", "i", "me", "to", "of", "in", "on", "for",
            "is", "am", "are", "was", "it", "that", "this", "with", "when", "after", "since"}
    words = {w for w in complaint.lower().split() if w not in stop and len(w) > 2}
    lowered = text.lower()
    return bool(words) and sum(1 for w in words if w in lowered) >= max(1, len(words) // 2)


async def evaluate_case(case: dict, cache: dict | None = None) -> dict:
    """Three runs: turn 1, turn 2 with the answer, and the empty-answer control.

    Turn 1 and the control depend only on the COMPLAINT, not on what the patient
    answered, so cases sharing a complaint share them. The pipeline is seeded and
    deterministic, so this is a cache, not an approximation — and it is worth
    having: one run is several seconds of model, SHAP and retrieval work.
    """
    cache = {} if cache is None else cache
    text = case["text"]

    if text not in cache:
        first = await run_turn(text)
        asked = first.get("clarification") or {}
        handshake = {k: asked.get(k) for k in ("feature", "question", "source", "statement")}
        control = await run_turn(text, [{**handshake, "answer": ""}])
        cache[text] = (first, handshake, control)
    first, handshake, control = cache[text]

    second = await run_turn(text, [{**handshake, "answer": case["answer"]}])

    # [2026-09-26 interview design] Turn 2 may legitimately ask ANOTHER
    # question, so "terminates" is no longer "turn 2 decides": it is "the
    # interview reaches a decision within INTERVIEW_MAX_QUESTIONS turns" when
    # every later question is answered with "not sure" — the answer that adds
    # nothing, so termination is the budget's doing, not the evidence's.
    from ..agents.base import INTERVIEW_MAX_QUESTIONS

    transcript = [{**handshake, "answer": case["answer"]}]
    turn, questions = second, [first.get("question"), second.get("question")]
    while turn["action"] == "ask" and len(transcript) < INTERVIEW_MAX_QUESTIONS + 1:
        asked = turn.get("clarification") or {}
        transcript.append({**{k: asked.get(k) for k in ("feature", "question", "source", "statement")},
                           "answer": "not sure"})
        turn = await run_turn(text, transcript)
        questions.append(turn.get("question"))
    asked_questions = [q for q in questions if q]

    scored = {
        "no_repeat_question": len(asked_questions) == len(set(asked_questions)),
        "context_retained": _overlap(second["normalised"], case["text"]),
        "terminates": turn["action"] in ("escalate", "proceed") and len(transcript) <= INTERVIEW_MAX_QUESTIONS,
        "answer_used": _decision(second) != _decision(control),
    }
    return {
        "id": case["id"],
        "implies": case.get("implies"),
        "asked": bool(first["action"] == "ask"),
        "turn1": first,
        "turn2": second,
        "control": control,
        "properties": scored,
        "score": round(sum(1 for v in scored.values() if v) / len(scored), 4),
    }


async def evaluate_async(cases: list[dict] | None = None) -> dict:
    cases = cases if cases is not None else load_cases()
    cache: dict = {}
    rows = [await evaluate_case(case, cache) for case in cases]
    n = len(rows) or 1
    return {
        "n": len(rows),
        # A case that never reached the ask branch cannot measure a handshake.
        # Reported rather than quietly dropped: a fixture that has stopped
        # triggering the behaviour it tests is a broken evaluation, not a pass.
        "asked_rate": round(sum(1 for r in rows if r["asked"]) / n, 4),
        "continuity": round(sum(r["score"] for r in rows) / n, 4),
        "by_property": {
            p: round(sum(1 for r in rows if r["properties"][p]) / n, 4) for p in PROPERTIES
        },
        "cases": rows,
    }


def evaluate(cases: list[dict] | None = None) -> dict:
    return asyncio.run(evaluate_async(cases))


def main() -> int:
    report = evaluate()
    summary = {k: v for k, v in report.items() if k != "cases"}
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
