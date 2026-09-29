"""Tests for the executive report generator (app.ml.report).

Covers the PDF builder, the Markdown twin, and the security-scan artifact
parsing — WITHOUT training a model, by pointing CAREROUTE_MODEL_DIR at a fake
`model_audit.json` and dropping sample scanner artifacts in a fake project dir.
"""
from __future__ import annotations

import json

import pytest

from app.ml import report


def _audit_payload() -> dict:
    return {
        "overallAccuracy": 0.921,
        "redFlagRecall": 0.962,
        "fairnessGapBefore": 0.495,
        "fairnessGapAfter": 0.114,
        "counterfactual": {"attribute": "sex", "n": 10, "sexFlipRate": 0.0, "meanAcuityDelta": 0.0},
        "demographicParity": {"byGroup": {"a": 0.21, "b": 0.18}, "statisticalParityDifference": 0.03},
        "equalOpportunity": {"byGroup": {"a": 0.94, "b": 0.97}, "equalOpportunityGap": 0.03},
        "calibration": {"method": "isotonic", "ece": 0.014, "brier": 0.112},
        "integrity": {"schema": 2, "versionId": "abc123", "dataSha256": "d" * 64, "modelSha256": "m" * 64},
        "drift": {"data": 0.03, "target": 0.02, "concept": 0.04},
        "modelVersion": "careroute-triage-rf-abc123 (rf/200, cal/isotonic)",
        "updatedAt": "2026-07-18T00:00:00+00:00",
    }


@pytest.fixture()
def report_env(tmp_path, monkeypatch):
    """Point the report at a fake model audit + fake project dir (no model)."""
    md = tmp_path / "models"
    md.mkdir()
    (md / "model_audit.json").write_text(json.dumps(_audit_payload()), encoding="utf-8")
    monkeypatch.setenv("CAREROUTE_MODEL_DIR", str(md))
    monkeypatch.setenv("CI_PROJECT_DIR", str(tmp_path))
    # No drift file by default -> report falls back to the audit's drift block.
    monkeypatch.setenv("CAREROUTE_MONITOR_DIR", str(tmp_path / "nope"))
    return tmp_path


def _write_scan_artifacts(root):
    (root / "gitleaks.json").write_text("[]", encoding="utf-8")
    (root / "semgrep.sarif").write_text(json.dumps(
        {"runs": [{"results": [{"level": "warning"}, {"level": "error"}]}]}), encoding="utf-8")
    (root / "bandit.json").write_text(json.dumps({"results": []}), encoding="utf-8")
    (root / "trivy-fs.json").write_text(json.dumps(
        {"Results": [{"Vulnerabilities": [{"Severity": "HIGH"}, {"Severity": "CRITICAL"}]}]}), encoding="utf-8")
    (root / "pip-audit.json").write_text(json.dumps(
        {"dependencies": [{"name": "foo", "vulns": [{"id": "CVE-1"}]}]}), encoding="utf-8")
    (root / "npm-audit.json").write_text(json.dumps(
        {"metadata": {"vulnerabilities": {"low": 1, "high": 0, "total": 1}}}), encoding="utf-8")
    (root / "modelscan.json").write_text(json.dumps({"issues": []}), encoding="utf-8")
    (root / "hadolint-backend.json").write_text(json.dumps([{"level": "info"}]), encoding="utf-8")
    (root / "hadolint-frontend.json").write_text("[]", encoding="utf-8")


def test_markdown_report_has_all_sections(report_env):
    out = report.build_markdown(str(report_env / "r.md"))
    text = (report_env / "r.md").read_text(encoding="utf-8")
    assert out.endswith("r.md")
    for heading in ("# CareRoute AI", "## Executive summary", "## Model quality & release gates",
                    "## Responsible-AI & fairness", "## Drift monitoring", "## Security scan results"):
        assert heading in text, heading
    # Values from the fake audit come through.
    assert "0.921" in text and "0.962" in text
    assert "clears" in text  # gate passes at acc 0.921 / recall 0.962


def test_markdown_renders_scan_results(report_env):
    _write_scan_artifacts(report_env)
    report.build_markdown(str(report_env / "r.md"))
    text = (report_env / "r.md").read_text(encoding="utf-8")
    assert "Gitleaks (secrets)" in text and "PASS" in text
    assert "Trivy (SCA/CVE)" in text and "BLOCKING" in text  # CRITICAL present
    assert "Semgrep (SAST)" in text and "REVIEW" in text
    assert "Hadolint (Dockerfiles)" in text  # both per-file JSONs merged


