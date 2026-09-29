"""Patient-facing rationale wording — owner: Sham Goh.   Run with: pytest -m intake

`build_rationale` output is rendered on the PATIENT's result card, so its wording
is a clinical-safety surface, not cosmetics. The rule these tests defend:

    on a P1/P2 case, nothing may suggest the patient waits for the clinician.

The original copy ("routed for human review; the final decision rests with the
clinician and will supersede this recommendation") said exactly that on a
call-995 case. These tests stop it coming back.
"""
import pytest

from app.agents import SymptomIntakeAgent
from app.agents.base import CaseState

pytestmark = pytest.mark.intake


def _case(**overrides) -> CaseState:
    state = CaseState(raw_text="placeholder")
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _emergency_case(**overrides) -> CaseState:
    base = dict(
        normalised_symptoms="Shortness of breath.",
        acuity_code="P1_RESUSCITATION",
        prior_acuity_code="P1_RESUSCITATION",
        confidence=0.98,
        evidence=["breathlessness"],
        care_tier="Emergency Department",
        clinic="Call 995 / nearest Emergency Department",
        wait_time_min=0,
        escalated=True,
        escalation_reason="Reflection: P1_RESUSCITATION requires clinician confirmation.",
    )
    return _case(**{**base, **overrides})


@pytest.fixture
def agent() -> SymptomIntakeAgent:
    return SymptomIntakeAgent()


# ---------------------------------------------------------------- safety rule
@pytest.mark.parametrize("code", ["P1_RESUSCITATION", "P2_EMERGENT"])
def test_emergency_never_tells_the_patient_to_wait(agent, code):
    text = agent.build_rationale(_emergency_case(acuity_code=code, prior_acuity_code=code))
    assert "do not wait" in text.lower()
    # Phrasings that imply the recommendation is provisional pending a human.
    for forbidden in ("rests with the clinician", "supersede", "awaiting", "pending"):
        assert forbidden not in text.lower(), f"{forbidden!r} implies waiting on a {code}"


def test_non_emergency_escalation_may_say_the_advice_could_change(agent):
    text = agent.build_rationale(
        _case(
            normalised_symptoms="Persistent vomiting.",
            acuity_code="P3_URGENT",
            confidence=0.45,
            evidence=["vomiting"],
            care_tier="Urgent Care",
            clinic="Bedok Urgent Care Clinic",
            wait_time_min=40,
            escalated=True,
            escalation_reason="Classifier confidence 0.45 is below the 0.60 escalation threshold.",
        )
    )
    assert "may update this advice" in text
    # Still tells them what to do if things turn while they wait.
    assert "treat it as an emergency" in text


# ------------------------------------------------------------- plain language
def test_no_raw_acuity_codes_or_agent_names_reach_the_patient(agent):
    text = agent.build_rationale(_emergency_case())
    for jargon in ("P1_RESUSCITATION", "P2_EMERGENT", "Reflection:", "acuity"):
        assert jargon not in text, f"{jargon!r} is internal vocabulary"
    assert "a life-threatening emergency (P1)" in text


def test_confidence_is_a_percentage_not_a_float(agent):
    text = agent.build_rationale(_emergency_case(confidence=0.98))
    assert "98% confident" in text
    assert "0.98" not in text


def test_symptom_text_does_not_produce_a_double_full_stop(agent):
    # normalised_symptoms usually already ends in "."; appending another gave
    # "Shortness of breathe.." on the real screen.
    text = agent.build_rationale(_emergency_case(normalised_symptoms="Shortness of breath."))
    assert ".." not in text


def test_destination_is_not_said_twice(agent):
    # care_tier "Emergency Department" is a substring of the clinic string, so
    # joining them blindly named the same place twice in one sentence.
    text = agent.build_rationale(_emergency_case())
    assert text.count("Emergency Department") == 1


def test_distinct_care_tier_and_clinic_are_both_kept(agent):
    text = agent.build_rationale(
        _case(
            normalised_symptoms="Mild sore throat.",
            acuity_code="P4_NON_URGENT",
            confidence=0.71,
            evidence=["sore throat"],
            care_tier="GP / Polyclinic",
            clinic="Tampines Polyclinic",
            wait_time_min=35,
        )
    )
    assert "GP / Polyclinic" in text and "Tampines Polyclinic" in text


def test_evidence_list_reads_as_a_sentence(agent):
    text = agent.build_rationale(
        _emergency_case(evidence=["breathlessness", "chest pain", "pallor"])
    )
    assert "breathlessness, chest pain and pallor" in text


# ------------------------------------------------------------------- wait time
def test_wait_time_is_hidden_on_an_emergency(agent):
    # "estimated wait 0 min" reads like a queue position for something that is
    # not a queue.
    text = agent.build_rationale(_emergency_case(wait_time_min=0))
    assert "wait" not in text.replace("Do not wait", "").lower()


