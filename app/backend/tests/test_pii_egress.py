"""Tests for the PII-egress gate (app.ml.pii_egress).

The gate answers one question: did any direct identifier reach an artifact that
LEAVES the trust boundary (the inference log, the ground-truth log, the drift
report, the executive report)? `app/redact.py` masks identifiers at the input
boundary; this is the independent check that the masking actually held, which is
the course's "PII egress events = 0" gate.

The most important property under test is that the gate's OWN report never
contains the identifier it found — a findings file that quotes the NRIC it
caught has just re-published it into a CI artifact.
"""
from __future__ import annotations

import json

import pytest

from app.ml import pii_egress


@pytest.fixture()
def egress_dir(tmp_path):
    d = tmp_path / "monitoring"
    d.mkdir()
    return d


def test_nric_reaching_an_artifact_is_detected(egress_dir):
    f = egress_dir / "inference_log.jsonl"
    f.write_text(json.dumps({"note": "patient S1234567D triaged"}) + "\n", encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    kinds = [x["kind"] for x in result["findings"]]
    assert "NRIC" in kinds
    assert result["verdict"] == "FAIL"


# ---------------------------------------------------------------------------
# The false-positive class that made this gate useless on its first real run
# (job 16463280258: 5,749 findings, zero of them identifiers). Each test below
# is one of the shapes it flagged.
# ---------------------------------------------------------------------------

def test_a_float_mantissa_is_not_a_medical_record_number(egress_dir):
    """`"driftShare": 0.0967741935483871` was read as an MRN: the mantissa is a
    16-digit run and the MRN rule is "7+ bare digits". Numbers are not scanned
    at all now -- an identifier is something a human wrote down."""
    f = egress_dir / "drift_report.json"
    f.write_text(json.dumps({"driftShare": 0.0967741935483871, "value": 1234567890.5}), encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    assert result["findings"] == []
    assert result["verdict"] == "PASS"


def test_an_opaque_case_id_is_not_a_phone_number(egress_dir):
    """`case_69837191d0` matched the Singapore numbering plan -- 8 hex digits
    opening with 6. It is a `new_id()` uuid4 slice and cannot be a phone."""
    f = egress_dir / "ground_truth.jsonl"
    f.write_text(json.dumps({"caseId": "case_69837191d0", "ts": "2026-09-12T14:05:37.910535+00:00"}) + "\n",
                 encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    assert result["findings"] == []


def test_a_model_version_is_not_a_phone_number(egress_dir):
    """A model hash with an 8-digit run (13f + 8 digits + d) matched the Singapore
    numbering plan in a local inference log."""
    f = egress_dir / "inference_log.jsonl"
    f.write_text(json.dumps({"modelVersion": "careroute-triage-rf-13f98765432d (rf/200, cal/isotonic)"}) + "\n",
                 encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    assert result["findings"] == []


def test_a_model_version_field_holding_a_phone_is_still_scanned(egress_dir):
    f = egress_dir / "inference_log.jsonl"
    f.write_text(json.dumps({"modelVersion": "call 91234567"}) + "\n", encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    assert "PHONE" in {x["kind"] for x in result["findings"]}


def test_a_feature_vector_is_not_a_person(egress_dir):
    """3,292 of the original findings were Presidio NER on `[1.0, 0.0, ...]`."""
    f = egress_dir / "inference_log.jsonl"
    f.write_text(json.dumps({"features": [1.0, 0.0, 0.0, 1.0], "acuity": "P1_RESUSCITATION",
                             "confidence": 0.6825}) + "\n", encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    assert result["findings"] == []


def test_an_undeclared_text_field_is_scanned_hardest(egress_dir):
    """The fail-safe direction, and the reason the exemption list is safe.

    Skipping known machine fields only helps if a NEW field is treated as
    suspect. The day somebody adds `rawText` to the inference log, it must be
    scanned -- that is the regression this gate exists to catch.
    """
    f = egress_dir / "inference_log.jsonl"
    f.write_text(json.dumps({"ts": "2026-09-12T12:00:00.123456+00:00",
                             "caseId": "case_69837191d0",
                             "rawText": "NRIC S1234567D, call 91234567"}) + "\n", encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    kinds = {x["kind"] for x in result["findings"]}
    assert {"NRIC", "PHONE"} <= kinds
    assert result["verdict"] == "FAIL"
    # ...and the finding names the field, so the leak is navigable.
    assert {x["field"] for x in result["findings"]} == {"rawText"}


def test_a_declared_field_holding_the_wrong_shape_is_still_scanned(egress_dir):
    """The exemption is shape-verified, not name-verified. A `caseId` that does
    not look like a `new_id()` token gets the full detector set, so the list
    cannot become a hiding place."""
    f = egress_dir / "ground_truth.jsonl"
    f.write_text(json.dumps({"caseId": "S1234567D"}) + "\n", encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    assert "NRIC" in {x["kind"] for x in result["findings"]}


def test_unparseable_json_falls_back_to_line_scanning(egress_dir):
    """Broken is not clean -- the same rule the absent-file branch follows."""
    f = egress_dir / "drift_report.json"
    f.write_text("{ this is not json, NRIC S1234567D", encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    assert "NRIC" in {x["kind"] for x in result["findings"]}


def test_the_machine_composed_report_still_gets_identifier_rules(egress_dir):
    """NER is off for the executive report (its proper nouns are tool names like
    Trivy and Horusec), but the identifier rules are not -- that is what
    "identifier egress" means, and it costs nothing."""
    f = egress_dir / "careroute_mlops_report.md"
    f.write_text("| scan:trivy-fs | SCA scan |\nLeaked NRIC S1234567D in the summary\n", encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    kinds = [x["kind"] for x in result["findings"]]
    assert kinds == ["NRIC"]


def test_clinical_vitals_are_not_flagged(egress_dir):
    """A vitals run is not a phone number. The redactor learned this the hard
    way (blood pressure became [REDACTED_PHONE]); the gate must not regress it
    or every clean pipeline goes red."""
    f = egress_dir / "inference_log.jsonl"
    f.write_text(
        json.dumps({"note": "blood pressure 140 90 110 70, chest pain since 12-05-2026"}) + "\n",
        encoding="utf-8",
    )

    result = pii_egress.scan_artifacts([str(f)])

    assert result["findings"] == []
    assert result["verdict"] == "PASS"


def test_report_never_contains_the_raw_identifier(egress_dir):
    """The gate must not re-publish what it caught."""
    f = egress_dir / "ground_truth.jsonl"
    f.write_text("clinician note for S7654321A, call +6591234567\n", encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])
    blob = json.dumps(result)

    assert result["findings"], "expected findings for this fixture"
    assert "S7654321A" not in blob
    assert "91234567" not in blob
    assert "+6591234567" not in blob


def test_already_redacted_text_is_clean(egress_dir):
    f = egress_dir / "inference_log.jsonl"
    f.write_text("patient [REDACTED_NRIC] phone [REDACTED_PHONE]\n", encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    assert result["findings"] == []
    assert result["verdict"] == "PASS"


def test_absent_artifact_is_skipped_not_reported_clean(egress_dir):
    """Same rule the security report follows: absent != clean."""
    missing = egress_dir / "never_written.jsonl"

    result = pii_egress.scan_artifacts([str(missing)])

    assert result["scanned"] == []
    assert str(missing) in result["skipped"]
    assert result["verdict"] == "PASS"


def test_findings_report_the_line_number(egress_dir):
    f = egress_dir / "inference_log.jsonl"
    f.write_text("clean line\nanother clean line\nleak S1234567D here\n", encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    assert result["findings"][0]["line"] == 3


def test_backend_and_status_are_recorded(egress_dir):
    """Mirrors monitor.py: say which detector actually ran and why, so a
    'clean' result can never silently mean 'the detector never loaded'."""
    f = egress_dir / "inference_log.jsonl"
    f.write_text("nothing here\n", encoding="utf-8")

    result = pii_egress.scan_artifacts([str(f)])

    assert result["backend"] in {"presidio", "builtin-regex"}
    assert result["backendStatus"]


def test_main_exits_non_zero_when_pii_escapes(egress_dir, tmp_path, capsys):
    f = egress_dir / "inference_log.jsonl"
    f.write_text("patient S1234567D\n", encoding="utf-8")
    out = tmp_path / "pii-egress.json"

    code = pii_egress.main(["--path", str(f), "--out", str(out)])

    assert code == 1
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["verdict"] == "FAIL"
    assert "S1234567D" not in out.read_text(encoding="utf-8")


def test_main_exits_zero_on_clean_artifacts(egress_dir, tmp_path):
    f = egress_dir / "inference_log.jsonl"
    f.write_text("all clear\n", encoding="utf-8")
    out = tmp_path / "pii-egress.json"

    code = pii_egress.main(["--path", str(f), "--out", str(out)])

    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "PASS"


# --------------------------------------------------------------------------
# Artifact locations differ between a laptop and CI: the report job writes to
# $CI_PROJECT_DIR/reports, not backend/reports. A gate that looked in the wrong
# place would report "skipped" forever and never actually audit the report.
# --------------------------------------------------------------------------

def test_default_paths_follow_the_monitor_and_report_dir_env_vars(tmp_path, monkeypatch):
    mon = tmp_path / "mon"
    rep = tmp_path / "rep"
    mon.mkdir()
    rep.mkdir()
    monkeypatch.setenv("CAREROUTE_MONITOR_DIR", str(mon))
    monkeypatch.setenv("CAREROUTE_REPORT_DIR", str(rep))

    paths = pii_egress.default_artifacts()

    assert str(mon / "inference_log.jsonl") in paths
    assert str(mon / "ground_truth.jsonl") in paths
    assert str(mon / "drift_report.json") in paths
    assert str(rep / "careroute_mlops_report.md") in paths


def test_default_paths_fall_back_to_the_repo_layout(monkeypatch):
    monkeypatch.delenv("CAREROUTE_MONITOR_DIR", raising=False)
    monkeypatch.delenv("CAREROUTE_REPORT_DIR", raising=False)

    paths = pii_egress.default_artifacts()

    assert any(p.endswith("inference_log.jsonl") for p in paths)
    assert any(p.endswith("careroute_mlops_report.md") for p in paths)
