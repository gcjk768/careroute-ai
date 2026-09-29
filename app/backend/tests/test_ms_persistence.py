"""[Microservices] A restarted gateway still has every escalation a clinician has not seen."""
from __future__ import annotations

import fakeredis
import pytest
import redis

from app.audit import AuditLog
from app.models import Acuity, CaseRecord
from app.store import Store, now_iso


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture(autouse=True)
def _ground_truth_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("CAREROUTE_GROUND_TRUTH_LOG", str(tmp_path / "gt.jsonl"))


def _case(case_id: str = "case-1") -> CaseRecord:
    return CaseRecord(
        caseId=case_id, sessionId="sess-1", rawText="chest pain", language="en", isVoice=False,
        normalisedSymptoms="chest pain", acuity=Acuity.from_code("P2_EMERGENT"), confidence=0.4,
        careTier="ED", escalated=True, rationale="r", citations=[], evidence=["chest pain"],
        safetyTriggered=False, createdAt=now_iso(),
    )


def test_cases_and_escalations_survive_a_restart(r):
    first = Store(redis_client=r)
    case = _case()
    first.save_case(case)
    escalation = first.create_escalation_from_case(case, "low confidence")

    second = Store(redis_client=r)
    assert second.get_case(case.caseId) == case
    assert second.get_escalation(escalation.id) == escalation
    # The episodic-memory digest must still verify after the round trip.
    assert [c.caseId for c in second.recall_session("sess-1")] == [case.caseId]


def test_seed_escalations_are_not_duplicated_on_restart(r):
    first_ids = {e.id for e in Store(redis_client=r).list_escalations()}
    assert {e.id for e in Store(redis_client=r).list_escalations()} == first_ids


def test_a_decision_survives_a_restart(r):
    first = Store(redis_client=r)
    escalation = first.create_escalation_from_case(_case(), "why")
    first.decide_escalation(escalation.id, "agree", "seen", "dr-a", None)
    assert Store(redis_client=r).get_escalation(escalation.id).status == "decided"


def test_redis_down_never_fails_a_case():
    dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2,
                       decode_responses=True)
    store = Store(redis_client=dead)
    store.save_case(_case())
    assert store.get_case("case-1") is not None


def test_audit_chain_survives_a_restart(r):
    first = AuditLog(redis_client=r)
    first.record("case-1", actor="intake", action="llm", detail="a")
    first.record("case-1", actor="classifier", action="model", detail="b")

    second = AuditLog(redis_client=r)
    assert second.for_case("case-1") == first.for_case("case-1")
    assert second.verify_chain("case-1") is True
    assert second.record("case-1", actor="safety", action="ok", detail="c").seq == 3
    assert second.verify_chain("case-1") is True
