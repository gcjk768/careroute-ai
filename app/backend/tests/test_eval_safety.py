"""[Responsible-AI] E7 - Safety red-flag context benchmark.

Phase 3 is the benchmark and signal-contract phase, not the NLP activation
phase. These tests validate the gold fixture and report deterministic-floor
coverage gaps that later NLP/LLM stages must close without weakening the floor.

Run with: pytest -m "eval and safety"
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import redflags
from app.agents.safety import (
    VALID_SIGNAL_ASSERTIONS,
    VALID_SIGNAL_SUBJECTS,
    VALID_SIGNAL_TEMPORALITIES,
)
from app.evals import by_id

pytestmark = [pytest.mark.eval, pytest.mark.safety]

SPEC = by_id("E7-safety-context")
FIXTURE = Path(__file__).parent / "fixtures" / "safety_context_cases.json"
REQUIRED_LANGUAGES = {"en", "zh", "ms", "ta"}
PRESERVATION_KEYS = {"symptom", "polarity", "subject", "temporality"}


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    with FIXTURE.open(encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["schemaVersion"] == 1
    assert data["clinicalReview"]["status"] == "unavailable_not_clinically_validated"
    return data["cases"]


def test_spec_and_implementation_agree():
    assert SPEC.status == "implemented"
    assert "tests/test_eval_safety.py" in SPEC.implemented_by
    assert SPEC.dataset_exists


def test_rows_match_signal_schema_and_closed_vocab(cases):
    categories = {rule.name for rule in redflags.RED_FLAG_RULES}
    seen_ids: set[str] = set()

    for row in cases:
        assert row["id"] not in seen_ids
        seen_ids.add(row["id"])
        assert row["category"] in categories
        assert row["assertion"] in VALID_SIGNAL_ASSERTIONS
        assert row["subject"] in VALID_SIGNAL_SUBJECTS
        assert row["temporality"] in VALID_SIGNAL_TEMPORALITIES
        assert row["partition"] in {"development", "holdout"}
        assert isinstance(row["goldTrigger"], bool)
        assert isinstance(row["regexTriggers"], bool)
        assert row["goldAction"] in {
            "add_or_confirm_trigger",
            "do_not_add_semantic_trigger",
            "route_to_human_review",
        }
        assert str(row["rationale"]).strip()


def test_mentions_have_stable_ids_and_original_offsets(cases):
    for row in cases:
        mention_ids: set[str] = set()
        for mention in row["mentions"]:
            assert mention["mentionId"] not in mention_ids
            mention_ids.add(mention["mentionId"])
            assert row["text"][mention["start"]:mention["end"]] == mention["text"]
            if "category" in mention:
                assert mention["category"] in {rule.name for rule in redflags.RED_FLAG_RULES}


def test_fixture_covers_phase_3_context_matrix(cases):
    categories = {rule.name for rule in redflags.RED_FLAG_RULES}
    active_categories = {row["category"] for row in cases if row["goldTrigger"]}
    negated_categories = {row["category"] for row in cases if row["assertion"] == "negated"}

    assert active_categories == categories
    assert negated_categories == categories
    assert {row["language"] for row in cases} >= REQUIRED_LANGUAGES
    assert {row["partition"] for row in cases} == {"development", "holdout"}
    assert {row["temporality"] for row in cases} >= {"current", "recent", "remote", "unknown"}
    assert {row["subject"] for row in cases} >= {"patient", "care_subject", "other_person", "unknown"}
    assert any(row["assertion"] == "conditional" for row in cases)
    assert any("hypothetical" in row["id"] for row in cases)
    assert any(len(row["mentions"]) > 1 for row in cases)
    assert any("code-switched" in row["id"] for row in cases)
    assert any("voice" in row["id"] for row in cases)


def test_non_english_rows_have_translation_labels(cases):
    for row in cases:
        if row["language"] == "en":
            continue
        assert row["referenceTranslation"]
        assert set(row["translationPreserved"]) == PRESERVATION_KEYS
        assert all(isinstance(row["translationPreserved"][key], bool) for key in PRESERVATION_KEYS)


def test_declared_regex_trigger_matches_current_policy(cases):
    mismatches = []
    for row in cases:
        actual = redflags.evaluate(row["text"]).triggered
        if actual != row["regexTriggers"]:
            mismatches.append(f"{row['id']}: fixture={row['regexTriggers']} actual={actual}")

    assert not mismatches, "regexTriggers drifted from redflags.evaluate: " + "; ".join(mismatches)


def test_report_context_conflicts_and_semantic_recall_gap(cases):
    hard_negatives = [row for row in cases if not row["goldTrigger"]]
    context_conflicts = [row["id"] for row in hard_negatives if row["regexTriggers"]]
    conflict_rate = len(context_conflicts) / len(hard_negatives)

    positives = [row for row in cases if row["goldTrigger"]]
    missed_by_regex = [row["id"] for row in positives if not row["regexTriggers"]]
    semantic_recall_gap = len(missed_by_regex) / len(positives)

    assert conflict_rate > 0.0, "benchmark must contain regex/context conflicts for HITL telemetry"
    assert semantic_recall_gap > 0.0, "benchmark must contain positives the regex floor cannot see"


def test_metric_summary_has_category_context_escalation_and_language_views(cases):
    categories = {rule.name for rule in redflags.RED_FLAG_RULES}
    summary = {
        "by_category": {
            category: {
                "rows": sum(1 for row in cases if row["category"] == category),
                "gold_positive": sum(1 for row in cases if row["category"] == category and row["goldTrigger"]),
            }
            for category in categories
        },
        "by_context": {
            label: sum(1 for row in cases if row["assertion"] == label)
            for label in VALID_SIGNAL_ASSERTIONS
        },
        "by_language": {
            language: {
                "rows": sum(1 for row in cases if row["language"] == language),
                "gold_positive": sum(1 for row in cases if row["language"] == language and row["goldTrigger"]),
            }
            for language in REQUIRED_LANGUAGES
        },
        "escalation": {
            "gold_positive": sum(1 for row in cases if row["goldTrigger"]),
            "gold_negative": sum(1 for row in cases if not row["goldTrigger"]),
            "regex_positive": sum(1 for row in cases if row["regexTriggers"]),
            "regex_negative": sum(1 for row in cases if not row["regexTriggers"]),
        },
    }

    assert all(value["rows"] > 0 for value in summary["by_category"].values())
    assert all(value["rows"] > 0 for value in summary["by_language"].values())
    assert summary["escalation"]["gold_positive"] > 0
    assert summary["escalation"]["gold_negative"] > 0