def test_wait_time_is_shown_when_it_is_actionable(agent):
    text = agent.build_rationale(
        _case(
            normalised_symptoms="Mild sore throat.",
            acuity_code="P4_NON_URGENT",
            confidence=0.71,
            care_tier="GP / Polyclinic",
            clinic="Tampines Polyclinic",
            wait_time_min=35,
        )
    )
    assert "about 35 minutes" in text


# ----------------------------------------------------------------- edge cases
def test_empty_state_still_produces_a_sentence(agent):
    text = agent.build_rationale(_case(acuity_code="P3_URGENT", confidence=0.6))
    assert text.strip()
    assert "None" not in text


# The three below were all found by running a real case through the browser,
# not by reading the code — the producing agents write sentence-shaped
# fragments, which only misbehave once they are inlined.
def test_real_safety_reason_does_not_leave_a_double_period(agent):
    # hitl.py appends "." to a safety_reason that already ends in one.
    text = agent.build_rationale(
        _emergency_case(
            escalation_reason=(
                "Safety-override triggered rule 'cardiac_chest_pain': "
                "Possible acute coronary syndrome (chest pain pattern).."
            )
        )
    )
    assert ".." not in text


def test_sentence_shaped_evidence_is_lowercased_mid_sentence(agent):
    text = agent.build_rationale(
        _emergency_case(
            evidence=["chest pain", "Possible acute coronary syndrome (chest pain pattern)."]
        )
    )
    assert "and possible acute coronary syndrome (chest pain pattern)." in text
    assert "and Possible" not in text


def test_acronym_evidence_keeps_its_capitals(agent):
    text = agent.build_rationale(_emergency_case(evidence=["ECG abnormality"]))
    assert "ECG abnormality" in text


def test_safety_reason_is_not_wrapped_in_nested_brackets(agent):
    text = agent.build_rationale(
        _emergency_case(
            safety_triggered=True,
            safety_rule="cardiac_chest_pain",
            safety_reason="Possible acute coronary syndrome (chest pain pattern).",
        )
    )
    assert "pattern).)" not in text
    assert "A safety rule — possible acute coronary syndrome (chest pain pattern) —" in text


# --------------------------------------- the badge and the prose must agree
def test_leads_with_the_final_acuity_not_the_classifiers_first_guess(agent):
    # Found on screen: the badge read "P2 · Emergent" while this paragraph
    # said P3 and never mentioned the upgrade. Reflection raised it, and only
    # a Safety-driven raise was being explained.
    text = agent.build_rationale(
        _case(
            normalised_symptoms="Vague complaint.",
            acuity_code="P2_EMERGENT",
            prior_acuity_code="P3_URGENT",
            confidence=0.43,
            care_tier="Emergency Department",
            clinic="Call 995 / nearest Emergency Department",
            escalated=True,
            escalation_reason="Reflection re-run: thin evidence persisted — escalated for caution.",
        )
    )
    assert "We assessed this as an emergency (P2)" in text
    assert "first read this as urgent, but not an emergency (P3)" in text
    assert "raised the level as a precaution" in text


def test_a_reflection_raise_is_not_attributed_to_a_safety_rule(agent):
    text = agent.build_rationale(
        _case(
            acuity_code="P2_EMERGENT", prior_acuity_code="P3_URGENT", confidence=0.43,
            care_tier="Emergency Department", clinic="Call 995",
            safety_triggered=False,
        )
    )
    assert "safety rule" not in text.lower()


def test_agent_name_prefixes_are_stripped_from_the_reason(agent):
    text = agent.build_rationale(
        _emergency_case(
            escalation_reason="Reflection re-run: thin evidence persisted — escalated for caution."
        )
    )
    assert "Reflection" not in text


def test_a_rewritten_fragment_mid_reason_is_capitalised(agent):
    # "...escalated for caution. we were not confident enough..." — the rewrite
    # lands after a sentence break, so it has to start with a capital.
    text = agent.build_rationale(
        _emergency_case(
            escalation_reason=(
                "Reflection re-run: thin evidence persisted — escalated for caution. "
                "Classifier confidence 0.43 is below the 0.60 escalation threshold."
            )
        )
    )
    assert ". we were not" not in text
    assert "We were not confident enough" in text


def test_safety_override_is_explained_as_unoverridable(agent):
    text = agent.build_rationale(
        _emergency_case(
            prior_acuity_code="P3_URGENT",
            safety_triggered=True,
            safety_rule="CARDIAC_CHEST_PAIN",
            safety_reason="cardiac red flag",
        )
    )
    assert "cannot be overruled by the model" in text
    assert "CARDIAC_CHEST_PAIN" not in text