def test_scan_results_parsing_counts(report_env):
    _write_scan_artifacts(report_env)
    rows = {r[0]: (r[1], r[2]) for r in report._scan_results()}
    assert rows["Gitleaks (secrets)"][0] == "0"
    assert rows["Semgrep (SAST)"][0] == "2"
    assert rows["Trivy (SCA/CVE)"][0] == "2"
    assert "BLOCKING" in rows["Trivy (SCA/CVE)"][1]
    assert rows["Hadolint (Dockerfiles)"][0] == "1"  # 1 (backend) + 0 (frontend)


def test_scan_results_empty_when_no_artifacts(report_env):
    assert report._scan_results() == []
    assert report._notable_findings() == []


def test_notable_findings_lists_actual_findings(report_env):
    _write_scan_artifacts(report_env)
    found = report._notable_findings()
    # Rows are [scanner, severity, finding]; Trivy CRITICAL must be present.
    scanners = {r[0] for r in found}
    assert "Trivy" in scanners and "Semgrep" in scanners
    assert any(r[1] == "CRITICAL" for r in found)
    # The Markdown twin shows the findings-summary section with the list.
    report.build_markdown(str(report_env / "r.md"))
    text = (report_env / "r.md").read_text(encoding="utf-8")
    assert "## Findings summary" in text
    assert "CRITICAL" in text


def test_pdf_report_is_written(report_env):
    out = report.build(str(report_env / "r.pdf"))
    data = (report_env / "r.pdf").read_bytes()
    assert out.endswith("r.pdf")
    assert data[:4] == b"%PDF"  # a real PDF
    assert len(data) > 1500


def test_report_uses_drift_file_when_present(report_env):
    mon = report_env / "mon"
    mon.mkdir()
    (mon / "drift_report.json").write_text(json.dumps({
        "backend": "evidently", "driftShare": 0.1,
        "dataDriftPSI": 0.05, "targetDriftPSI": 0.02,
        "performance": {"referenceAccuracy": 0.94, "currentAccuracy": 0.90},
    }), encoding="utf-8")
    import os
    os.environ["CAREROUTE_MONITOR_DIR"] = str(mon)
    try:
        report.build_markdown(str(report_env / "r.md"))
        text = (report_env / "r.md").read_text(encoding="utf-8")
        assert "evidently" in text
    finally:
        os.environ["CAREROUTE_MONITOR_DIR"] = str(report_env / "nope")


def test_main_writes_both_formats(report_env, monkeypatch):
    monkeypatch.setenv("CAREROUTE_REPORT_DIR", str(report_env / "out"))
    rc = report.main()
    assert rc == 0
    assert (report_env / "out" / "careroute_mlops_report.pdf").exists()
    assert (report_env / "out" / "careroute_mlops_report.md").exists()


# ---------------------------------------------------------------------------
# Scanners added alongside the expanded security-scan stage. The SARIF-emitting
# tools all go through ONE generic reader, so the tests below check that reader
# (levels, security-severity, locations) plus each bespoke parser.
# ---------------------------------------------------------------------------
def _sarif(results):
    return json.dumps({"version": "2.1.0", "runs": [{"results": results}]})


def _write_new_scan_artifacts(root):
    # SARIF family — one per registered scanner, exercising both severity paths.
    (root / "checkov.sarif").write_text(_sarif([
        {"ruleId": "CKV_DOCKER_2", "level": "warning",
         "message": {"text": "Ensure HEALTHCHECK is set"},
         "locations": [{"physicalLocation": {
             "artifactLocation": {"uri": "backend/Dockerfile"},
             "region": {"startLine": 3}}}]},
    ]), encoding="utf-8")
    # security-severity 9.1 must beat level=warning and land as CRITICAL.
    (root / "njsscan.sarif").write_text(_sarif([
        {"ruleId": "node_insecure_random", "level": "warning",
         "properties": {"security-severity": "9.1"},
         "message": {"text": "Insecure randomness"}},
    ]), encoding="utf-8")
    (root / "osv.sarif").write_text(_sarif([]), encoding="utf-8")

    (root / "gl-sast-report.json").write_text(json.dumps(
        {"vulnerabilities": [{"severity": "High", "name": "Hardcoded password",
                              "location": {"file": "app/config.py"}}]}), encoding="utf-8")
    (root / "gl-secret-detection-report.json").write_text(json.dumps(
        {"vulnerabilities": []}), encoding="utf-8")

    # TruffleHog + Nuclei are JSON-LINES, not JSON documents.
    (root / "trufflehog.json").write_text(
        json.dumps({"DetectorName": "AWS", "Verified": True,
                    "SourceMetadata": {"Data": {"Filesystem": {"file": "a.env"}}}}) + "\n"
        + json.dumps({"DetectorName": "Slack", "Verified": False,
                      "SourceMetadata": {"Data": {}}}) + "\n"
        + "not json at all\n",  # a truncated line must not break the report
        encoding="utf-8")
    (root / "nuclei.json").write_text(
        json.dumps({"info": {"severity": "medium", "name": "Missing CSP"},
                    "matched-at": "http://127.0.0.1:8000/"}) + "\n", encoding="utf-8")

    (root / "detect-secrets.json").write_text(json.dumps(
        {"results": {"backend/app/config.py": [{"type": "Secret Keyword", "line_number": 12}]}}),
        encoding="utf-8")
    (root / "retire.json").write_text(json.dumps([
        {"file": "frontend/x.js", "results": [
            {"component": "jquery", "version": "1.4.1", "vulnerabilities": [
                {"severity": "high", "identifiers": {"CVE": ["CVE-2011-4969"]}}]}]},
    ]), encoding="utf-8")
    (root / "horusec.json").write_text(json.dumps(
        {"analysisVulnerabilities": [{"vulnerabilities": {
            "severity": "MEDIUM", "details": "Weak hash", "file": "backend/app/x.py"}}]}),
        encoding="utf-8")
    (root / "zap-api.json").write_text(json.dumps(
        {"site": [{"alerts": [{"riskdesc": "Medium (High)", "alert": "Missing X-Frame-Options",
                               "instances": [{"uri": "/"}, {"uri": "/api"}]}]}]}), encoding="utf-8")
    (root / "nikto.json").write_text(json.dumps(
        {"vulnerabilities": [{"id": "999957", "msg": "Server leaks inodes"}]}), encoding="utf-8")


