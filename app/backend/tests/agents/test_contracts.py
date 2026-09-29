"""Cross-agent isolation guard — the framework's safety net.

Runs EVERY agent through `run_and_check`, which fails if an agent mutates a
CaseState field outside its declared `writes` lane or omits a declared `returns`
key. This is the test that stops one member's change from silently breaking
another member's agent. Run with: pytest -m contract
"""
import pytest

from harness import AGENT_CLASSES, make_case, run_and_check

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("slug", sorted(AGENT_CLASSES))
def test_agent_declares_a_contract(slug):
    agent = AGENT_CLASSES[slug]()
    assert hasattr(agent, "CONTRACT"), f"{slug} agent must declare a CONTRACT"
    assert agent.CONTRACT.writes is not None
    assert agent.CONTRACT.returns


@pytest.mark.parametrize("slug", sorted(AGENT_CLASSES))
def test_agent_stays_within_its_lane(slug):
    # A severe, ambiguous case exercises the widest write-path in every agent.
    result, _state = run_and_check(AGENT_CLASSES[slug](), make_case())
    assert result, f"{slug} agent returned an empty result"
