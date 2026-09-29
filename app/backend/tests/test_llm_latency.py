"""[MLOps] LLM latency histogram + the alert rules that read it.

The last item left open when token/cost accounting closed Gap 2 in
[[Lecture Alignment]]. The tests that matter here are the ones about FAILED
calls: a latency metric that records only successes reports a provider chain
getting faster as it gets sicker.
"""
import pytest
import yaml

from app import metrics

pytestmark = pytest.mark.eval

RULES = "../../monitoring/alert.rules.yml"  # repo root, not backend/


def _samples(name: str) -> dict:
    """Current values of a histogram's _count series, keyed by label tuple."""
    from prometheus_client import REGISTRY

    out = {}
    for metric in REGISTRY.collect():
        for s in metric.samples:
            if s.name == name:
                out[tuple(sorted(s.labels.items()))] = s.value
    return out


def test_a_failed_call_is_timed_too():
    """The whole reason the metric carries an `outcome` label."""
    pytest.importorskip("prometheus_client")
    before = _samples("careroute_llm_latency_seconds_count")
    metrics.observe_llm_latency("gpt-4o-mini", 12.5, "timeout")
    after = _samples("careroute_llm_latency_seconds_count")
    key = (("model", "gpt-4o-mini"), ("outcome", "timeout"))
    assert after.get(key, 0) == before.get(key, 0) + 1
    print("[LLM LATENCY PASS] A timed-out provider call is recorded, not dropped — otherwise")
    print("                   p95 improves as the chain degrades.")


def test_outcomes_do_not_share_a_bucket():
    pytest.importorskip("prometheus_client")
    metrics.observe_llm_latency("m", 0.5, "ok")
    metrics.observe_llm_latency("m", 30.0, "error")
    s = _samples("careroute_llm_latency_seconds_count")
    assert (("model", "m"), ("outcome", "ok")) in s
    assert (("model", "m"), ("outcome", "error")) in s


def test_negative_durations_are_clamped():
    """A monotonic clock should never produce one, but a metric that can go
    backwards makes a rate() silently useless."""
    metrics.observe_llm_latency("m", -3.0, "ok")  # must not raise


def test_it_is_a_no_op_without_prometheus(monkeypatch):
    monkeypatch.setattr(metrics, "LLM_LATENCY", None)
    metrics.observe_llm_latency("m", 1.0, "ok")  # must not raise


def test_buckets_cover_the_longest_provider_timeout():
    """The default Prometheus ladder tops out at 10s. Every provider timeout in
    this app is longer, so a timing-out chain would pile into +Inf and become
    unmeasurable exactly when it matters most."""
    pytest.importorskip("prometheus_client")
    from app import config

    longest = config.OPENAI_TIMEOUT
    buckets = [b for b in metrics.LLM_LATENCY._upper_bounds if b != float("inf")]
    assert max(buckets) >= longest, (
        f"longest provider timeout is {longest}s but the top bucket is {max(buckets)}s"
    )


def test_every_llm_alert_reads_a_metric_that_exists():
    """An alert on a misspelled metric never fires and never complains.

    Checks the base metric names the careroute-llm group references are ones
    app/metrics.py actually exports — the failure mode of a rules file is silence.
    """
    import os
    import re

    path = os.path.join(os.path.dirname(__file__), RULES)
    groups = yaml.safe_load(open(path, encoding="utf-8"))["groups"]
    llm = [g for g in groups if g["name"] == "careroute-llm"]
    assert llm, "careroute-llm rule group missing"

    exported = {"careroute_llm_tokens_total", "careroute_llm_cost_usd_total",
                "careroute_llm_latency_seconds", "careroute_triage_requests_total"}
    referenced = set()
    for rule in llm[0]["rules"]:
        for m in re.findall(r"careroute_[a-z_]+", rule["expr"]):
            referenced.add(m.removesuffix("_bucket").removesuffix("_count"))
    unknown = referenced - exported
    assert not unknown, f"alert rules reference metrics that do not exist: {unknown}"
    print(f"[LLM LATENCY PASS] {len(llm[0]['rules'])} LLM alert rules, every referenced metric")
    print("                   exported by app/metrics.py.")


def test_every_llm_alert_keeps_the_service_label():
    """The file's own standing rule: `sum()` drops every label not named, and
    alertmanager groups and inhibits on `service`. An aggregation that loses it
    produces alerts that cannot be grouped or suppressed."""
    import os

    path = os.path.join(os.path.dirname(__file__), RULES)
    groups = yaml.safe_load(open(path, encoding="utf-8"))["groups"]
    llm = [g for g in groups if g["name"] == "careroute-llm"][0]
    for rule in llm["rules"]:
        assert "by (service)" in rule["expr"] or "by (le, service)" in rule["expr"], rule["alert"]
