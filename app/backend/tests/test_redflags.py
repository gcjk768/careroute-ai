"""Agent-behaviour unit tests for the deterministic Safety-Override rules
(app/redflags.py). These rules are the "last line of defense" and must
never be relaxed by a downstream (possibly wrong) classifier output --
`apply_override` may only raise acuity severity, never lower it.
"""
from __future__ import annotations

from app.redflags import apply_override, evaluate


def test_chest_pain_triggers_p1():
    result = evaluate("I have severe chest pain radiating down my left arm and I'm sweating.")
    assert result.triggered
    assert result.forced_acuity == "P1_RESUSCITATION"


def test_stroke_signs_trigger_p1():
    result = evaluate("Sudden weakness on one side of my body and slurred speech since this morning.")
    assert result.triggered
    assert result.forced_acuity == "P1_RESUSCITATION"


def test_suicidal_ideation_escalates():
    result = evaluate("I keep thinking I want to end my life.")
    assert result.triggered
    assert result.forced_acuity == "P2_EMERGENT"


def test_no_red_flag_returns_not_triggered():
    result = evaluate("I have a mild headache and slight tiredness.")
    assert result.triggered is False
    assert result.rule is None
    assert result.forced_acuity is None


def test_apply_override_can_raise_severity():
    result = evaluate("chest pain")  # P1_RESUSCITATION
    forced = apply_override("P4_NON_URGENT", result)
    assert forced == "P1_RESUSCITATION"


def test_apply_override_never_lowers_severity():
    # Classifier already assessed P1 (most severe); a P2-level red flag
    # (suicidal ideation) must NOT downgrade it back to P2.
    result = evaluate("I want to end my life")  # P2_EMERGENT rule
    forced = apply_override("P1_RESUSCITATION", result)
    assert forced == "P1_RESUSCITATION"


def test_apply_override_no_trigger_is_noop():
    result = evaluate("I have a mild cold.")
    forced = apply_override("P3_URGENT", result)
    assert forced == "P3_URGENT"


import pytest

from app import redflags as _rf


@pytest.mark.parametrize("text,acuity", [
    ("my father collapsed and is not responding", "P1_RESUSCITATION"),
    ("he is unresponsive", "P1_RESUSCITATION"),
    ("passed out and won't wake up", "P1_RESUSCITATION"),
    ("he is not breathing", "P1_RESUSCITATION"),
    ("stopped breathing", "P1_RESUSCITATION"),
    ("overdosed on paracetamol", "P1_RESUSCITATION"),
    ("took 40 panadol tablets", "P1_RESUSCITATION"),
    ("overdose", "P1_RESUSCITATION"),
    ("swallowed bleach", "P1_RESUSCITATION"),
    ("baby is floppy and blue", "P1_RESUSCITATION"),
    ("lips turned blue", "P1_RESUSCITATION"),
    ("lips are going blue", "P1_RESUSCITATION"),
    ("choking", "P1_RESUSCITATION"),
    ("choking on food", "P1_RESUSCITATION"),
    ("pregnant and bleeding", "P2_EMERGENT"),
    ("pregnant with heavy bleeding", "P2_EMERGENT"),
    ("short of breath", "P1_RESUSCITATION"),
    ("shortness of breath", "P1_RESUSCITATION"),
    ("trouble breathing", "P1_RESUSCITATION"),
    ("difficulty in breathing", "P1_RESUSCITATION"),
    ("breathing difficulty", "P1_RESUSCITATION"),
    ("I can not breathe", "P1_RESUSCITATION"),
    ("cant breath", "P1_RESUSCITATION"),
    ("chest hurts", "P1_RESUSCITATION"),
    ("tight chest", "P1_RESUSCITATION"),
    ("pain in chest", "P1_RESUSCITATION"),
    ("left arm pain and sweating", "P1_RESUSCITATION"),
    ("sudden numbness on the left side", "P1_RESUSCITATION"),
    ("weak on one side", "P1_RESUSCITATION"),
    ("can't lift my arm", "P1_RESUSCITATION"),
    ("cannot move my arm", "P1_RESUSCITATION"),
    ("speech is slurred", "P1_RESUSCITATION"),
    ("slurring words", "P1_RESUSCITATION"),
    ("epi pen", "P1_RESUSCITATION"),
    ("epi-pen", "P1_RESUSCITATION"),
    ("I don't want to live anymore", "P2_EMERGENT"),
    ("thinking about ending it all", "P2_EMERGENT"),
    ("hurting myself", "P2_EMERGENT"),
    ("profuse bleeding", "P2_EMERGENT"),
    ("bleeding that won't stop", "P2_EMERGENT"),
    ("vomited blood", "P2_EMERGENT"),
    ("throwing up blood", "P2_EMERGENT"),
    ("child having a fit", "P2_EMERGENT"),
    ("convulsing", "P2_EMERGENT"),
    ("worst headache ever", "P1_RESUSCITATION"),
    ("thunderclap headache", "P1_RESUSCITATION"),
])
def test_emergency_phrasings_reach_the_floor(text, acuity):
    """Recall was 1.0 only on the curated eval set: there was no rule at all for
    unresponsiveness, overdose, choking, pregnancy bleeding or infant cyanosis,
    and the existing rules needed the exact textbook wording."""
    result = _rf.evaluate(text)
    assert result.triggered, text
    assert result.forced_acuity == acuity, (text, result.rule)


