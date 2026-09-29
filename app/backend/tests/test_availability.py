"""[MLOps] Availability: yield's missing denominator, and harvest.

Lecture 03 teaches uptime / yield / harvest. The tests that matter here are the
ones about the DENOMINATOR: a yield metric computed from counters that only
count successes is the constant 1.0, and reports perfect availability during an
outage.
"""
import pytest
import yaml

from app import availability, metrics
from app.agents.base import CaseState

pytestmark = pytest.mark.eval

RULES = "../../monitoring/alert.rules.yml"  # repo root, not backend/


def _state(**overrides) -> CaseState:
    state = CaseState(raw_text="chest pain")
    defaults = {
        "explanation_source": "shap",
        "explanation": [{"feature": "chest_pain", "weight": 0.8}],
        "citations": [{"title": "Chest Pain", "snippet": "…", "source": "AHA"}],
        "clinic": "Bukit Timah Polyclinic",
        "latitude": 1.3,
        "longitude": 103.8,
        "route_available": True,
    }
    for key, value in {**defaults, **overrides}.items():
        setattr(state, key, value)
    return state


def _samples(name: str) -> dict:
    from prometheus_client import REGISTRY

    out = {}
    for metric in REGISTRY.collect():
        for s in metric.samples:
            if s.name == name:
                out[tuple(sorted(s.labels.items()))] = s.value
    return out


# --- harvest ---------------------------------------------------------------------
def test_a_complete_answer_harvests_one():
    score, parts = availability.harvest(_state())
    assert score == 1.0
    assert all(parts[c] is True for c in availability.COMPONENTS)


def test_the_keyword_fallback_is_a_degraded_answer_not_a_failed_one():
    """The trade this metric exists to make visible: the case is still served."""
    score, parts = availability.harvest(_state(explanation_source="keyword"))
    assert parts["assessment"] is False
    assert score == 0.8


def test_retrieval_falling_over_costs_harvest():
    score, parts = availability.harvest(_state(citations=[]))
    assert parts["citations"] is False and score == 0.8


def test_a_case_without_coordinates_is_not_punished_for_having_no_route():
    """Otherwise harvest measures how many patients shared their location."""
    score, parts = availability.harvest(
        _state(latitude=None, longitude=None, route_available=False)
    )
    assert parts["route"] is None
    assert score == 1.0                     # four applicable components, four present


def test_a_case_with_coordinates_and_no_route_is_degraded():
    score, parts = availability.harvest(_state(route_available=False))
    assert parts["route"] is False
    assert score == 0.8


def test_everything_degraded_harvests_zero_and_still_counts_as_served():
    score, _ = availability.harvest(
        _state(explanation_source="keyword", explanation=[], citations=[], clinic=None,
               route_available=False)
    )
    assert score == 0.0


# --- the metric -------------------------------------------------------------------
def test_observing_harvest_records_the_score_and_which_part_degraded():
    pytest.importorskip("prometheus_client")
    before = _samples("careroute_answer_components_total")
    score = metrics.observe_harvest(_state(citations=[]))
    after = _samples("careroute_answer_components_total")
    assert score == 0.8
    key = (("component", "citations"), ("state", "degraded"))
    assert after.get(key, 0) == before.get(key, 0) + 1


def test_a_telemetry_fault_never_fails_a_triage_that_already_succeeded():
    class _Exploding:
        def __getattr__(self, name):
            raise RuntimeError("state is gone")

    assert metrics.observe_harvest(_Exploding()) is None


# --- yield ------------------------------------------------------------------------
def test_the_error_outcome_exists_so_yield_has_a_denominator():
    """`_counted_stream` is the only place a FAILED triage is counted."""
    import asyncio

    from app.main import _counted_stream

    async def _boom():
        yield b"data: {}\n\n"
        raise RuntimeError("orchestration exploded")

    async def _drain():
        async for _ in _counted_stream(_boom()):
            pass

    pytest.importorskip("prometheus_client")
    before = _samples("careroute_triage_requests_total")
    with pytest.raises(RuntimeError):
        asyncio.run(_drain())
    after = _samples("careroute_triage_requests_total")
    key = (("outcome", "error"),)
    assert after.get(key, 0) == before.get(key, 0) + 1


def test_a_client_disconnect_is_not_counted_as_a_service_failure():
    import asyncio

    from app.main import _counted_stream

    async def _cancelled():
        yield b"data: {}\n\n"
        raise asyncio.CancelledError

    async def _drain():
        async for _ in _counted_stream(_cancelled()):
            pass

    pytest.importorskip("prometheus_client")
    before = _samples("careroute_triage_requests_total")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_drain())
    after = _samples("careroute_triage_requests_total")
    key = (("outcome", "error"),)
    assert after.get(key, 0) == before.get(key, 0)


# --- the rules that read them ------------------------------------------------------
def _rules(group_name: str) -> list[dict]:
    import os

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), RULES)
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    group = next(g for g in doc["groups"] if g["name"] == group_name)
    return group["rules"]


def test_yield_and_harvest_are_recorded_rules_not_dashboard_arithmetic():
    names = {r.get("record") for r in _rules("careroute-availability")}
    assert {"careroute:yield:ratio5m", "careroute:harvest:mean5m",
            "careroute:availability:ratio30d"} <= names


def test_the_yield_denominator_includes_the_requests_that_never_ran():
    """Rate-limited and quarantined requests are yield losses: the service
    received them and did not serve them. Leaving them out is how a shedding
    service reports 100% yield."""
    rule = next(r for r in _rules("careroute-availability") if r.get("record") == "careroute:yield:ratio5m")
    expr = rule["expr"]
    assert "careroute_rate_limited_total" in expr
    assert "careroute_abuse_events_total" in expr
    assert 'outcome="error"' in expr or "outcome=~" in expr


def test_every_aggregating_availability_rule_keeps_the_service_label():
    """Same trap as the LLM rules: `sum()` without `by (service)` drops the
    label alertmanager groups and inhibits on. Rules that do not aggregate
    (the `up` gauge, and alerts reading a recorded series) carry it already."""
    for rule in _rules("careroute-availability"):
        expr = " ".join(rule["expr"].split())
        if "sum(" in expr:
            assert "by (service)" in expr, rule.get("record") or rule.get("alert")


def test_the_denominator_survives_a_counter_that_never_fired():
    """`a + b` where b has never been incremented matches nothing and empties
    the whole expression — the metric would vanish while the service is calm."""
    rule = next(r for r in _rules("careroute-availability") if r.get("record") == "careroute:yield:ratio5m")
    expr = " ".join(rule["expr"].split())
    assert " or rate(" in expr and "+ sum(rate(careroute_rate_limited" not in expr
