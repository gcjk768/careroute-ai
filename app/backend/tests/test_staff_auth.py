"""[AI-Security] Staff endpoints require the shared key when one is configured.

OWASP LLM02 asks for "strict inference-time access control"; AIC Day 3 asks
for RBAC. Until 2026-09-15 the escalation queue, the clinician decision POST,
per-case audit trails and session history answered anyone who could reach
port 8000 — including a patient. This pins the smallest honest control: a
shared key, enforced server-side, with the open/protected state visible on
/api/health so an unprotected deployment can never be mistaken for a
protected one.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)

STAFF_PATHS = [
    ("get", "/api/escalations"),
    ("get", "/api/escalations/esc_does_not_exist"),
    ("post", "/api/escalations/esc_does_not_exist/decision"),
    ("get", "/api/cases/case_does_not_exist/audit"),
    ("get", "/api/sessions/sess_does_not_exist/history"),
]


def _call(method: str, path: str, **kw):
    if method == "post":
        return client.post(path, json={"decision": "confirmed"}, **kw)
    return client.get(path, **kw)


@pytest.mark.parametrize("method,path", STAFF_PATHS)
def test_staff_endpoints_reject_missing_key_when_configured(monkeypatch, method, path):
    monkeypatch.setattr(config, "STAFF_API_KEY", "test-staff-key")
    resp = _call(method, path)
    assert resp.status_code == 401
    assert "X-Staff-Key" in resp.json()["detail"]


@pytest.mark.parametrize("method,path", STAFF_PATHS)
def test_staff_endpoints_reject_wrong_key(monkeypatch, method, path):
    monkeypatch.setattr(config, "STAFF_API_KEY", "test-staff-key")
    resp = _call(method, path, headers={"X-Staff-Key": "not-the-key"})
    assert resp.status_code == 401


def test_correct_key_reaches_the_endpoint(monkeypatch):
    monkeypatch.setattr(config, "STAFF_API_KEY", "test-staff-key")
    resp = client.get("/api/escalations", headers={"X-Staff-Key": "test-staff-key"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    # Authorised but nonexistent resources still 404 — auth is checked first,
    # existence second, so an unauthenticated caller cannot enumerate ids.
    resp = client.get("/api/escalations/esc_does_not_exist", headers={"X-Staff-Key": "test-staff-key"})
    assert resp.status_code == 404


def test_patient_endpoints_never_require_the_key(monkeypatch):
    """The key guards STAFF surfaces only. A patient must always be able to
    triage, read health and rate a decision without it."""
    monkeypatch.setattr(config, "STAFF_API_KEY", "test-staff-key")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/fairness").status_code == 200
    resp = client.post("/api/triage/stream", json={"text": "mild sore throat"})
    assert resp.status_code == 200


def test_open_mode_is_visible_on_health(monkeypatch):
    monkeypatch.setattr(config, "STAFF_API_KEY", "")
    assert client.get("/api/health").json()["staffAuth"] == "open"
    assert client.get("/api/escalations").status_code == 200

    monkeypatch.setattr(config, "STAFF_API_KEY", "test-staff-key")
    assert client.get("/api/health").json()["staffAuth"] == "api-key"
