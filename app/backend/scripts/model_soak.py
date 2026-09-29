"""Compare model configurations on the 100 soak scenarios, through the real API.

    python scripts/model_soak.py --url http://127.0.0.1:8000 --label gpt4.1 --out results-a.json
    python scripts/model_soak.py --url http://127.0.0.1:8001 --label gpt5   --out results-b.json

Same cases and answer rotation as the Playwright soak (frontend/tests/e2e/fixtures/soak_scenarios.json,
interview_soak.spec.js) but API-level: the browser adds nothing to a MODEL comparison, and two backends
with different OPENAI_MODEL_* settings can then run side by side. Each turn re-posts the complaint plus
the transcript, exactly as the UI does. Per scenario it records the decision, the questions asked, the
wall time, and every LLM call the final event reports (task, tier, model, latency) — and checks the
same invariants: red flags decided on turn 1 at P1/P2, vague complaints interviewed, ≤ 4 questions,
no repeated question, only adversarial input blocked.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "frontend" / "tests" / "e2e" / "fixtures" / "soak_scenarios.json"
ANSWERS = ["No", "Not sure", "Yes", "It started yesterday, it is mild, nothing else."]
BUDGET = 4


def turn(url: str, text: str, clarifications: list[dict]) -> dict:
    req = urllib.request.Request(f"{url}/api/triage/stream", method="POST",  # noqa: S310 - scheme checked in main()
                                 data=json.dumps({"text": text, "clarifications": clarifications}).encode(),
                                 headers={"Content-Type": "application/json"})
    final, error = None, None
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected, bandit.B310-1
    with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310  # nosec B310
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            if ev.get("event") == "final":
                final = ev
            elif ev.get("event") == "error":
                error = ev.get("message", "")
    return {"final": final, "error": error}


def run_case(url: str, i: int, row: dict) -> dict:
    answer = ANSWERS[(i - 1) % len(ANSWERS)]
    clar, asked, calls, problems = [], [], [], []
    started = time.monotonic()
    for _ in range(BUDGET + 2):
        r = turn(url, row["text"], clar)
        if r["error"] is not None and r["final"] is None:
            if row["kind"] != "adversarial":
                problems.append(f"blocked: {r['error'][:60]}")
            return {"i": i, **row, "outcome": "blocked", "questions": asked, "calls": calls,
                    "ms": round((time.monotonic() - started) * 1000), "problems": problems}
        f = r["final"]
        calls += f.get("llm") or []
        q = f.get("clarification")
        if not q or (f.get("interview") or {}).get("done", True):
            break
        if q["question"] in asked:
            problems.append("repeated question")
        asked.append(q["question"])
        if len(asked) > BUDGET:
            problems.append("over budget")
            break
        clar.append({"question": q["question"], "answer": answer, "feature": q.get("feature"),
                     "source": q.get("source"), "statement": q.get("statement")})
    code = (f.get("acuity") or {}).get("code", "")
    if row["kind"] == "redflag" and (asked or not code.startswith(("P1", "P2"))):
        problems.append(f"red flag: asked={len(asked)} acuity={code}")
    if row["kind"] == "vague" and not asked:
        problems.append("vague not interviewed")
    return {"i": i, **row, "outcome": "decided", "acuity": code, "confidence": f.get("confidence"),
            "escalated": f.get("escalated"), "questions": asked, "calls": calls,
            "ms": round((time.monotonic() - started) * 1000), "problems": problems}


def summarise(label: str, rows: list[dict]) -> dict:
    decided = [r for r in rows if r["outcome"] == "decided"]
    calls = [c for r in rows for c in r["calls"] if not c.get("cached")]
    by_model: dict[str, list[int]] = {}
    for c in calls:
        by_model.setdefault(f"{c.get('tier')}:{c.get('model')}", []).append(int(c.get("ms") or 0))
    routine = [r for r in decided if r["kind"] == "routine"]
    return {
        "label": label,
        "passed": sum(not r["problems"] for r in rows), "of": len(rows),
        "problems": [f"#{r['i']} {p}" for r in rows for p in r["problems"]],
        "redflagP1P2": sum(r["acuity"].startswith(("P1", "P2")) for r in decided if r["kind"] == "redflag"),
        "vagueMeanQ": round(st.mean(len(r["questions"]) for r in decided if r["kind"] == "vague"), 2),
        "routineEscalated": sum(bool(r["escalated"]) for r in routine),
        "routineP2plus": sum(r["acuity"].startswith(("P1", "P2")) for r in routine),
        "medianCaseS": round(st.median(r["ms"] for r in rows) / 1000, 1),
        "llmCalls": len(calls),
        "callMedianMsByTierModel": {k: int(st.median(v)) for k, v in sorted(by_model.items())},
        "callCountByTierModel": {k: len(v) for k, v in sorted(by_model.items())},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if not a.url.startswith(("http://", "https://")):
        ap.error("--url must be http:// or https://")  # urlopen would also read file:// (ruff S310)
    rows = []
    for i, row in enumerate(json.loads(SCENARIOS.read_text(encoding="utf-8")), 1):
        try:
            rows.append(run_case(a.url, i, row))
        except Exception as exc:   # noqa: BLE001 - one broken case must not end the comparison
            rows.append({"i": i, **row, "outcome": "error", "questions": [], "calls": [], "ms": 0,
                         "problems": [f"error: {type(exc).__name__}: {str(exc)[:80]}"]})
        r = rows[-1]
        print(f"[{a.label}] {i:3d} {r['kind']:11} {r.get('acuity', r['outcome']):17} Q={len(r['questions'])} "
              f"{r['ms'] / 1000:5.1f}s {'; '.join(r['problems'])}", flush=True)
    summary = summarise(a.label, rows)
    a.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
