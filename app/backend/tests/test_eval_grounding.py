"""[LLMSecOps] E15 — groundedness: what term overlap can and cannot see.

Most of this file pins NEGATIVE results on purpose. The deterministic scorer's
misses are the entire argument for adding an LLM judge, and an argument that
lives only in a docstring stops being true without anyone noticing.
"""
import asyncio
import json

import pytest

from app import llm
from app.evals import grounding as G

pytestmark = pytest.mark.eval


@pytest.fixture(scope="module")
def overlap():
    return G.evaluate_overlap()


def _row(report, case_id):
    return next(r for r in report["rows"] if r["id"] == case_id)


# --- what overlap is good at -------------------------------------------------------
def test_every_invented_fact_is_caught(overlap):
    """Its home ground: an answer that introduces vocabulary the context never had."""
    assert overlap["by_family"]["invented"]["accuracy"] == 1.0


def test_an_invented_medication_is_named_not_just_flagged(overlap):
    unsupported = _row(overlap, "fever-invented-medication")["unsupported"]
    assert "amoxicillin" in unsupported


# --- what overlap cannot see ------------------------------------------------------
def test_a_flipped_negation_scores_a_perfect_one(overlap):
    """"No cough, no rash" -> "cough and rash". Every word is in the context, so
    term overlap calls it grounded. This is why the judge exists."""
    row = _row(overlap, "fever-negation-flipped")
    assert row["score"] == 1.0 and row["verdict"] == "faithful" and row["label"] == "unfaithful"


def test_a_swapped_subject_scores_a_perfect_one(overlap):
    """Who the finding belongs to is not a vocabulary question."""
    row = _row(overlap, "bleeding-subject-swapped")
    assert row["score"] == 1.0 and row["verdict"] == "faithful"


def test_faithful_paraphrases_are_rejected(overlap):
    """The other direction, and the one a gate would hurt: clinical synonyms —
    dyspnoea, pyrexia, diaphoresis — are new words, so every faithful paraphrase
    in the corpus is flagged."""
    assert overlap["by_family"]["paraphrase"]["accuracy"] == 0.0
    assert overlap["false_positive_rate"] >= 0.5


def test_no_tolerance_makes_this_a_usable_gate():
    """The finding, reproducible rather than asserted: the whole curve stays far
    below the 0.85 faithfulness bar the courseware asks for."""
    sweep = G.sweep_tolerance()
    best = max(s["accuracy"] for s in sweep)
    assert best < 0.85, f"a tolerance now reaches {best}; re-read E15 before trusting it"


def test_the_overall_accuracy_is_reported_and_not_gated(overlap):
    """Recorded so a regression is visible; deliberately not a threshold."""
    assert 0.4 <= overlap["accuracy"] <= 0.7


# --- the judge --------------------------------------------------------------------
def test_without_a_provider_the_judge_reports_skipped_not_passed():
    """The suite disables the LLM, so this is the CI path. An unreachable judge
    and a judge that found nothing are the same empty result, and only one of
    them is evidence."""
    report = asyncio.run(G.evaluate_judge_async())
    assert report["available"] is False
    assert report["skipped"] == len(G.load_cases())


def test_a_judge_verdict_is_parsed_and_bounded():
    async def _fake(system, prompt, json_mode=False, json_schema=None, *, task=None):
        assert task == "eval.grounding"
        return json.dumps({
            "faithful": False,
            "unsupported_claims": ["invented a penicillin allergy"] * 20,
            "reason": "x" * 500,
        })

    verdict = asyncio.run(G.judge("answer", "context", _fake))
    assert verdict["faithful"] is False
    assert len(verdict["unsupported_claims"]) == 10      # bounded
    assert len(verdict["reason"]) == 300                 # bounded


def test_a_malformed_verdict_is_not_a_pass():
    async def _junk(*_a, **_kw):
        return '{"faithful": "probably"}'

    assert asyncio.run(G.judge("answer", "context", _junk)) is None


def test_the_judge_scores_the_corpus_and_its_agreement_with_overlap():
    """With a scripted judge that gets every row right, the reported agreement
    with the cheap scorer is exactly the cheap scorer's accuracy — which is the
    number that says what the judge is being paid for."""
    cases = G.load_cases()
    labels = {c["id"]: c["label"] for c in cases}

    async def _oracle(system, prompt, json_mode=False, json_schema=None, *, task=None):
        case = next(c for c in cases if c["answer"] in prompt)
        return json.dumps({"faithful": labels[case["id"]] == "faithful", "reason": "scripted"})

    report = asyncio.run(G.evaluate_judge_async(cases, _oracle))
    assert report["available"] is True and report["accuracy"] == 1.0
    assert report["agreement_with_overlap"] == G.evaluate_overlap(cases)["accuracy"]


def test_the_judge_task_is_routed_deep_and_cacheable():
    """A judge that disagrees with itself cannot grade anything."""
    route = llm.route_table()["eval.grounding"]
    assert route["tier"] == "deep" and route["cacheable"] is True