@pytest.mark.parametrize("text", ["outfitting my house", "benefitting from rest", "I am fitting my new shoes"])
def test_fitting_inside_another_word_is_not_a_seizure(text):
    assert not _rf.evaluate(text).triggered, text


def test_invisible_characters_do_not_hide_a_red_flag():
    """The guardrail folds zero-width and soft-hyphen characters before its own
    scan, but the red-flag regexes ran on the raw text."""
    assert _rf.evaluate("chest\u200bpain and sweating").rule == "cardiac_chest_pain"
    assert _rf.evaluate("chest\u00adpain").rule == "cardiac_chest_pain"
    assert _rf.evaluate("can\u200bt breathe").rule == "breathlessness"


def test_a_denied_red_flag_does_not_force_the_floor():
    """"I don't have chest pain" forced P1. A denial in the same clause, within
    three tokens, bounded by and/but, is the same rule the classifier applies;
    the floor stays a floor for anything that is reported."""
    assert not _rf.evaluate("I don't have chest pain").triggered
    assert not _rf.evaluate("no chest pain, just a runny nose").triggered
    assert _rf.evaluate("no chest pain but I can't breathe").rule == "breathlessness"
    assert _rf.evaluate("no fever, chest pain since this morning").rule == "cardiac_chest_pain"
    assert _rf.evaluate("chest pain, no not going away").rule == "cardiac_chest_pain"
    assert _rf.evaluate("I have never had chest pain like this before").rule == "cardiac_chest_pain"


# Live scenario test 2026-09-24: both came out P3 with no feature lit.
def test_hard_to_wake_child_is_emergent():
    result = evaluate("My 2 year old has a fever of 40 degrees, is very drowsy and hard to wake up.")
    assert result.forced_acuity == "P2_EMERGENT"


def test_head_injury_with_confusion_is_emergent():
    result = evaluate("My 80 year old father fell at home, hit his head and is now confused.")
    assert result.forced_acuity == "P2_EMERGENT"


def test_plain_drowsiness_and_denied_confusion_do_not_fire():
    assert not evaluate("A bit drowsy after my cold medicine.").triggered
    assert not evaluate("Bumped my head on a cupboard, no confusion, no vomiting.").triggered


def test_meningitis_pattern_is_emergent():
    for text in ("High fever with a stiff neck and a purple rash.", "My neck is stiff and I have a temperature.",
                 "There is a rash that does not fade when I press a glass on it.", "Non-blanching spots on his legs."):
        assert evaluate(text).forced_acuity == "P2_EMERGENT", text


def test_stiff_neck_or_rash_alone_does_not_fire():
    for text in ("Neck stiffness from sleeping wrong.", "Stiff neck, no fever.", "Itchy rash after a new soap.",
                 "Purple bruise on my leg."):
        assert not evaluate(text).triggered, text
