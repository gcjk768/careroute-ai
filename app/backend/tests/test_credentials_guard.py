"""Tests for the no-live-credentials guard (app.credentials_guard).

The course's rule is "nothing below production spends money": CI and staging run
against mocks or capped sandboxes, and a guard asserts that no non-production
environment is holding a credential that would let an agent transact for real.

CareRoute's agent-reachable live services are OneMap (a rate-limited account
whose password has already leaked once), the LLM provider, and Onyx. CI PLUMBING
credentials — the SonarQube token, the pipeline trigger token, the MLflow token —
are deliberately NOT covered: they are the pipeline's own credentials, no agent
can reach them, and failing the build over them would train everyone to ignore
the gate.

The fixtures below carry `# gitleaks:allow`. To prove the guard REPORTS a live
credential a fixture has to look live, so `scan:secrets-gitleaks` flagged five of
them as `generic-api-key` and failed the build on this file alone. The marker is
per-line and deliberately so: an allowlist entry for the whole path would stop
scanning the one file in the repo most likely to gain new key-shaped strings.
Every value here is invented — none has ever authenticated against anything.
"""
from __future__ import annotations

import pytest

from app import credentials_guard as guard


def test_real_looking_provider_key_is_reported():
    env = {"CI": "true", "OPENAI_API_KEY": "sk-proj-9RtQm4xZb2LcVn8pWk3f"}  # gitleaks:allow

    assert guard.find_live_credentials(env) == ["OPENAI_API_KEY"]


def test_placeholder_value_is_not_reported():
    env = {"CI": "true", "OPENAI_API_KEY": "changeme", "ONEMAP_PASSWORD": "your-password-here"}

    assert guard.find_live_credentials(env) == []


def test_unset_and_empty_credentials_are_not_reported():
    env = {"CI": "true", "OPENAI_API_KEY": "", "ONEMAP_EMAIL": "   "}

    assert guard.find_live_credentials(env) == []


def test_ci_plumbing_tokens_are_out_of_scope():
    """These are the pipeline's own credentials, not an agent's."""
    env = {
        "CI": "true",
        "SONAR_TOKEN": "sqp_8f21c4d9ab77e5",  # gitleaks:allow
        "CAREROUTE_PIPELINE_TRIGGER_TOKEN": "glptt-4d9c1188aa",
        "MLFLOW_TRACKING_TOKEN": "dbx-9911aa",
        "NVD_API_KEY": "1f2e3d4c-5b6a",  # gitleaks:allow
    }

    assert guard.find_live_credentials(env) == []


def test_onemap_credentials_are_in_scope():
    env = {"CI": "true", "ONEMAP_EMAIL": "team@example.org", "ONEMAP_PASSWORD": "Rt8#vQ2mLp"}

    assert guard.find_live_credentials(env) == ["ONEMAP_EMAIL", "ONEMAP_PASSWORD"]


def test_ci_is_recognised_as_non_production():
    assert guard.is_non_production({"CI": "true"}) is True


def test_explicit_production_is_not_non_production():
    assert guard.is_non_production({"CAREROUTE_ENV": "production"}) is False


def test_plain_developer_machine_is_not_treated_as_ci():
    """A developer legitimately holds live OneMap credentials for
    tests/test_routing_live.py; the guard must not fail their local run."""
    assert guard.is_non_production({}) is False


def test_findings_never_include_the_secret_value():
    secret = "sk-proj-DoNotLeakThisValue123"  # noqa: S105  # fake canary value the guard must never echo
    env = {"CI": "true", "OPENAI_API_KEY": secret}

    report = guard.build_report(env)

    assert secret not in str(report)
    assert "OPENAI_API_KEY" in report["live"]