def test_sarif_reader_handles_levels_severity_and_locations(report_env):
    _write_new_scan_artifacts(report_env)
    rows = {r[0]: (r[1], r[2]) for r in report._scan_results()}
    # level=warning -> MEDIUM, and the file:line shows up in the finding text.
    assert rows["Checkov (IaC/config)"][0] == "1"
    assert "MEDIUM: 1" in rows["Checkov (IaC/config)"][1]
    # properties.security-severity 9.1 outranks level=warning.
    assert "CRITICAL: 1" in rows["njsscan (Node SAST)"][1]
    # An empty SARIF run is a PASS, not a missing scanner.
    assert rows["OSV-Scanner (deps)"] == ("0", "PASS - no findings")

    findings = {r[0]: r for r in report._notable_findings()}
    assert "(Dockerfile:3)" in findings["Checkov"][2]


def test_absent_sarif_scanner_is_skipped_not_reported_clean(report_env):
    """A scanner that never ran must be ABSENT from the table — reporting it as
    'PASS - no findings' would claim coverage the pipeline did not actually have."""
    _write_new_scan_artifacts(report_env)
    labels = {r[0] for r in report._scan_results()}
    assert "OSV-Scanner (deps)" in labels      # wrote an empty SARIF -> ran, clean
    assert "KICS (IaC/config)" not in labels   # no artifact -> did not run
    assert "Grype (SCA)" not in labels


def test_trufflehog_verified_secret_is_blocking(report_env):
    _write_new_scan_artifacts(report_env)
    rows = {r[0]: (r[1], r[2]) for r in report._scan_results()}
    # Two well-formed lines; the malformed third is ignored.
    assert rows["TruffleHog (secrets)"][0] == "2"
    assert "BLOCKING" in rows["TruffleHog (secrets)"][1]
    assert "1 VERIFIED" in rows["TruffleHog (secrets)"][1]
    # A verified credential outranks an unverified candidate in the findings list.
    th = [r for r in report._notable_findings() if r[0] == "TruffleHog"]
    assert th[0][1] == "CRITICAL" and "VERIFIED" in th[0][2]


def test_remaining_new_scanners_parse(report_env):
    _write_new_scan_artifacts(report_env)
    rows = {r[0]: (r[1], r[2]) for r in report._scan_results()}
    assert rows["GitLab SAST (native)"] == ("1", "REVIEW - HIGH: 1")
    assert rows["GitLab Secret Detection (native)"][1].startswith("PASS")
    assert rows["detect-secrets"][0] == "1"
    assert rows["Retire.js (JS libs)"][0] == "1"
    assert rows["Horusec (multi-language SAST)"][0] == "1"
    assert rows["Nuclei (DAST templates)"][0] == "1"
    assert rows["OWASP ZAP (API scan)"][0] == "1"
    assert rows["Nikto (web server)"][0] == "1"
    # ZAP's "Medium (High)" riskdesc parses to the MEDIUM bucket.
    assert "MEDIUM: 1" in rows["OWASP ZAP (API scan)"][1]


def test_new_scanners_reach_the_markdown_report(report_env):
    _write_new_scan_artifacts(report_env)
    report.build_markdown(str(report_env / "r.md"))
    text = (report_env / "r.md").read_text(encoding="utf-8")
    for label in ("Checkov (IaC/config)", "TruffleHog (secrets)", "GitLab SAST (native)",
                  "Retire.js (JS libs)", "Nuclei (DAST templates)", "OWASP ZAP (API scan)"):
        assert label in text, label


