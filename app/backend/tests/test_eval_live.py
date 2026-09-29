"""[LLMSecOps][MLOps] The live-model evaluation's pass/fail logic, offline.

`python -m app.evals.live` runs the REAL pipeline against a real provider in the
`eval:live-llm` CI job. Every other test scripts the LLM, which is why the
24 Sep 2026 finding (the Reflection critic escalated 100% of cases under
gpt-4o-mini) was invisible to CI. The verdict logic is pure and pinned here, so
the manual job can only fail for a reason these tests describe.
"""
from __future__ import annotations

import json

import pytest

from app.evals import live


def _row(vid, *, must, escalated, critic=None, withheld=False):
    return {"id": vid, "must_escalate": must, "escalated": escalated,
            "critic_action": critic, "critic_withheld": withheld}


def _healthy_rows():
    rows = [_row(f"rf{i}", must=True, escalated=True, critic="escalate") for i in range(4)]
    rows += [_row("ok1", must=False, escalated=False, critic="accept"),
             _row("ok2", must=False, escalated=False, critic="escalate", withheld=True),
             _row("ok3", must=False, escalated=True, critic="escalate"),
             _row("ok4", must=False, escalated=False)]
    return rows


_JUDGE = {"available": True, "n": 4, "accuracy": 0.75}


def test_a_healthy_run_passes():
    report = live.summarise(_healthy_rows(), band=(0.25, 0.75), judge=_JUDGE, llm_calls_ok=12)
    assert report["passed"] is True
    assert report["redFlagRecall"] == 1.0
    assert report["escalationRate"] == pytest.approx(5 / 8, abs=1e-3)
    # 6 critic escalations requested, 1 withheld by the grounded gate.
    assert report["critic"]["escalateRequested"] == 6
    assert report["critic"]["groundedEscalationRate"] == pytest.approx(5 / 6, abs=1e-3)


def test_a_missed_red_flag_fails():
    rows = _healthy_rows()
    rows[0]["escalated"] = False
    report = live.summarise(rows, band=(0.25, 0.75), judge=_JUDGE, llm_calls_ok=12)
    assert report["passed"] is False
    assert report["checks"]["redFlagRecall"] is False


def test_the_escalate_everything_critic_fails_the_band():
    """The live finding itself: every case escalated -> rate 1.0 -> out of band."""
    rows = [dict(r, escalated=True) for r in _healthy_rows()]
    report = live.summarise(rows, band=(0.25, 0.75), judge=_JUDGE, llm_calls_ok=12)
    assert report["passed"] is False
    assert report["checks"]["escalationRateInBand"] is False


def test_a_run_that_never_reached_the_model_is_not_a_pass():
    report = live.summarise(_healthy_rows(), band=(0.25, 0.75), judge=_JUDGE, llm_calls_ok=0)
    assert report["passed"] is False
    assert report["checks"]["llmWasUsed"] is False


def test_an_unrun_judge_is_not_a_pass():
    report = live.summarise(_healthy_rows(), band=(0.25, 0.75),
                            judge={"available": False, "n": 0}, llm_calls_ok=3)
    assert report["checks"]["judgeRan"] is False
    assert report["passed"] is False


def test_a_budget_stop_is_incomplete():
    report = live.summarise(_healthy_rows(), band=(0.25, 0.75), judge=_JUDGE, llm_calls_ok=3,
                            stopped_early=True)
    assert report["checks"]["complete"] is False
    assert report["passed"] is False


def test_no_critic_verdicts_reports_none_not_zero():
    rows = [_row("a", must=True, escalated=True), _row("b", must=False, escalated=False)]
    report = live.summarise(rows, band=(0.25, 0.75), judge=_JUDGE, llm_calls_ok=1)
    assert report["critic"]["groundedEscalationRate"] is None


@pytest.mark.parametrize(("raw", "band"), [("0.25,0.75", (0.25, 0.75)), (" 0.1 , 0.9 ", (0.1, 0.9))])
def test_parse_band(raw, band):
    assert live.parse_band(raw) == band


@pytest.mark.parametrize("raw", ["0.8,0.2", "x,y", "0.5", "-0.1,0.5", "0.2,1.5"])
def test_parse_band_rejects_nonsense(raw):
    with pytest.raises(ValueError):
        live.parse_band(raw)


def test_without_a_provider_the_runner_skips_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(live.config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(live.config, "LLM_GATEWAY_URL", "")
    out = tmp_path / "live.json"
    assert live.main(["--out", str(out)]) == 2
    assert json.loads(out.read_text())["status"] == "skipped"
