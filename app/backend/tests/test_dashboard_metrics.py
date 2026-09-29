"""[MLOps] The Grafana dashboard reads only metrics that exist.

A panel on a misspelled metric renders "No data" forever, which on a quiet demo
looks exactly like a healthy system. This is the dashboard twin of
test_llm_latency.py::test_every_llm_alert_reads_a_metric_that_exists.
"""
import json
import os
import re

import pytest

from app import metrics

pytestmark = pytest.mark.eval

DASHBOARD = os.path.join(
    os.path.dirname(__file__), "..", "..", "monitoring", "grafana", "dashboards", "careroute-triage.json"
)
RULES = os.path.join(os.path.dirname(__file__), "..", "..", "monitoring", "alert.rules.yml")


def _exprs() -> list[str]:
    with open(DASHBOARD, encoding="utf-8") as f:
        dash = json.load(f)
    return [t["expr"] for p in dash["panels"] for t in p.get("targets", [])]


def _exported() -> set[str]:
    """Base names of every careroute_* metric app/metrics.py registers."""
    from prometheus_client import REGISTRY

    return {m.name for m in REGISTRY.collect() if m.name.startswith("careroute_")}


def test_every_careroute_metric_on_the_dashboard_is_exported():
    pytest.importorskip("prometheus_client")
    assert metrics.enabled()
    exported = _exported()
    referenced = set()
    for expr in _exprs():
        for name in re.findall(r"\bcareroute_[a-z_]+", expr):
            base = re.sub(r"_(bucket|count|sum|total)$", "", name)
            referenced.add(base)
    unknown = referenced - exported
    assert not unknown, f"dashboard panels read metrics app/metrics.py does not export: {unknown}"


def test_every_recording_rule_on_the_dashboard_is_defined():
    import yaml

    with open(RULES, encoding="utf-8") as f:
        groups = yaml.safe_load(f)["groups"]
    recorded = {r["record"] for g in groups for r in g["rules"] if "record" in r}
    used = {m for expr in _exprs() for m in re.findall(r"careroute:[a-z0-9_:]+", expr)}
    missing = used - recorded
    assert not missing, f"dashboard reads recording rules alert.rules.yml does not define: {missing}"


def test_every_exported_metric_has_a_panel():
    """The point of the dashboard is that nothing the app measures is invisible.
    A new metric in app/metrics.py should arrive with a panel."""
    pytest.importorskip("prometheus_client")
    text = "\n".join(_exprs())
    unplotted = {name for name in _exported() if name not in text}
    assert not unplotted, f"metrics with no dashboard panel: {unplotted}"
