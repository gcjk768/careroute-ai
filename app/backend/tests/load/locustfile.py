"""Load test for the CareRoute backend.

Run headless (as CI does):

    locust -f backend/tests/load/locustfile.py --headless \
           -u 20 -r 5 -t 60s --host http://127.0.0.1:8000

Targets only endpoints that need NO LLM provider, so the numbers measure OUR
service rather than a third party's latency: the health probe, the Prometheus
scrape, the escalation queue (hits the store) and the fairness endpoint (hits
the trained model). The SSE triage endpoint is deliberately excluded — it is
long-lived by design and its latency is dominated by the agent pipeline, so
mixing it in would make the p95 here meaningless.

On exit the run is checked against two SLOs and a machine-readable summary is
written for the PDF report. Thresholds are env-tunable:

    CAREROUTE_LOAD_P95_MS      default 800    95th-percentile response time
    CAREROUTE_LOAD_MAX_FAIL    default 0.01   fraction of failed requests
    CAREROUTE_LOAD_SUMMARY     default locust-summary.json
"""
from __future__ import annotations

import json
import os

from locust import HttpUser, between, events, task

P95_MS = float(os.environ.get("CAREROUTE_LOAD_P95_MS", "800"))
MAX_FAIL_RATIO = float(os.environ.get("CAREROUTE_LOAD_MAX_FAIL", "0.01"))
SUMMARY_PATH = os.environ.get("CAREROUTE_LOAD_SUMMARY", "locust-summary.json")


class CareRouteUser(HttpUser):
    """A clinician-dashboard session: polls the queue, occasionally opens detail."""

    wait_time = between(0.1, 0.5)

    @task(5)
    def health(self):
        self.client.get("/api/health", name="GET /api/health")

    @task(3)
    def escalations(self):
        self.client.get("/api/escalations", name="GET /api/escalations")

    @task(2)
    def fairness(self):
        # Exercises the trained severity model, not just the web layer.
        self.client.get("/api/fairness", name="GET /api/fairness")

    @task(1)
    def metrics(self):
        self.client.get("/metrics", name="GET /metrics")


@events.quitting.add_listener
def _check_slos(environment, **_kwargs):
    """Write the summary and set the process exit code from the SLOs.

    Locust exits 0 even when every request failed, so without this the job would
    be green whatever the service did. The CI job is currently ADVISORY
    (allow_failure) because there is no measured baseline yet — the exit code is
    still set correctly so the gate can be made blocking by deleting one line in
    .gitlab-ci.yml once a baseline exists.
    """
    stats = environment.stats.total
    p95 = stats.get_response_time_percentile(0.95) or 0
    fail_ratio = stats.fail_ratio
    summary = {
        "requests": stats.num_requests,
        "failures": stats.num_failures,
        "failRatio": round(fail_ratio, 4),
        "medianMs": stats.median_response_time,
        "p95Ms": p95,
        "maxMs": stats.max_response_time,
        "rps": round(stats.total_rps, 2),
        "thresholds": {"p95Ms": P95_MS, "maxFailRatio": MAX_FAIL_RATIO},
    }
    breaches = []
    if stats.num_requests == 0:
        breaches.append("no requests were issued (is the target up?)")
    if p95 > P95_MS:
        breaches.append(f"p95 {p95:.0f}ms > {P95_MS:.0f}ms")
    if fail_ratio > MAX_FAIL_RATIO:
        breaches.append(f"failure ratio {fail_ratio:.2%} > {MAX_FAIL_RATIO:.2%}")
    summary["breaches"] = breaches
    summary["verdict"] = "PASS" if not breaches else "FAIL"

    try:
        with open(SUMMARY_PATH, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
    except OSError as exc:  # pragma: no cover - never fail a run over the artifact
        print(f"WARNING: could not write {SUMMARY_PATH}: {exc}")

    # Set the exit code BEFORE printing. Printing is the one step here that can
    # raise (a Windows console defaults to cp1252 and dies on non-ASCII), and an
    # exception in an event handler is swallowed by locust — which would leave
    # the verdict unset and the run reported as a crash instead of a breach.
    # For the same reason every string printed below stays ASCII-only.
    environment.process_exit_code = 1 if breaches else 0

    print("--------- load-test SLO check ---------")
    print(json.dumps(summary, indent=2))
    print("SLO BREACH: " + "; ".join(breaches) if breaches else "SLOs met.")
