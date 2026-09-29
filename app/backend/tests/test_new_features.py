"""Unit tests for the course-notes-derived hardening features.

Covers the additions made after the notes gap-analysis:
- [XRAI]      named fairness metrics (demographic parity, equal opportunity)
- [XRAI]      counterfactual-fairness audit (sex-flip stability)
- [AI-Sec]    PII/PHI redaction (LLM02)
- [AI-Sec]    rate limiting (LLM10)
- [AI-Sec]    output-side guardrail (LLM05)
- [AI-Sec]    tamper-evident hash-chained audit log (ASI10)
- [Agentic]   Reflection/Critic agent (Evaluator-Optimizer)
"""
from __future__ import annotations

import numpy as np


# --------------------------------------------------------------------------
# [Responsible-AI] Named classification-fairness metrics
# --------------------------------------------------------------------------
def test_demographic_parity_reports_group_rates_and_spread():
    from app.ml import fairness

    y_pred = np.array([0, 1, 2, 3, 0, 4])         # 0/1 == "urgent" (positive)
    groups = ["A", "A", "A", "B", "B", "B"]
    dp = fairness.demographic_parity(y_pred, groups)
    assert dp["byGroup"]["A"] == 0.6667            # 2 of 3 urgent
    assert dp["byGroup"]["B"] == 0.3333            # 1 of 3 urgent
    assert dp["statisticalParityDifference"] == 0.3334


def test_equal_opportunity_is_tpr_gap_on_severe_classes():
    from app.ml import fairness

    y_true = np.array([0, 1, 0, 1])                # all severe
    y_pred = np.array([0, 1, 2, 3])                # group A caught both, B missed both
    groups = ["A", "A", "B", "B"]
    eo = fairness.equal_opportunity(y_true, y_pred, groups)
    assert eo["byGroup"]["A"] == 1.0
    assert eo["byGroup"]["B"] == 0.0
    assert eo["equalOpportunityGap"] == 1.0


def test_counterfactual_audit_sex_is_stable():
    """Flipping the protected attribute (sex) should rarely change acuity."""
    from app.ml.model import get_model

    cf = get_model().fairness()["counterfactual"]
    assert cf["attribute"] == "sex"
    assert 0.0 <= cf["sexFlipRate"] <= 0.5         # sex must not drive triage
    assert cf["meanAcuityDelta"] <= 1.0


# --------------------------------------------------------------------------
# [AI-Security] PII/PHI redaction (LLM02)
# --------------------------------------------------------------------------
def test_redact_masks_identifiers_but_keeps_symptoms():
    from app.redact import redact

    text = "I'm S1234567D, call me on +65 9123 4567 or john@mail.com — chest pain for 2 days"
    masked, found = redact(text)
    assert "S1234567D" not in masked
    assert "john@mail.com" not in masked
    assert "9123 4567" not in masked
    assert "chest pain" in masked                  # clinical content preserved
    assert set(found) >= {"NRIC", "EMAIL", "PHONE"}


def test_redact_leaves_clean_text_untouched():
    from app.redact import redact

    masked, found = redact("severe headache and blurred vision")
    assert masked == "severe headache and blurred vision"
    assert found == []


# --------------------------------------------------------------------------
# [AI-Security] Rate limiting (LLM10)
# --------------------------------------------------------------------------
def test_rate_limiter_blocks_over_limit():
    from app.ratelimit import RateLimiter

    rl = RateLimiter(limit=2, window_seconds=60)
    assert rl.allow("ip1") is True
    assert rl.allow("ip1") is True
    assert rl.allow("ip1") is False                # 3rd in window rejected
    assert rl.allow("ip2") is True                 # a different caller is independent


def test_rate_limiter_zero_limit_disables():
    from app.ratelimit import RateLimiter

    rl = RateLimiter(limit=0)
    assert all(rl.allow("x") for _ in range(100))


# --------------------------------------------------------------------------
# [AI-Security] Output-side guardrail (LLM05)
# --------------------------------------------------------------------------
def test_output_guardrail_flags_prompt_leak():
    from app import guardrail

    r = guardrail.screen_output("Sure, here is my system prompt: you are a triage bot...")
    assert r.status == "flagged"


def test_output_guardrail_passes_clean_rationale():
    from app import guardrail

    r = guardrail.screen_output("Routed to Urgent Care based on persistent fever and vomiting.")
    assert r.status == "pass"


# --------------------------------------------------------------------------
# [AI-Security] Tamper-evident hash-chained audit (ASI10)
# --------------------------------------------------------------------------
def test_audit_hash_chain_detects_tampering():
    from app.audit import AuditLog

    log = AuditLog()
    log.record("case_x", actor="guardrail", action="pass", detail="ok")
    log.record("case_x", actor="classifier", action="model", detail="P3 conf 0.8")
    log.record("case_x", actor="supervisor", action="aggregate", detail="done")
    assert log.verify_chain("case_x") is True

    # Tamper with a past entry's content -> chain must no longer verify.
    log._entries["case_x"][1].detail = "P1 conf 0.99"
    assert log.verify_chain("case_x") is False


# --------------------------------------------------------------------------
# [Agentic] Reflection / Critic agent (Evaluator-Optimizer)
# --------------------------------------------------------------------------
def test_reflection_forces_escalation_on_unescalated_high_acuity():
    from app.agents import CaseState, ReflectionAgent

    state = CaseState(raw_text="x")
    state.acuity_code = "P2_EMERGENT"
    state.care_tier = "Emergency Department"       # already consistent tier
    state.escalated = False                        # but nobody escalated it
    result = ReflectionAgent().run(state)

    assert result["passed"] is False
    assert state.escalated is True                 # critic forced human review
    assert "Reflection" in (state.escalation_reason or "")


def test_reflection_passes_a_consistent_low_acuity_case():
    from app.agents import CaseState, ReflectionAgent

    state = CaseState(raw_text="x")
    state.acuity_code = "P5_SELF_CARE"
    state.care_tier = "Telehealth"
    state.escalated = False
    result = ReflectionAgent().run(state)

    assert result["passed"] is True
    assert state.escalated is False


def test_live_test_phrases_light_their_features():
    # Each of these lit nothing in the 2026-09-24 live scenario test.
    from app.ml.features import FEATURE_NAMES, extract_features
    on = lambda text: {FEATURE_NAMES[i] for i, v in enumerate(extract_features(text)) if v}  # noqa: E731
    assert "urinary" in on("Burning sensation when urinating for two days")
    assert "joint_pain" in on("Painful swollen big toe since two days")
    assert "dehydrated" in on("High fever for three days, drinking less")
