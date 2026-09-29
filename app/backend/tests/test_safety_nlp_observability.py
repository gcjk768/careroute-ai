"""The shadow Safety-NLP pass is observable: on the final event and in the audit trail."""
import json

from fastapi.testclient import TestClient

from app import llm, main
from app.audit import audit_log

# Aggregate, text-free keys only (app/safety_nlp/telemetry.py).
SUMMARY_KEYS = {
    "totalSignals", "triggeredSignals", "abstentions", "uncertaintyCount", "disagreementCount",
    "statusCounts", "channelCounts", "stageCounts", "stageLatencyMs", "stageAvailabilityCounts",
}


def test_final_event_and_audit_carry_the_text_free_nlp_summary(monkeypatch):
    async def offline(*a, **kw):
        raise llm.LLMUnavailableError("offline")

    monkeypatch.setattr(llm, "complete", offline)
    text = "I have a mild sore throat"
    resp = TestClient(main.app).post("/api/triage/stream", json={"text": text})
    final = [json.loads(line[5:]) for line in resp.text.splitlines()
             if line.startswith("data:") and '"final"' in line][-1]

    summary = final["safetyNlp"]
    assert set(summary) == SUMMARY_KEYS
    assert text not in json.dumps(summary)  # never the patient's words

    shadow = [e for e in audit_log.for_case(final["caseId"]) if e["action"] == "nlp_shadow"]
    assert len(shadow) == 1 and json.loads(shadow[0]["detail"]) == summary
