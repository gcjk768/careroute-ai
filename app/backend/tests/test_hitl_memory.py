"""HITL long-term memory, learning loop and SLA (app/memory, agents/hitl.py, store.py).

1. PRECEDENT MEMORY — a decided escalation becomes a de-identified precedent
   (symptom feature vector + model/clinician acuity), retrieved by cosine
   similarity on new cases. Precedents may only make HITL MORE cautious.
2. LEARNING LOOP — clinician disagreements are queued for retraining.
3. SLA — an undecided escalation past CAREROUTE_HITL_SLA_MINUTES is breached,
   evaluated on read against an injectable clock.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app import store as store_mod
from app.agents import HumanInTheLoopAgent
from app.memory import precedents, retrain_queue
from app.models import Acuity, CaseRecord
from app.store import Store, now_iso

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "agents"))
from harness import make_case, run_and_check  # noqa: E402

PII_TEXT = "Tan Ah Kow S1234567D 91234567 crushing chest pain radiating to left arm"


@pytest.fixture
def memory(tmp_path, monkeypatch):
    mem = precedents.PrecedentMemory(path=str(tmp_path / "precedents.jsonl"))
    monkeypatch.setattr(precedents, "MEMORY", mem)
    return mem


def _case(text: str, code: str, case_id: str = "case_abc") -> CaseRecord:
    return CaseRecord(
        caseId=case_id, sessionId="sess-1", rawText=text, language="en", isVoice=False,
        normalisedSymptoms=text, acuity=Acuity.from_code(code), confidence=0.4,
        careTier="GP", escalated=True, rationale="r", citations=[], evidence=[],
        safetyTriggered=False, createdAt=now_iso(),
    )


def _decide(store: Store, text: str, model: str, clinician: str, case_id: str = "case_abc"):
    esc = store.create_escalation_from_case(_case(text, model, case_id), "low confidence")
    return store.decide_escalation(esc.id, "modify", "called patient Tan", "Dr Lim", clinician)


# --------------------------------------------------------------------------
# 1. Precedent memory
# --------------------------------------------------------------------------

def test_decision_stores_a_deidentified_precedent(memory, tmp_path, monkeypatch):
    monkeypatch.setenv("CAREROUTE_GROUND_TRUTH_LOG", str(tmp_path / "gt.jsonl"))
    _decide(Store(), PII_TEXT, "P3_URGENT", "P1_RESUSCITATION")

    raw = (tmp_path / "precedents.jsonl").read_text(encoding="utf-8")
    row = json.loads(raw)
    assert set(row) == {"ts", "v", "modelAcuity", "clinicianAcuity", "agreed"}
    assert row["modelAcuity"] == "P3_URGENT" and row["clinicianAcuity"] == "P1_RESUSCITATION"
    assert row["agreed"] is False
    assert set(row["v"]) <= {0, 1} and sum(row["v"]) >= 1
    # No PII / free text / linkable ids anywhere in the persisted row.
    for needle in ("Tan", "S1234567D", "91234567", "chest", "case_abc", "Dr Lim", "called"):
        assert needle not in raw


def test_unlabelled_decision_stores_no_precedent(memory, tmp_path, monkeypatch):
    monkeypatch.setenv("CAREROUTE_GROUND_TRUTH_LOG", str(tmp_path / "gt.jsonl"))
    s = Store()
    esc = s.create_escalation_from_case(_case("chest pain", "P3_URGENT"), "r")
    s.decide_escalation(esc.id, "approve", "", "Dr Lim")  # no finalAcuity -> no label
    assert memory.rows() == []


def test_nearest_is_cosine_ranked_and_ignores_dissimilar(memory):
    chest = precedents.vector("chest pain")
    head = precedents.vector("mild headache")
    memory.add({"ts": "t", "v": head, "modelAcuity": "P4_NON_URGENT",
                "clinicianAcuity": "P4_NON_URGENT", "agreed": True})
    memory.add({"ts": "t", "v": chest, "modelAcuity": "P3_URGENT",
                "clinicianAcuity": "P2_EMERGENT", "agreed": False})
    hits = memory.nearest(precedents.vector("chest pain and sweating"))
    assert [r["clinicianAcuity"] for _s, r in hits] == ["P2_EMERGENT"]
    assert hits[0][0] == pytest.approx(1.0)


def test_redis_backend_survives_restart():
    r = fakeredis.FakeRedis(decode_responses=True)
    precedents.PrecedentMemory(redis_client=r).add(
        {"ts": "t", "v": [1, 0], "modelAcuity": "P3_URGENT", "clinicianAcuity": "P2_EMERGENT",
         "agreed": False})
    assert len(precedents.PrecedentMemory(redis_client=r).rows()) == 1


def test_file_backend_is_size_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(precedents, "_MAX_BYTES", 200)
    mem = precedents.PrecedentMemory(path=str(tmp_path / "p.jsonl"))
    for _ in range(20):
        mem.add({"ts": "t", "v": [1, 0, 0], "modelAcuity": "P3_URGENT",
                 "clinicianAcuity": "P2_EMERGENT", "agreed": False})
    assert 0 < len(mem.rows()) < 20


def _seed(memory, text: str, model: str, clinician: str, n: int) -> None:
    for _ in range(n):
        memory.add({"ts": "t", "v": precedents.vector(text), "modelAcuity": model,
                    "clinicianAcuity": clinician, "agreed": model == clinician})


def test_hitl_escalates_when_clinicians_uptriaged_similar_cases(memory):
    _seed(memory, "chest pain", "P4_NON_URGENT", "P2_EMERGENT", 3)
    state = make_case(confidence=0.95, acuity_code="P4_NON_URGENT",
                      normalised_symptoms="chest pain")
    result, state = run_and_check(HumanInTheLoopAgent(), state)
    assert state.escalated is True
    assert "Similar past cases: clinician chose P2_EMERGENT in 3/3" in state.escalation_reason
    assert result["precedents"]["upTriaged"] == 3


def test_hitl_never_deescalates_on_precedent(memory):
    """Clinicians agreed / down-triaged similar cases: a safety floor still stands."""
    _seed(memory, "chest pain", "P2_EMERGENT", "P5_SELF_CARE", 5)
    state = make_case(confidence=0.95, acuity_code="P2_EMERGENT", normalised_symptoms="chest pain",
                      safety_triggered=True, safety_rule="chest_pain")
    result, state = run_and_check(HumanInTheLoopAgent(), state)
    assert state.escalated is True
    # Surfaced for the clinician, never acted on.
    assert "clinician chose P5_SELF_CARE in 5/5" in state.escalation_reason
    assert result["precedents"]["escalate"] is False


def test_hitl_confident_case_with_agreeing_precedents_proceeds(memory):
    _seed(memory, "chest pain", "P4_NON_URGENT", "P4_NON_URGENT", 5)
    state = make_case(confidence=0.95, acuity_code="P4_NON_URGENT", normalised_symptoms="chest pain")
    _result, state = run_and_check(HumanInTheLoopAgent(), state)
    assert state.escalated is False


def test_hitl_needs_a_quorum_of_uptriaged_precedents(memory):
    _seed(memory, "chest pain", "P4_NON_URGENT", "P2_EMERGENT", 2)  # below MIN_UPTRIAGE
    state = make_case(confidence=0.95, acuity_code="P4_NON_URGENT", normalised_symptoms="chest pain")
    _result, state = run_and_check(HumanInTheLoopAgent(), state)
    assert state.escalated is False


# --------------------------------------------------------------------------
# 2. Learning loop — retrain queue
# --------------------------------------------------------------------------

def test_disagreement_is_queued_for_retraining(memory, tmp_path, monkeypatch):
    monkeypatch.setenv("CAREROUTE_GROUND_TRUTH_LOG", str(tmp_path / "gt.jsonl"))
    monkeypatch.setenv("CAREROUTE_RETRAIN_QUEUE", str(tmp_path / "rq.jsonl"))
    monkeypatch.setenv("CAREROUTE_INFERENCE_LOG", str(tmp_path / "missing.jsonl"))
    s = Store()
    _decide(s, PII_TEXT, "P3_URGENT", "P1_RESUSCITATION", case_id="case_1")
    _decide(s, "mild headache", "P4_NON_URGENT", "P4_NON_URGENT", case_id="case_2")  # agreed

    raw = (tmp_path / "rq.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(x) for x in raw.splitlines()]
    assert len(rows) == 1 and retrain_queue.size() == 1
    from app.ml.data import ACUITY_INDEX_TO_CODE
    from app.ml.features import FEATURE_NAMES
    row = rows[0]
    assert len(row["features"]) == len(FEATURE_NAMES)
    assert ACUITY_INDEX_TO_CODE[row["label"]] == "P1_RESUSCITATION"
    assert row["featureSource"] == "text" and row["ageProvided"] is False
    for needle in ("Tan", "S1234567D", "chest", "case_1", "Dr Lim"):
        assert needle not in raw


def test_retrain_queue_prefers_the_served_feature_vector(memory, tmp_path, monkeypatch):
    monkeypatch.setenv("CAREROUTE_GROUND_TRUTH_LOG", str(tmp_path / "gt.jsonl"))
    monkeypatch.setenv("CAREROUTE_RETRAIN_QUEUE", str(tmp_path / "rq.jsonl"))
    inf = tmp_path / "inf.jsonl"
    monkeypatch.setenv("CAREROUTE_INFERENCE_LOG", str(inf))
    from app.ml.features import FEATURE_NAMES
    served = [0.0] * (len(FEATURE_NAMES) - 1) + [1.0]
    inf.write_text(json.dumps({"caseId": "case_9", "features": served, "acuity": "P3_URGENT",
                               "ageProvided": True}) + "\n", encoding="utf-8")
    _decide(Store(), "chest pain", "P3_URGENT", "P2_EMERGENT", case_id="case_9")
    row = json.loads((tmp_path / "rq.jsonl").read_text(encoding="utf-8"))
    assert row["features"] == served and row["featureSource"] == "inference_log"
    assert row["ageProvided"] is True


# --------------------------------------------------------------------------
# 3. SLA
# --------------------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    now = {"t": datetime.now(UTC)}
    monkeypatch.setattr(store_mod, "_utcnow", lambda: now["t"])
    return now


def test_sla_breach_is_evaluated_on_read(clock, monkeypatch, tmp_path):
    monkeypatch.setenv("CAREROUTE_GROUND_TRUTH_LOG", str(tmp_path / "gt.jsonl"))
    monkeypatch.setenv("CAREROUTE_HITL_SLA_MINUTES", "15")
    s = Store()
    esc = s.create_escalation_from_case(_case("chest pain", "P2_EMERGENT"), "r")
    assert s.get_escalation(esc.id).slaBreached is False
    clock["t"] += timedelta(minutes=16)
    assert s.get_escalation(esc.id).slaBreached is True
    assert all(e.slaBreached for e in s.list_escalations())  # seeds are overdue too
    assert s.open_overdue() == 5


def test_decided_in_time_is_not_breached_later(clock, monkeypatch, tmp_path):
    monkeypatch.setenv("CAREROUTE_GROUND_TRUTH_LOG", str(tmp_path / "gt.jsonl"))
    s = Store()
    esc = s.create_escalation_from_case(_case("chest pain", "P2_EMERGENT"), "r")
    clock["t"] += timedelta(minutes=5)
    s.decide_escalation(esc.id, "approve", "", "Dr Lim")
    clock["t"] += timedelta(hours=2)
    assert s.get_escalation(esc.id).slaBreached is False


@pytest.fixture
def api(clock, monkeypatch, tmp_path, memory):
    monkeypatch.setenv("CAREROUTE_GROUND_TRUTH_LOG", str(tmp_path / "gt.jsonl"))
    monkeypatch.setenv("CAREROUTE_HITL_SLA_MINUTES", "15")
    s = Store()
    monkeypatch.setattr(main_mod, "store", s)
    return TestClient(main_mod.app), s


def test_api_surfaces_sla_and_patient_safety_net(api, clock):
    client, s = api
    p2 = s.create_escalation_from_case(_case("chest pain", "P2_EMERGENT", "case_p2"), "r")
    p4 = s.create_escalation_from_case(_case("mild headache", "P4_NON_URGENT", "case_p4"), "r")

    status = client.get("/api/cases/case_p2/status").json()
    assert status["reviewStatus"] == "pending" and status["slaBreached"] is False
    assert status["safetyNet"] is None

    clock["t"] += timedelta(minutes=20)
    listed = {e["id"]: e for e in client.get("/api/escalations").json()}
    assert listed[p2.id]["slaBreached"] is True and listed[p2.id]["slaDueAt"]
    assert client.get(f"/api/escalations/{p4.id}").json()["slaBreached"] is True

    status = client.get("/api/cases/case_p2/status").json()
    assert status["slaBreached"] is True and "995" in status["safetyNet"]
    # Breached, but not P1/P2: no emergency safety-net message.
    assert client.get("/api/cases/case_p4/status").json()["safetyNet"] is None
    assert client.get("/api/cases/nope/status").status_code == 404

    metrics_text = client.get("/metrics").text
    if "disabled" not in metrics_text:
        assert "careroute_hitl_sla_breached_total" in metrics_text
        assert "careroute_hitl_open_overdue 6.0" in metrics_text
        assert "careroute_retrain_queue_total" in metrics_text
