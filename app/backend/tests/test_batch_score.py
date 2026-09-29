"""[MLOps] Batch serving — the nightly re-score of the escalation queue.

A fake model throughout: this tests the BATCH job (what it reads, what it
reports, what it refuses to do), not the classifier. Training a real forest here
would make a 200ms test a 4-minute one and measure nothing extra.
"""
import json

import pytest

from app.ml import batch_score

pytestmark = pytest.mark.eval


class _FakeModel:
    """Scores whatever the text says to score, so a test can state its intent."""

    def __init__(self, mapping: dict[str, str], version: str = "fake-1"):
        self._mapping = mapping
        self._audit = {"modelVersion": version}

    def predict(self, text, age_band=None, sex=None, *, case_id=None):
        if text == "boom":
            raise RuntimeError("model exploded on this row")
        return {"acuity_code": self._mapping.get(text, "P3_URGENT"), "confidence": 0.9}


def test_a_case_the_current_model_ranks_higher_is_flagged_for_review():
    """The reason the job exists: the queue and the model both move."""
    rows = [{"caseId": "c1", "text": "chest", "acuity": "P3_URGENT"}]
    report = batch_score.score(rows, _FakeModel({"chest": "P1_RESUSCITATION"}))
    assert report["counts"]["more_urgent"] == 1
    assert [r["caseId"] for r in report["needsReview"]] == ["c1"]


def test_a_de_escalation_is_recorded_but_not_surfaced_for_review():
    """Downgrades are reported; they are not what a clinician is paged about."""
    rows = [{"caseId": "c1", "text": "cold", "acuity": "P2_EMERGENT"}]
    report = batch_score.score(rows, _FakeModel({"cold": "P5_SELF_CARE"}))
    assert report["counts"]["less_urgent"] == 1
    assert report["needsReview"] == []


def test_an_unchanged_case_is_counted_not_dropped():
    rows = [{"caseId": "c1", "text": "same", "acuity": "P3_URGENT"}]
    report = batch_score.score(rows, _FakeModel({"same": "P3_URGENT"}))
    assert report["counts"]["unchanged"] == 1 and report["scored"] == 1


def test_a_row_the_model_cannot_score_costs_that_row_and_not_the_batch():
    """A batch that aborts on row 2 of 5000 has served nothing."""
    rows = [
        {"caseId": "c1", "text": "chest", "acuity": "P3_URGENT"},
        {"caseId": "c2", "text": "boom", "acuity": "P3_URGENT"},
        {"caseId": "c3", "text": "", "acuity": "P3_URGENT"},
        {"caseId": "c4", "text": "chest", "acuity": "P3_URGENT"},
    ]
    report = batch_score.score(rows, _FakeModel({"chest": "P2_EMERGENT"}))
    assert report["scored"] == 2 and report["failed"] == 2


def test_the_model_version_that_did_the_scoring_is_recorded():
    """A re-score without the version that produced it cannot be acted on later."""
    report = batch_score.score([], _FakeModel({}, version="careroute-triage-rf-abc123"))
    assert report["modelVersion"] == "careroute-triage-rf-abc123"


def test_only_pending_escalations_are_re_scored():
    """Re-scoring a decided case second-guesses a clinician, not a model."""
    from app.store import Store

    store = Store()
    rows = batch_score.rows_from_store(store)
    assert rows, "the seeded store should have pending escalations"
    assert all(r["text"] for r in rows)

    for escalation in store.list_escalations():
        store.decide_escalation(escalation.id, "approved", "seen", "Dr Test", escalation.acuity.code)
    assert batch_score.rows_from_store(store) == []


def test_a_malformed_line_in_an_export_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "queue.jsonl"
    path.write_text(
        '{"caseId": "c1", "text": "chest"}\n'
        "not json at all\n"
        "\n"
        '{"caseId": "c2", "text": "cold"}\n',
        encoding="utf-8",
    )
    rows = batch_score.rows_from_jsonl(str(path))
    assert [r["caseId"] for r in rows] == ["c1", "c2"]


def test_the_batch_never_mutates_an_escalation():
    """A job that silently re-prioritises a clinical queue overnight is making
    the decision this system is designed not to make."""
    from app.store import Store

    store = Store()
    before = {e.id: (e.status, e.acuity.code) for e in store.list_escalations()}
    batch_score.score(batch_score.rows_from_store(store), _FakeModel({}, version="v"))
    after = {e.id: (e.status, e.acuity.code) for e in store.list_escalations()}
    assert before == after


def test_the_cli_writes_a_report_and_exits_zero_even_when_cases_need_review(tmp_path, monkeypatch):
    """'The queue has drifted' is the job's output, not a failed run — a
    non-zero exit here trains whoever reads the schedule to ignore it."""
    source = tmp_path / "queue.jsonl"
    source.write_text('{"caseId": "c1", "text": "chest", "acuity": "P4_NON_URGENT"}\n', encoding="utf-8")
    out = tmp_path / "report.json"
    monkeypatch.setattr(batch_score, "score",
                        lambda rows, model=None: {"modelVersion": "v", "scored": 1, "failed": 0,
                                                  "counts": {"more_urgent": 1, "unchanged": 0, "less_urgent": 0},
                                                  "needsReview": [{"caseId": "c1"}], "rows": []})
    assert batch_score.main(["--input", str(source), "--output", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["needsReview"] == [{"caseId": "c1"}]
