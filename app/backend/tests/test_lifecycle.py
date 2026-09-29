"""[MLOps] Registry lifecycle — Development -> Staging -> Production (Gap 1).

The MLflow client is faked rather than mocked-per-call: these tests are about the
lifecycle RULES, and the rules are what a real tracking server would not help us
check any faster. What a real server would add is confidence that the API names
exist, and that is pinned separately by `test_alias_api_exists_on_the_pinned_mlflow`.
"""
import pytest

from app.ml import lifecycle, promote

pytestmark = pytest.mark.eval


class FakeClient:
    """Minimal stand-in for MlflowClient's registry surface."""

    def __init__(self, versions=("1",), fail=()):
        self.versions = list(versions)
        self.aliases: dict[str, str] = {}
        self.tags: dict[tuple[str, str], str] = {}
        self.fail = set(fail)

    def _guard(self, op):
        if op in self.fail:
            raise RuntimeError(f"simulated {op} failure")

    def search_model_versions(self, _filter):
        self._guard("search")
        return [type("MV", (), {"version": v})() for v in self.versions]

    def set_model_version_tag(self, _name, version, key, value):
        self._guard("tag")
        self.tags[(str(version), key)] = value

    def set_registered_model_alias(self, _name, alias, version):
        self._guard("alias")
        self.aliases[alias] = str(version)

    def get_model_version_by_alias(self, _name, alias):
        if alias not in self.aliases:
            raise RuntimeError("alias not set")
        v = self.aliases[alias]
        return type("MV", (), {"version": v, "tags": {k[1]: val for k, val in self.tags.items()
                                                      if k[0] == v}})()

    def get_model_version(self, _name, version):
        return type("MV", (), {"version": str(version),
                               "tags": {k[1]: val for k, val in self.tags.items()
                                        if k[0] == str(version)}})()


PAYLOAD = {"dataSha256": "d" * 64, "modelSha256": "m" * 64, "versionId": "abc123"}
AUDIT = {"overallAccuracy": 0.9093, "redFlagRecall": 0.9571, "calibration": {"ece": 0.0217}}


def test_a_gated_build_is_staged_not_promoted():
    """The whole point of the split: passing the release gate earns `challenger`,
    never `champion`. Auto-promotion would make shadow deploy decorative."""
    c = FakeClient(versions=("3",))
    version = lifecycle.record_stage(c, "run-1", PAYLOAD, AUDIT, passed_release_gate=True)
    assert version == "3"
    assert c.aliases == {lifecycle.STAGING_ALIAS: "3"}
    assert lifecycle.PRODUCTION_ALIAS not in c.aliases
    assert c.tags[("3", lifecycle.STAGE_TAG)] == lifecycle.STAGING
    print("[LIFECYCLE PASS] A gated build becomes @challenger and stops there; promoting it")
    print("                 stays a deliberate act, which is what the manual gate is for.")


def test_a_failed_gate_is_development_and_gets_no_alias():
    c = FakeClient(versions=("4",))
    lifecycle.record_stage(c, "run-2", PAYLOAD, AUDIT, passed_release_gate=False)
    assert c.aliases == {}
    assert c.tags[("4", lifecycle.STAGE_TAG)] == lifecycle.DEVELOPMENT
    assert c.tags[("4", "release_gate")] == "failed"


def test_registry_failure_never_breaks_a_passing_build():
    """Tracking is optional; a release gate is not. A registry outage must not be
    able to un-build a model that already passed."""
    for op in ("search", "tag", "alias"):
        c = FakeClient(versions=("5",), fail=(op,))
        lifecycle.record_stage(c, "run-3", PAYLOAD, AUDIT)  # must not raise
    print("[LIFECYCLE PASS] Every registry write is best-effort — a tracking outage cannot")
    print("                 fail a training run whose release gate already passed.")


def test_the_newest_version_wins_when_a_run_registered_more_than_one():
    c = FakeClient(versions=("2", "10", "7"))
    assert lifecycle.record_stage(c, "run-4", PAYLOAD, AUDIT) == "10"


def test_promotion_reports_the_version_it_replaced():
    """A promotion that cannot name its predecessor is one you cannot roll back."""
    c = FakeClient()
    c.aliases[lifecycle.PRODUCTION_ALIAS] = "1"
    result = lifecycle.promote(c, "2")
    assert result == {"promoted": "2", "previous": "1"}
    assert c.aliases[lifecycle.PRODUCTION_ALIAS] == "2"
    assert c.tags[("2", lifecycle.STAGE_TAG)] == lifecycle.PRODUCTION
    # Demoted to Staging, not Development — it cleared the same gate and remains a
    # legitimate rollback target.
    assert c.tags[("1", lifecycle.STAGE_TAG)] == lifecycle.STAGING


def test_first_promotion_has_no_predecessor():
    c = FakeClient()
    assert lifecycle.promote(c, "1")["previous"] == "(none)"


def test_promote_cli_refuses_a_version_that_failed_the_gate(monkeypatch):
    c = FakeClient(versions=("9",))
    lifecycle.record_stage(c, "run-5", PAYLOAD, AUDIT, passed_release_gate=False)
    monkeypatch.setattr(promote, "_client", lambda: c)

    assert promote.main(["--version", "9"]) == 1
    assert lifecycle.PRODUCTION_ALIAS not in c.aliases, "refused version must not be promoted"
    print("[LIFECYCLE PASS] --version is a human typing a number; a version tagged")
    print("                 release_gate=failed exits non-zero instead of being promoted.")


def test_promote_cli_promotes_the_current_challenger(monkeypatch):
    c = FakeClient(versions=("6",))
    lifecycle.record_stage(c, "run-6", PAYLOAD, AUDIT, passed_release_gate=True)
    monkeypatch.setattr(promote, "_client", lambda: c)

    assert promote.main([]) == 0
    assert c.aliases[lifecycle.PRODUCTION_ALIAS] == "6"


def test_promote_cli_exits_non_zero_when_nothing_is_staged(monkeypatch):
    """The CLI fails LOUDLY where train.py fails quietly. A promotion that silently
    does nothing leaves an operator believing a new version is serving."""
    monkeypatch.setattr(promote, "_client", lambda: FakeClient())
    assert promote.main([]) == 1


def test_stage_for_is_pure():
    assert lifecycle.stage_for(True) == lifecycle.STAGING
    assert lifecycle.stage_for(False) == lifecycle.DEVELOPMENT


def test_version_tags_carry_the_lineage_hashes():
    tags = lifecycle.version_tags(PAYLOAD, AUDIT, True)
    assert tags["data_sha256"] == PAYLOAD["dataSha256"]
    assert tags["model_sha256"] == PAYLOAD["modelSha256"]
    assert tags["red_flag_recall"] == "0.9571"


def test_alias_api_exists_on_the_pinned_mlflow():
    """Guards the reason this module uses aliases at all.

    The decks teach `transition_model_version_stage`, which MLflow removed in 3.x
    while this project pins 3.13.0. If a future bump ever removed the alias API too,
    the failure would otherwise surface as a silently skipped registry write inside
    a broad `except Exception` — i.e. as nothing at all.
    """
    mlflow = pytest.importorskip("mlflow")
    client = mlflow.MlflowClient
    for name in ("set_registered_model_alias", "get_model_version_by_alias",
                 "set_model_version_tag", "search_model_versions"):
        assert hasattr(client, name), f"MlflowClient.{name} is gone; lifecycle.py needs revisiting"