def test_load_test_summary_is_reported(report_env):
    (report_env / "locust-summary.json").write_text(json.dumps(
        {"requests": 4210, "failures": 0, "failRatio": 0.0, "medianMs": 12,
         "p95Ms": 240, "maxMs": 900, "rps": 70.2,
         "thresholds": {"p95Ms": 800, "maxFailRatio": 0.01},
         "breaches": [], "verdict": "PASS"}), encoding="utf-8")
    rows = {r[0]: (r[1], r[2]) for r in report._scan_results()}
    assert rows["Locust (load test)"][0] == "4210"
    assert rows["Locust (load test)"][1].startswith("PASS")
    assert "240ms" in rows["Locust (load test)"][1]


def test_load_test_slo_breach_is_surfaced(report_env):
    (report_env / "locust-summary.json").write_text(json.dumps(
        {"requests": 100, "failures": 30, "failRatio": 0.3, "p95Ms": 1900, "rps": 3.1,
         "breaches": ["p95 1900ms > 800ms", "failure ratio 30.00% > 1.00%"],
         "verdict": "FAIL"}), encoding="utf-8")
    rows = {r[0]: (r[1], r[2]) for r in report._scan_results()}
    assert rows["Locust (load test)"][1].startswith("REVIEW")
    assert "p95 1900ms > 800ms" in rows["Locust (load test)"][1]


# --------------------------------------------------------------------------
# Agentic gate results (guardrail score, no-live-credentials). Added with the
# courseware alignment work; same absent-is-not-clean rule as the scanners.
# --------------------------------------------------------------------------

def test_guardrail_score_is_reported(report_env, tmp_path):
    (tmp_path / "guardrail-score.json").write_text(json.dumps({
        "recall": 1.0, "falsePositiveRate": 0.0, "bypassRate": 0.0,
        "counts": {"cases": 55, "attacks": 37, "benign": 18, "injection": 30},
        "thresholds": {"minRecall": 0.95, "maxFalsePositiveRate": 0.05, "maxBypassRate": 0.02},
        "escaped": [], "falsePositives": [], "breaches": [], "verdict": "PASS",
    }), encoding="utf-8")

    rows = report._agentic_gates()
    flat = " ".join(str(c) for r in rows for c in r)

    assert "Guardrail" in flat
    assert "100" in flat or "1.0" in flat


def test_guardrail_breach_is_surfaced_as_failing(report_env, tmp_path):
    (tmp_path / "guardrail-score.json").write_text(json.dumps({
        "recall": 0.80, "falsePositiveRate": 0.30, "bypassRate": 0.10,
        "counts": {"cases": 10, "attacks": 5, "benign": 5, "injection": 5},
        "thresholds": {"minRecall": 0.95, "maxFalsePositiveRate": 0.05, "maxBypassRate": 0.02},
        "escaped": ["inj01"], "falsePositives": ["ben01"],
        "breaches": ["recall 80.00% < 95.00%"], "verdict": "FAIL",
    }), encoding="utf-8")

    flat = " ".join(str(c) for r in report._agentic_gates() for c in r)

    assert "FAIL" in flat


def test_live_credentials_finding_is_reported(report_env, tmp_path):
    (tmp_path / "no-live-credentials.json").write_text(json.dumps({
        "applicable": True, "environment": "ci",
        "checked": ["OPENAI_API_KEY", "ONEMAP_EMAIL"],
        "live": ["ONEMAP_EMAIL"], "verdict": "FAIL",
    }), encoding="utf-8")

    flat = " ".join(str(c) for r in report._agentic_gates() for c in r)

    assert "ONEMAP_EMAIL" in flat
    assert "FAIL" in flat


def test_absent_agentic_artifacts_are_skipped_not_clean(report_env):
    rows = report._agentic_gates()

    flat = " ".join(str(c) for r in rows for c in r).lower()
    assert "pass" not in flat or "not run" in flat or "skipped" in flat


def test_agentic_gates_reach_the_markdown_report(report_env, tmp_path):
    (tmp_path / "guardrail-score.json").write_text(json.dumps({
        "recall": 1.0, "falsePositiveRate": 0.0, "bypassRate": 0.0,
        "counts": {"cases": 55, "attacks": 37, "benign": 18, "injection": 30},
        "thresholds": {"minRecall": 0.95, "maxFalsePositiveRate": 0.05, "maxBypassRate": 0.02},
        "escaped": [], "falsePositives": [], "breaches": [], "verdict": "PASS",
    }), encoding="utf-8")
    out = tmp_path / "r.md"

    report.build_markdown(str(out))

    text = out.read_text(encoding="utf-8")
    assert "## Agentic gate results" in text
    assert "Guardrail effectiveness (E9)" in text
