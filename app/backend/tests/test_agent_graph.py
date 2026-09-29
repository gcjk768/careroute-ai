"""[AgentOps] Graph lint + loop safety for the triage pipeline.

The course's agent lane gates two structural properties of an agent graph, and
neither is covered by testing the agents individually:

* **Every node is reachable.** A worker that is registered, contract-checked and
  unit-tested but never actually invoked by `orchestrate()` is dead weight that
  still reports green. Reachability is measured from a REAL run, by collecting
  the `agent_result` events the orchestrator emits, so it cannot drift from what
  the pipeline does.
* **Recursion is bounded.** An agent loop with no cap is the failure mode that
  turns one triage into an unbounded spend. CareRoute's only loop is Reflection,
  already capped by `REFLECTION_MAX_ITERS` and `REFLECTION_BUDGET_MS`; these
  tests pin that the cap exists, is small, and is actually honoured end to end.

`MAX_AGENT_STEPS_PER_TRIAGE` is CareRoute's reading of the reference pipeline's
"average turns per task <= 9": there are seven workers, one of which (handoff)
runs only on escalation and one of which (reflection) may iterate, so a single
triage that emits more than nine worker results has looped somewhere it should
not have.
"""
from __future__ import annotations

import pytest

from app.agents import AGENT_LABELS, CaseState, SymptomIntakeAgent
from app.agents.reflection import REFLECTION_MAX_ITERS

# Upper bound on worker results for ONE triage. See module docstring.
MAX_AGENT_STEPS_PER_TRIAGE = 9
# The reference pipeline's graph lint bounds recursion at 12; anything near that
# for a linear triage graph means a loop was introduced without a budget.
MAX_REFLECTION_ITERS = 12

# Representative cases. Together they must exercise every node: the escalating
# case is the only one that reaches `handoff`.
ESCALATING_CASE = "crushing chest pain radiating to my left arm, sweating"
BENIGN_CASE = "mild sore throat for two days, no fever"


async def _no_delay() -> None:
    return None


async def _drive(text: str) -> list[dict]:
    """Run the whole pipeline for one case, collecting SSE events in order."""
    orchestrator = SymptomIntakeAgent()
    state = CaseState(raw_text=text)
    return [
        event
        async for event in orchestrator.orchestrate(
            state, audit=lambda **_kw: None, log=lambda *_a, **_kw: None, delay=_no_delay
        )
    ]


def _result_slugs(events: list[dict]) -> list[str]:
    return [e["agent"] for e in events if e.get("event") == "agent_result"]


@pytest.mark.asyncio
async def test_every_declared_agent_is_reachable():
    """No dead nodes: every slug in AGENT_LABELS runs for some real case."""
    reached = set(_result_slugs(await _drive(ESCALATING_CASE)))
    reached |= set(_result_slugs(await _drive(BENIGN_CASE)))

    unreachable = set(AGENT_LABELS) - reached
    assert not unreachable, f"declared but never invoked by orchestrate(): {sorted(unreachable)}"


@pytest.mark.asyncio
async def test_no_agent_result_uses_an_undeclared_slug():
    """The other direction: no orphan node emitting events with no label."""
    slugs = set(_result_slugs(await _drive(ESCALATING_CASE)))

    undeclared = slugs - set(AGENT_LABELS)
    assert not undeclared, f"emitted by orchestrate() but absent from AGENT_LABELS: {sorted(undeclared)}"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [ESCALATING_CASE, BENIGN_CASE])
async def test_total_worker_steps_per_triage_are_bounded(case):
    """Loop safety: one triage may not exceed the step budget."""
    steps = _result_slugs(await _drive(case))

    assert len(steps) <= MAX_AGENT_STEPS_PER_TRIAGE, (
        f"{len(steps)} worker steps for one triage (budget {MAX_AGENT_STEPS_PER_TRIAGE}): {steps}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [ESCALATING_CASE, BENIGN_CASE])
async def test_no_worker_runs_more_than_the_reflection_budget_allows(case):
    """Only Reflection may repeat, and only up to its declared cap."""
    steps = _result_slugs(await _drive(case))
    counts: dict[str, int] = {}
    for slug in steps:
        counts[slug] = counts.get(slug, 0) + 1

    for slug, n in counts.items():
        budget = REFLECTION_MAX_ITERS if slug == "reflection" else 1
        assert n <= budget, f"{slug} ran {n}x in one triage (budget {budget}): {steps}"


def test_reflection_recursion_cap_is_finite_and_small():
    assert isinstance(REFLECTION_MAX_ITERS, int)
    assert 1 <= REFLECTION_MAX_ITERS <= MAX_REFLECTION_ITERS, (
        f"reflection iteration cap {REFLECTION_MAX_ITERS} outside [1, {MAX_REFLECTION_ITERS}]"
    )
