"""[MLOps][Responsible-AI] The evaluation SPEC type — the reviewer's Point 2,
expressed as data instead of prose.

Proposal review (Junhua, 2026-07-24), Point 2:

    "Please expand the agent and system evaluation plan... For each evaluation,
     please define the test dataset, expected outputs, evaluation metrics, and
     acceptance criteria."

Those four things are mandatory fields on `EvalSpec` below. `validate_spec`
rejects a spec that leaves any of them empty, and `tests/test_eval_plan.py` runs
that validation over every entry in `app/evals/plan.py`. An evaluation plan that
is incomplete therefore fails the build rather than quietly shipping.

The spec also records what prose cannot: whether the evaluation is actually
IMPLEMENTED and which test proves it. `status="planned"` is a legitimate,
honest state — several of these need a dataset or a feature that does not exist
yet — but a planned spec must still define all four mandatory fields, so the
plan is complete even where the implementation is not.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

#: Repo-relative root used to resolve `dataset` paths (backend/).
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IMPLEMENTED = "implemented"
PLANNED = "planned"
VALID_STATUSES = frozenset({IMPLEMENTED, PLANNED})


class EvalSpecError(ValueError):
    """Raised when an evaluation spec is incomplete or internally inconsistent."""


@dataclass(frozen=True)
class EvalSpec:
    """One evaluation, complete in the four terms the reviewer asked for.

    id / name          stable identifier + the reviewer's own wording for it.
    owner / agent      which team member owns it, and which agent it evaluates.
    dataset            [MANDATORY] path to the test dataset, backend-relative.
    expected_output    [MANDATORY] what a correct prediction looks like.
    metrics            [MANDATORY] the metric(s) computed. Never just "accuracy"
                       where a safety-critical direction exists — say which.
    acceptance_criteria[MANDATORY] the numeric bar that gates a release. Where
                       one already exists in CI, reuse the SAME number so the
                       plan describes reality rather than an aspiration.
    status             implemented | planned.
    implemented_by     test module(s)/node id(s) that execute this evaluation.
                       Required when status == implemented.
    blocked_on         why a planned eval is not yet implemented. Required when
                       status == planned — an unexplained gap is how a plan rots.
    """

    id: str
    name: str
    owner: str
    agent: str
    dataset: str
    expected_output: str
    metrics: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    status: str = PLANNED
    implemented_by: tuple[str, ...] = field(default_factory=tuple)
    blocked_on: str = ""

    @property
    def dataset_path(self) -> str:
        """Absolute path to the dataset file."""
        return os.path.normpath(os.path.join(_BACKEND_ROOT, self.dataset))

    @property
    def dataset_exists(self) -> bool:
        return os.path.isfile(self.dataset_path)


def validate_spec(spec: EvalSpec) -> None:
    """Enforce that a spec answers all four of the reviewer's questions.

    Raises `EvalSpecError` naming the owner's gap, so the failure message tells
    the responsible member exactly what to write.
    """
    who = f"eval '{spec.id}' (owner: {spec.owner})"

    for label, value in (
        ("dataset", spec.dataset),
        ("expected_output", spec.expected_output),
        ("owner", spec.owner),
        ("agent", spec.agent),
        ("name", spec.name),
    ):
        if not str(value).strip():
            raise EvalSpecError(f"{who} is missing a {label} — Point 2 requires it for every evaluation.")

    if not spec.metrics:
        raise EvalSpecError(f"{who} defines no metrics — Point 2 requires evaluation metrics.")
    if not spec.acceptance_criteria:
        raise EvalSpecError(
            f"{who} defines no acceptance_criteria — Point 2 requires a numeric bar. Reuse the "
            f"threshold that already gates CI where one exists."
        )
    if spec.status not in VALID_STATUSES:
        raise EvalSpecError(f"{who} has unknown status {spec.status!r}; expected one of {sorted(VALID_STATUSES)}.")

    if spec.status == IMPLEMENTED:
        if not spec.implemented_by:
            raise EvalSpecError(
                f"{who} claims status='implemented' but names no test that runs it. Point at the "
                f"test module so the claim is checkable."
            )
        if not spec.dataset_exists:
            raise EvalSpecError(
                f"{who} claims status='implemented' but its dataset {spec.dataset!r} does not exist "
                f"at {spec.dataset_path!r}."
            )
    elif not spec.blocked_on.strip():
        raise EvalSpecError(
            f"{who} is 'planned' but does not say what it is blocked on. State the missing dataset "
            f"or feature so the gap is visible rather than forgotten."
        )


def validate_plan(specs: list[EvalSpec] | tuple[EvalSpec, ...]) -> None:
    """Validate a whole plan, including cross-spec rules (unique ids)."""
    seen: set[str] = set()
    for spec in specs:
        validate_spec(spec)
        if spec.id in seen:
            raise EvalSpecError(f"duplicate eval id {spec.id!r} in the plan.")
        seen.add(spec.id)
