"""Tests for pipeline-outcome notification (app.ml.notify).

The CI/CD deck gives two slides to this — "Notification of Build Outcome" and
"Notification of Test outcome ... via email, chat, or dashboards" — on the
argument that feedback is what makes a pipeline a loop rather than a log. This
pipeline had none: a red `train:model` at 02:00 on the scheduled retrain sat
unread until someone opened the pipelines page.

The digest deliberately leads with FAILURES. A notification that opens with 66
green jobs and buries the one red one has optimised for looking good rather than
for being read, and the only line that matters is the one naming what broke.
"""
from __future__ import annotations

from app.ml import notify


def _jobs():
    return [
        {"name": "lint:backend", "status": "success", "stage": "lint"},
        {"name": "train:model", "status": "failed", "stage": "train"},
        {"name": "test:backend", "status": "success", "stage": "test"},
        {"name": "test:e2e", "status": "failed", "stage": "test", "allow_failure": True},
        {"name": "scan:trivy-fs", "status": "skipped", "stage": "security-scan"},
    ]


def test_digest_counts_each_status():
    d = notify.build_digest(_jobs(), pipeline_id="42", ref="main")

    assert d["counts"]["success"] == 2
    assert d["counts"]["failed"] == 2
    assert d["counts"]["skipped"] == 1


def test_blocking_failures_are_separated_from_allowed_ones():
    """An advisory job going red is not a broken pipeline."""
    d = notify.build_digest(_jobs(), pipeline_id="42", ref="main")

    assert d["blockingFailures"] == ["train:model"]
    assert d["allowedFailures"] == ["test:e2e"]


def test_verdict_is_failed_when_a_blocking_job_failed():
    d = notify.build_digest(_jobs(), pipeline_id="42", ref="main")

    assert d["verdict"] == "FAILED"


def test_verdict_is_passed_when_only_advisory_jobs_failed():
    jobs = [
        {"name": "lint:backend", "status": "success", "stage": "lint"},
        {"name": "test:e2e", "status": "failed", "stage": "test", "allow_failure": True},
    ]

    assert notify.build_digest(jobs, pipeline_id="1", ref="main")["verdict"] == "PASSED"


def test_message_names_the_broken_job_first():
    text = notify.format_message(notify.build_digest(_jobs(), pipeline_id="42", ref="main"))
    body = text.split("\n")

    assert "FAILED" in body[0]
    assert any("train:model" in line for line in body[:3]), text


def test_message_is_plain_ascii_safe():
    """Webhook targets and CI consoles mangle non-ASCII differently; the
    locustfile already learned this the hard way on a cp1252 console."""
    text = notify.format_message(notify.build_digest(_jobs(), pipeline_id="42", ref="main"))

    text.encode("ascii")


def test_empty_job_list_is_reported_as_unknown_not_success():
    """No jobs means the API call failed, which is not a green pipeline."""
    d = notify.build_digest([], pipeline_id="42", ref="main")

    assert d["verdict"] == "UNKNOWN"


def test_send_without_a_webhook_reports_not_configured(capsys):
    digest = notify.build_digest(_jobs(), pipeline_id="42", ref="main")

    sent = notify.send(digest, webhook=None)

    assert sent is False
    assert "train:model" in capsys.readouterr().out
