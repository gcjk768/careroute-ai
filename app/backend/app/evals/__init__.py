"""[MLOps][Responsible-AI] CareRoute evaluation plan — the reviewer's Point 2.

`plan.EVALUATION_PLAN` holds one `EvalSpec` per evaluation Junhua asked for, each
defining the four mandatory things (dataset, expected outputs, metrics,
acceptance criteria). `tests/test_eval_plan.py` validates the whole plan on every
CI run, so an incomplete or stale evaluation plan breaks the build.

Two specs are fully implemented as reference implementations for the other four
owners to copy: `E5-hitl-trigger` and `E6-tool-selection`.
"""
from __future__ import annotations

from .plan import EVALUATION_PLAN, by_id, by_owner
from .spec import (
    IMPLEMENTED,
    PLANNED,
    EvalSpec,
    EvalSpecError,
    validate_plan,
    validate_spec,
)

__all__ = [
    "EVALUATION_PLAN",
    "IMPLEMENTED",
    "PLANNED",
    "EvalSpec",
    "EvalSpecError",
    "by_id",
    "by_owner",
    "validate_plan",
    "validate_spec",
]
