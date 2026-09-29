"""[MLOps][Responsible-AI] The evaluation PLAN itself is tested — Point 2.

Proposal review (Junhua, 2026-07-24) Point 2 asked us to expand the evaluation
plan and, for each evaluation, to define the test dataset, expected outputs,
evaluation metrics, and acceptance criteria.

A plan written in a document degrades silently: datasets get renamed, thresholds
drift, an evaluation is quietly dropped. So the plan lives in `app/evals/plan.py`
as data, and this file enforces it on every CI run:

  * all six evaluations the reviewer listed are present,
  * each defines all four mandatory things,
  * anything claiming to be implemented names a test that EXISTS and a dataset
    file that EXISTS,
  * anything still planned says what it is blocked on, so a gap stays visible
    instead of being mistaken for coverage.

Run with: pytest -m eval
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.evals import EVALUATION_PLAN, IMPLEMENTED, PLANNED, validate_plan
from app.evals.spec import EvalSpecError, validate_spec
from app.evals.plan import E5_HITL_TRIGGER

pytestmark = pytest.mark.eval


def test_e7_states_the_real_red_flag_category_count():
    """E7 said "seven" categories long after RED_FLAG_RULES grew to eleven."""
    from app.evals.plan import E7_SAFETY_CONTEXT
    from app.redflags import RED_FLAG_RULES

    text = " ".join(E7_SAFETY_CONTEXT.acceptance_criteria)
    assert "seven" not in text
    assert f"all {len(RED_FLAG_RULES)} RED_FLAG_RULES categories" in text

_BACKEND = Path(__file__).resolve().parents[1]

#: The six evaluations named in the review, verbatim. If the plan stops covering
#: one of these, that is a regression against reviewer feedback, not a refactor.
REVIEWER_REQUESTED = {
    "E1-intake-extraction",
    "E2-clarifying-questions",
    "E3-routing-accuracy",
    "E4-severity-classification",
    "E5-hitl-trigger",
    "E6-tool-selection",
}


def test_plan_is_internally_valid():
    """Every spec defines dataset + expected outputs + metrics + acceptance
    criteria, ids are unique, and implemented specs point at real artifacts."""
    validate_plan(EVALUATION_PLAN)


def test_plan_covers_every_evaluation_the_reviewer_asked_for():
    ids = {spec.id for spec in EVALUATION_PLAN}
    missing = REVIEWER_REQUESTED - ids
    assert not missing, f"evaluation plan no longer covers reviewer-requested evaluations: {sorted(missing)}"


@pytest.mark.parametrize("spec", EVALUATION_PLAN, ids=[s.id for s in EVALUATION_PLAN])
def test_every_spec_names_an_owner_and_an_agent(spec):
    """One owner per evaluation, mirroring one owner per agent — so no evaluation
    is everybody's job and therefore nobody's."""
    assert spec.owner.strip()
    assert spec.agent.strip()


@pytest.mark.parametrize("spec", EVALUATION_PLAN, ids=[s.id for s in EVALUATION_PLAN])
def test_implemented_specs_point_at_real_files(spec):
    """A spec may only claim 'implemented' if the test and dataset actually exist."""
    if spec.status != IMPLEMENTED:
        return
    assert spec.dataset_exists, f"{spec.id}: dataset {spec.dataset} not found at {spec.dataset_path}"
    for module in spec.implemented_by:
        assert (_BACKEND / module).is_file(), f"{spec.id}: implemented_by names a missing file {module}"


@pytest.mark.parametrize("spec", EVALUATION_PLAN, ids=[s.id for s in EVALUATION_PLAN])
def test_planned_specs_declare_what_blocks_them(spec):
    """An unexplained gap is how an evaluation plan rots into a wish list."""
    if spec.status == PLANNED:
        assert len(spec.blocked_on.strip()) > 40, (
            f"{spec.id} is planned but does not meaningfully say what blocks it."
        )


def test_at_least_two_reference_implementations_exist():
    """The remaining owners need something concrete to copy. If these ever drop
    below two, the 'template' framing in app/evals/plan.py is no longer true."""
    implemented = [s for s in EVALUATION_PLAN if s.status == IMPLEMENTED]
    assert len(implemented) >= 2, (
        f"only {len(implemented)} evaluation(s) implemented; the plan promises worked examples "
        f"for the other owners to follow."
    )


def test_hitl_spec_requires_both_recall_and_specificity():
    """The specific gap this review exposed: measuring recall alone lets a system
    pass by escalating everything. Pin it so it cannot be quietly removed."""
    criteria = " ".join(E5_HITL_TRIGGER.acceptance_criteria).lower()
    assert "recall" in criteria
    assert "specificity" in criteria


def test_validation_actually_rejects_an_incomplete_spec():
    """Negative test — without this, every assertion above could pass vacuously."""
    from app.evals import EvalSpec

    incomplete = EvalSpec(
        id="bad", name="bad", owner="nobody", agent="intake",
        dataset="tests/fixtures/does_not_matter.json",
        expected_output="something",
        metrics=(),                       # <- missing
        acceptance_criteria=("x",),
        status=PLANNED, blocked_on="x" * 50,
    )
    with pytest.raises(EvalSpecError, match="metrics"):
        validate_spec(incomplete)