def test_main_exits_non_zero_when_ci_holds_live_credentials(tmp_path):
    out = tmp_path / "no-live-credentials.json"
    env = {"CI": "true", "ONEMAP_PASSWORD": "Rt8#vQ2mLp"}

    code = guard.main(["--out", str(out)], env=env)

    assert code == 1
    assert "Rt8#vQ2mLp" not in out.read_text(encoding="utf-8")


def test_main_exits_zero_when_ci_is_clean(tmp_path):
    out = tmp_path / "no-live-credentials.json"

    code = guard.main(["--out", str(out)], env={"CI": "true"})

    assert code == 0


def test_main_is_inert_outside_ci(tmp_path):
    """On a developer box the guard reports and passes rather than failing."""
    out = tmp_path / "no-live-credentials.json"
    env = {"ONEMAP_PASSWORD": "Rt8#vQ2mLp"}

    code = guard.main(["--out", str(out)], env=env)

    assert code == 0


@pytest.mark.parametrize("value", ["dummy", "test", "fake", "placeholder", "xxx", "not-available"])
def test_common_placeholders_are_recognised(value):
    assert guard.find_live_credentials({"CI": "true", "ONYX_API_KEY": value}) == []


# --------------------------------------------------------------------------
# Declared exemptions. The red-team jobs (garak, promptfoo) need a live
# OPENAI_API_KEY in CI to do anything at all, which is exactly what this gate
# forbids. The course's own answer is a self-hosted model in CI ("vLLM, pinned
# small", money at risk: none) — until that exists, the compromise has to be
# NAMED IN THE PIPELINE rather than silently tolerated or the gate deleted.
# --------------------------------------------------------------------------

def test_declared_exemption_is_not_a_failure():
    env = {
        "CI": "true",
        "OPENAI_API_KEY": "sk-proj-9RtQm4xZb2LcVn8pWk3f",  # gitleaks:allow
        "CAREROUTE_ALLOWED_LIVE_CREDENTIALS": "OPENAI_API_KEY",
    }

    report = guard.build_report(env)

    assert report["verdict"] == "PASS"
    assert report["exempted"] == ["OPENAI_API_KEY"]
    assert report["live"] == []


def test_an_exemption_does_not_cover_the_others():
    """Exempting the LLM key must not quietly exempt OneMap too."""
    env = {
        "CI": "true",
        "OPENAI_API_KEY": "sk-proj-9RtQm4xZb2LcVn8pWk3f",  # gitleaks:allow
        "ONEMAP_PASSWORD": "Rt8#vQ2mLp",
        "CAREROUTE_ALLOWED_LIVE_CREDENTIALS": "OPENAI_API_KEY",
    }

    report = guard.build_report(env)

    assert report["verdict"] == "FAIL"
    assert report["live"] == ["ONEMAP_PASSWORD"]
    assert report["exempted"] == ["OPENAI_API_KEY"]


def test_exemption_list_is_comma_separated_and_whitespace_tolerant():
    env = {
        "CI": "true",
        "OPENAI_API_KEY": "sk-live-aaa",
        "ONYX_API_KEY": "onyx-bbb",
        "CAREROUTE_ALLOWED_LIVE_CREDENTIALS": " OPENAI_API_KEY , ONYX_API_KEY ",
    }

    assert guard.build_report(env)["verdict"] == "PASS"


def test_exempting_a_credential_that_is_not_set_changes_nothing():
    env = {"CI": "true", "CAREROUTE_ALLOWED_LIVE_CREDENTIALS": "OPENAI_API_KEY"}

    report = guard.build_report(env)

    assert report["verdict"] == "PASS"
    assert report["exempted"] == []


def test_exemptions_are_reported_so_they_cannot_hide():
    """The whole point of an exemption over a deletion: it stays visible."""
    env = {"CI": "true", "OPENAI_API_KEY": "sk-live-aaa",
           "CAREROUTE_ALLOWED_LIVE_CREDENTIALS": "OPENAI_API_KEY"}

    assert "OPENAI_API_KEY" in guard.build_report(env)["exempted"]
