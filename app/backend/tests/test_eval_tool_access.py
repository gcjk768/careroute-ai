"""[AI-Security] E6 — "Tool-selection accuracy", reframed as tool-access enforcement.

*** REFERENCE IMPLEMENTATION for the reviewer's Point 2. ***
Spec: app/evals/plan.E6_TOOL_SELECTION.

THE REFRAMING IS THE FINDING. The review asked for "tool-selection accuracy".
No agent in CareRoute selects a tool at runtime — every worker declares a static
least-privilege TOOL_ALLOWLIST and `enforce_tool_access()` is called at each
call site. There is no selection to be accurate about, so reporting a
selection-accuracy number would be reporting a fiction.

What is real, measurable, and materially more important for AI security
(OWASP LLM06 excessive agency / LLM08 excessive permissions, FR-12) is
ENFORCEMENT: every permitted (agent, tool) pair must work and every forbidden
pair must be blocked. tests/fixtures/tool_access_cases.json is that matrix.

Run with: pytest -m eval
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents import (
    CareRoutingAgent,
    HumanInTheLoopAgent,
    ReflectionAgent,
    SafetyOverrideAgent,
    SeverityClassifierAgent,
    SymptomIntakeAgent,
    ToolAccessError,
    enforce_tool_access,
)
from app.evals import by_id

pytestmark = pytest.mark.eval

SPEC = by_id("E6-tool-selection")

AGENTS: dict[str, type] = {
    "intake": SymptomIntakeAgent,
    "classifier": SeverityClassifierAgent,
    "safety": SafetyOverrideAgent,
    "routing": CareRoutingAgent,
    "hitl": HumanInTheLoopAgent,
    "reflection": ReflectionAgent,
}

_FIXTURE = Path(__file__).parent / "fixtures" / "tool_access_cases.json"


def _pairs() -> list[dict]:
    with _FIXTURE.open(encoding="utf-8") as fh:
        return json.load(fh)["pairs"]


PAIRS = _pairs()


@pytest.mark.parametrize(
    "pair", PAIRS, ids=[f"{p['agent']}:{p['tool']}:{'allow' if p['permitted'] else 'deny'}" for p in PAIRS]
)
def test_tool_access_matrix(pair):
    """Every row of the permission matrix, in both directions."""
    agent = AGENTS[pair["agent"]]()
    if pair["permitted"]:
        enforce_tool_access(agent, pair["tool"])  # must not raise
    else:
        with pytest.raises(ToolAccessError):
            enforce_tool_access(agent, pair["tool"])


def test_forbidden_pair_block_rate_is_total():
    """Acceptance criterion: block rate == 1.0. A single unblocked pair is a
    least-privilege breach, so this is asserted in aggregate as well as per-row —
    the aggregate is what gets reported as the evaluation's headline metric."""
    forbidden = [p for p in PAIRS if not p["permitted"]]
    assert forbidden, "matrix defines no forbidden pairs — it would prove nothing"
    unblocked = []
    for pair in forbidden:
        try:
            enforce_tool_access(AGENTS[pair["agent"]](), pair["tool"])
            unblocked.append(f"{pair['agent']}->{pair['tool']}")
        except ToolAccessError:
            pass
    assert not unblocked, f"forbidden tool access was permitted: {unblocked}"


def test_matrix_covers_every_agent_and_every_declared_tool():
    """Coverage guard: a tool added to an allow-list but never added here would
    otherwise be evaluated by nobody."""
    covered = {(p["agent"], p["tool"]) for p in PAIRS if p["permitted"]}
    missing = []
    for slug, cls in AGENTS.items():
        for tool in cls().TOOL_ALLOWLIST:
            if (slug, tool) not in covered:
                missing.append(f"{slug}->{tool}")
    assert not missing, (
        f"tools granted but not covered by the evaluation matrix: {missing}. "
        f"Add a row to tests/fixtures/tool_access_cases.json."
    )


def test_privilege_creep_is_reported(capsys):
    """Advisory, non-blocking: tools GRANTED in the allow-list but not claimed as
    USED in the agent's CAPABILITY. Surfacing dead privilege at review time is
    how an allow-list stays least-privilege instead of growing monotonically."""
    creep = {}
    for slug, cls in AGENTS.items():
        agent = cls()
        unused = sorted(set(agent.TOOL_ALLOWLIST) - set(agent.CAPABILITY.tools))
        if unused:
            creep[slug] = unused
    print(f"[E6] privilege-creep delta (advisory): {creep or 'none'}")
    assert isinstance(creep, dict)  # advisory only — never blocks a release


def test_spec_and_implementation_agree():
    assert SPEC.status == "implemented"
    assert "tests/test_eval_tool_access.py" in SPEC.implemented_by
    assert SPEC.dataset_exists
