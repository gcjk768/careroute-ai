"""[Agentic] E13 — TOOL-CALL ACCURACY, scored.

The agentic CI deck's quality-gate table asks for tool-call accuracy ≥ 0.95.
Until now the answer was a handful of assertions in
`tests/agents/test_routing_react.py`: "is this bad call refused?" — the same
shape of check that E9 replaced for the guardrail, and with the same blind
spot. A harness that refuses everything passes every refusal test ever written
and breaks the agent, and nothing in a pass/fail suite can tell you that.

So this scores a RATE over a labelled corpus, and scores BOTH directions:

  accuracy              fraction of cases whose verdict matches the label
  refusal recall        over the rows that must be refused (a leak is a call
                        executed against an unregistered tool or an unverified
                        clinic — the agency gate, so the bar is 1.0)
  false-refusal rate    over the rows that must EXECUTE. The number a
                        refuse-everything harness cannot hide behind.
  per family            unregistered / malformed / unverified_target /
                        bad_arguments / no_call / valid, because a blended
                        number hides which defence is the weak one.

WHAT THIS DOES AND DOES NOT MEASURE. It scores the HARNESS around the model's
tool choice — validation, argument binding, refusal — over scripted replies.
Whether a live model picks the *clinically sensible* tool needs a live model
and is not measured in CI; the same split as E11 for the critic. The harness is
the part that has to be right when the model is wrong, which is the part a gate
can actually hold.

Run it:  python -m app.evals.tool_calls
"""
from __future__ import annotations

import json
import os
import sys

_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tests", "fixtures", "tool_calls.json",
)

#: Families whose rows must never be executed.
REFUSAL_FAMILIES = ("unregistered", "malformed", "unverified_target", "bad_arguments")


def load_corpus(path: str = _FIXTURE) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def verdict(reply: object, candidates: list[dict]) -> tuple[str, str | None, str | None]:
    """(verdict, tool, reason) for one scripted model reply.

    verdict is `execute` (a validated call), `refuse` (the harness marked it
    invalid, so the loop turns it into an error observation) or `none` (no tool
    call at all — the ordinary decision path).
    """
    from ..agents.routing import CareRoutingAgent

    request = CareRoutingAgent._tool_call_request(reply, candidates)
    if request is None:
        return "none", None, None
    if request.get("invalid"):
        return "refuse", request.get("name"), request["invalid"]
    return "execute", request.get("name"), None


def evaluate(corpus: dict | None = None) -> dict:
    corpus = corpus if corpus is not None else load_corpus()
    candidates = corpus["candidates"]
    rows = []
    for case in corpus["cases"]:
        got, tool, reason = verdict(case["reply"], candidates)
        correct = got == case["expected"]
        # A call that executes the RIGHT action but against the wrong tool is
        # not a pass: the label names the tool for the rows where it matters.
        if correct and case.get("expected_tool") and tool != case["expected_tool"]:
            correct = False
        rows.append({"id": case["id"], "family": case["family"], "expected": case["expected"],
                     "got": got, "tool": tool, "reason": reason, "correct": correct})

    must_refuse = [r for r in rows if r["expected"] == "refuse"]
    must_execute = [r for r in rows if r["expected"] == "execute"]
    families = sorted({r["family"] for r in rows})

    def _rate(selected: list[dict], predicate) -> float:
        return round(sum(1 for r in selected if predicate(r)) / len(selected), 4) if selected else 0.0

    return {
        "n": len(rows),
        "accuracy": _rate(rows, lambda r: r["correct"]),
        "refusal_recall": _rate(must_refuse, lambda r: r["got"] == "refuse"),
        "false_refusal_rate": _rate(must_execute, lambda r: r["got"] != "execute"),
        "by_family": {
            f: {"n": sum(1 for r in rows if r["family"] == f),
                "accuracy": _rate([r for r in rows if r["family"] == f], lambda r: r["correct"])}
            for f in families
        },
        "failures": [r for r in rows if not r["correct"]],
    }


def main() -> int:
    report = evaluate()
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
