"""[MLOps][HITL] The clinician-agreement counters exist at 0 before the first decision.

A labelled Counter series is only created on its first .inc(), so the first sample Prometheus
ever scrapes is already 1. increase() needs an earlier sample to compare against, so the
dashboard's "Clinician agreement (range)" tile read 0% after the first decisions.
"""
import pytest

from app import metrics

prometheus_client = pytest.importorskip("prometheus_client")


@pytest.mark.parametrize("agreement", ["agreed", "overridden", "unlabelled"])
def test_every_agreement_label_is_exported_before_any_decision(agreement):
    value = prometheus_client.REGISTRY.get_sample_value(
        "careroute_hitl_decisions_total", {"agreement": agreement})
    assert value is not None, f"{agreement} series is created lazily; increase() will miss its first decision"
