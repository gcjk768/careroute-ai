"""[LLMSecOps][MLOps] LIVE-MODEL evaluation — the real pipeline, a real model.

Every CI test scripts the LLM (conftest patches `llm.complete`), which is right
for a deterministic gate and blind to how the pipeline behaves under a real
model. The 24 Sep 2026 AWS load test proved the cost of that blindness: under
gpt-4o-mini the Reflection critic escalated 100% of cases, and no test could
have seen it. This runner closes the gap on a small budget:

  1. drives the REAL pipeline (`main._triage_event_stream`, live provider) over
     the 20 gold vignettes (tests/fixtures/triage_vignettes.json);
  2. asserts red-flag recall == 1.0 — the safety floor must hold under a model
     too, not only on the deterministic path;
  3. asserts the escalation rate sits in a band (default 0.25-0.75; the gold
     set is 10/20 must-escalate). Above the band is the escalate-everything
     critic; below it means red flags are being missed or HITL never fires;
  4. REPORTS the critic's grounded-escalation rate: of the critic's `escalate`
     verdicts, the fraction the grounded gate applied (agents/reflection.py);
  5. runs the E15 LLM-as-judge (evals/grounding.py) on a small sample — the
     judge the Evaluation Plan lists as "unrun in CI".

It fails (non-zero) if the model was never actually reached, because a live
evaluation that silently fell back to the deterministic path passes vacuously.

Run:  python -m app.evals.live --out live-eval.json [--band 0.25,0.75]
          [--judge-sample 6] [--budget-usd 0.50] [--limit N]
Exit: 0 passed · 1 failed a check · 2 skipped (no provider configured).
Env:  CAREROUTE_LIVE_ESCALATION_BAND overrides the default band.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path

from .. import config

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "triage_vignettes.json"
DEFAULT_BAND = "0.25,0.75"


def parse_band(raw: str) -> tuple[float, float]:
    lo, hi = (float(x) for x in raw.split(","))  # ValueError on a malformed band
    if not 0.0 <= lo <= hi <= 1.0:
        raise ValueError(f"escalation band must satisfy 0 <= lo <= hi <= 1, got {raw!r}")
    return lo, hi


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def summarise(rows: list[dict], *, band: tuple[float, float], judge: dict | None,
              llm_calls_ok: int, stopped_early: bool = False) -> dict:
    """Pure verdict over per-vignette rows. Each row: id, must_escalate,
    escalated, critic_action (None / accept / rerun / escalate), critic_withheld."""
    must = [r for r in rows if r["must_escalate"]]
    recall = _rate(sum(r["escalated"] for r in must), len(must))
    escalation_rate = _rate(sum(r["escalated"] for r in rows), len(rows))
    critic_seen = [r for r in rows if r.get("critic_action")]
    requested = [r for r in critic_seen if r["critic_action"] == "escalate"]
    applied = [r for r in requested if not r.get("critic_withheld")]
    checks = {
        "redFlagRecall": recall is not None and math.isclose(recall, 1.0),
        "escalationRateInBand": escalation_rate is not None and band[0] <= escalation_rate <= band[1],
        "llmWasUsed": llm_calls_ok > 0,
        "judgeRan": bool(judge and judge.get("available")),
        "complete": not stopped_early,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "n": len(rows),
        "redFlagRecall": recall,
        "missedRedFlags": [r["id"] for r in must if not r["escalated"]],
        "escalationRate": escalation_rate,
        "escalationBand": list(band),
        "critic": {
            "verdicts": len(critic_seen),
            "escalateRequested": len(requested),
            "escalateRequestRate": _rate(len(requested), len(critic_seen)),
            # The grounded gate's effect: requested escalations it let through.
            "groundedEscalationRate": _rate(len(applied), len(requested)),
        },
        "llmCallsOk": llm_calls_ok,
        "judge": judge,
    }


def _metric_sum(name: str, **match: str) -> float:
    try:
        from prometheus_client import REGISTRY
    except ImportError:
        return 0.0
    return sum(s.value for fam in REGISTRY.collect() for s in fam.samples
               if s.name == name and all(s.labels.get(k) == v for k, v in match.items()))


def _metric_by(name: str, label: str) -> dict[str, float]:
    from prometheus_client import REGISTRY

    out: dict[str, float] = {}
    for fam in REGISTRY.collect():
        for s in fam.samples:
            if s.name == name:
                out[s.labels.get(label, "")] = out.get(s.labels.get(label, ""), 0.0) + s.value
    return out


async def _final_of(text: str) -> dict | None:
    from ..main import _triage_event_stream
    from ..models import TriageRequest

    final = None
    async for chunk in _triage_event_stream(TriageRequest(text=text, language="en", isVoice=False)):
        line = chunk.strip()
        if line.startswith("data:"):
            event = json.loads(line[len("data:"):].strip())
            if event.get("event") == "final":
                final = event
    return final


async def run(vignettes: list[dict], *, budget_usd: float, judge_sample: int) -> tuple[list[dict], bool, dict]:
    from .. import main as main_mod
    from . import grounding

    async def _no_delay(*_a, **_k):
        return None

    main_mod._step_delay = _no_delay   # UI pacing only; a CLI run has no UI
    spent_before = _metric_sum("careroute_llm_cost_usd_total")
    rows, stopped = [], False
    for v in vignettes:
        if _metric_sum("careroute_llm_cost_usd_total") - spent_before > budget_usd:
            stopped = True
            break
        final = await _final_of(v["text"]) or {}
        critic = (final.get("reflection") or {}).get("critic") or {}
        rows.append({
            "id": v["id"], "must_escalate": bool(v["must_escalate"]),
            "escalated": bool(final.get("escalated")),
            "acuity": (final.get("acuity") or {}).get("code"),
            "gold": v["gold_acuity"],
            "critic_action": critic.get("action"),
            "critic_withheld": bool(critic.get("escalationWithheld")),
            "escalationReason": final.get("escalationReason"),
        })
    if judge_sample <= 0:
        return rows, stopped, None
    # Evenly strided so every family (verbatim/paraphrase/invented/semantic) is sampled.
    cases = grounding.load_cases()
    sample = cases[::max(1, len(cases) // judge_sample)][:judge_sample]
    judge = await grounding.evaluate_judge_async(sample)
    return rows, stopped, {k: v for k, v in judge.items() if k != "rows"}


def _write(path: str, report: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live-model evaluation of the triage pipeline.")
    parser.add_argument("--out", default="live-eval.json")
    parser.add_argument("--band", default=os.environ.get("CAREROUTE_LIVE_ESCALATION_BAND", DEFAULT_BAND))
    parser.add_argument("--judge-sample", type=int, default=6)
    parser.add_argument("--budget-usd", type=float, default=0.50)
    parser.add_argument("--limit", type=int, default=0, help="first N vignettes only (0 = all 20)")
    args = parser.parse_args(argv)
    band = parse_band(args.band)

    if not (config.OPENAI_API_KEY or config.LLM_GATEWAY_URL) or config.KILL_SWITCH:
        _write(args.out, {"status": "skipped",
                          "reason": "no LLM provider configured (OPENAI_API_KEY unset) or kill switch on"})
        print("live eval SKIPPED: no LLM provider configured — not run is not passed")
        return 2

    from .. import llm, rag_embed

    rag_embed.warm()   # onnxruntime must load on the main thread (see rag_embed.warm)
    vignettes = json.loads(_FIXTURE.read_text(encoding="utf-8"))["vignettes"]
    if args.limit > 0:
        vignettes = vignettes[:args.limit]
    ok_before = _metric_sum("careroute_llm_latency_seconds_count", outcome="ok")
    cost_before = _metric_sum("careroute_llm_cost_usd_total")
    rows, stopped, judge = asyncio.run(run(vignettes, budget_usd=args.budget_usd, judge_sample=args.judge_sample))
    report = summarise(rows, band=band, judge=judge, stopped_early=stopped,
                       llm_calls_ok=int(_metric_sum("careroute_llm_latency_seconds_count", outcome="ok") - ok_before))
    report.update({
        "status": "passed" if report["passed"] else "failed",
        "model": config.OPENAI_MODEL,
        "costUsd": round(_metric_sum("careroute_llm_cost_usd_total") - cost_before, 6),
        "budgetUsd": args.budget_usd,
        "routes": _metric_by("careroute_llm_route_total", "reason"),
        "guard": _metric_by("careroute_llm_guard_total", "action"),
        "cache": llm.cache_stats(),
        "rows": rows,
    })
    _write(args.out, report)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
