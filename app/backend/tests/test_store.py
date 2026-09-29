"""Tests for the in-memory store — HITL ground-truth capture, episodic recall,
and the fairness snapshot (app.store)."""
from __future__ import annotations

import json

from app import store as store_mod
from app.models import Acuity, CaseRecord
from app.store import Store, build_fairness_response, valid_session_id


def test_decide_escalation_records_ground_truth(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREROUTE_GROUND_TRUTH_LOG", str(tmp_path / "gt.jsonl"))
    s = Store()
    esc = s.list_escalations()[0]
    agreed = s.decide_escalation(esc.id, "approve", "ok", "Dr X", esc.acuity.code)
    assert agreed.modelAgreed is True and agreed.finalAcuity == esc.acuity.code

    esc2 = s.list_escalations()[1]
    other = "P1_RESUSCITATION" if esc2.acuity.code != "P1_RESUSCITATION" else "P5_SELF_CARE"
    overridden = s.decide_escalation(esc2.id, "modify", "", "Dr X", other)
    assert overridden.modelAgreed is False

    rows = [json.loads(x) for x in (tmp_path / "gt.jsonl").read_text().strip().splitlines()]
    assert len(rows) == 2
    assert {r["agreement"] for r in rows} == {"agreed", "overridden"}
    # [XRAI] Accountability: the row names the clinician who labelled the case.
    assert {r["clinician"] for r in rows} == {"Dr X"}
    assert agreed.clinician == "Dr X"


def test_decide_escalation_without_label(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREROUTE_GROUND_TRUTH_LOG", str(tmp_path / "gt.jsonl"))
    s = Store()
    esc = s.list_escalations()[0]
    out = s.decide_escalation(esc.id, "approve", "note", "Dr Y")  # no finalAcuity
    assert out.modelAgreed is None
    row = json.loads((tmp_path / "gt.jsonl").read_text().strip())
    assert row["agreement"] == "unlabelled"


def test_decide_escalation_unknown_id_returns_none():
    assert Store().decide_escalation("nope", "a", "", "c") is None


def test_valid_session_id():
    assert valid_session_id("abc-123.x:y") is True
    assert valid_session_id("bad id!") is False
    assert valid_session_id(None) is False
    assert valid_session_id("") is False


def test_save_and_recall_session_scoped():
    s = Store()
    rec = CaseRecord(
        caseId="c1", sessionId="sess-1", rawText="x", language="en", isVoice=False,
        normalisedSymptoms="cough", acuity=Acuity.from_code("P3_URGENT"), confidence=0.6,
        careTier="GP", escalated=False, rationale="", citations=[], evidence=["cough"],
        safetyTriggered=False, createdAt=store_mod.now_iso(),
    )
    s.save_case(rec)
    got = s.recall_session("sess-1")
    assert len(got) == 1 and got[0].caseId == "c1"
    # A different / invalid session recalls nothing (episodic scoping).
    assert s.recall_session("other-session") == []
    assert s.recall_session("bad id!") == []


def _case(case_id: str = "c1", session: str = "sess-1", acuity: str = "P3_URGENT") -> CaseRecord:
    return CaseRecord(
        caseId=case_id, sessionId=session, rawText="x", language="en", isVoice=False,
        normalisedSymptoms="cough", acuity=Acuity.from_code(acuity), confidence=0.6,
        careTier="GP", escalated=False, rationale="", citations=[], evidence=["cough"],
        safetyTriggered=False, createdAt=store_mod.now_iso(),
    )


def test_tampered_episodic_memory_is_excluded_from_recall():
    """[AI-Security][ASI06] Memory poisoning must not survive into a later triage.

    A record mutated after it was written is the persistent-influence case OWASP
    calls Memory & Context Poisoning: `recall_session` replays prior visits into a
    new triage, so a downgraded acuity in history biases the next decision.
    """
    s = Store()
    s.save_case(_case(acuity="P1_RESUSCITATION"))
    assert len(s.recall_session("sess-1")) == 1

    # Downgrade the stored emergency in place, exactly as a compromised writer
    # or a tampered SQL row would.
    s.cases["c1"].acuity = Acuity.from_code("P5_SELF_CARE")

    assert s.recall_session("sess-1") == []  # fails closed, not "returned with a warning"
    report = s.verify_episodic_memory("sess-1")
    assert report["verified"] is False and report["tampered"] == ["c1"]


def test_missing_digest_is_treated_as_tampered():
    """Absent is not clean: a record with no digest cannot be shown to be genuine."""
    s = Store()
    s.save_case(_case())
    del s._memory_digests["c1"]

    assert s.recall_session("sess-1") == []


def test_untampered_memory_verifies_clean():
    """The control test. A check that never passes is as useless as one that never fires."""
    s = Store()
    s.save_case(_case("c1"))
    s.save_case(_case("c2"))

    assert len(s.recall_session("sess-1")) == 2
    assert s.verify_episodic_memory("sess-1") == {
        "sessionId": "sess-1", "tracked": 2, "tampered": [], "verified": True,
    }


def test_build_fairness_response():
    fr = build_fairness_response()
    assert fr.overallAccuracy > 0 and fr.redFlagRecall > 0
    assert fr.subgroups and fr.modelVersion
