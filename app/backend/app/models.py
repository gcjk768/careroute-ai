"""Pydantic request/response models and the acuity lookup table.

Kept dependency-free of the rest of the app so it can be imported anywhere
without circular imports.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------
# Acuity
# --------------------------------------------------------------------------
class AcuityCode(StrEnum):
    P1 = "P1_RESUSCITATION"
    P2 = "P2_EMERGENT"
    P3 = "P3_URGENT"
    P4 = "P4_NON_URGENT"
    P5 = "P5_SELF_CARE"


# code -> (label, numeric score, care tier)
ACUITY_TABLE: dict[str, dict[str, Any]] = {
    AcuityCode.P1.value: {"label": "Resuscitation", "score": 1, "care_tier": "Emergency Department"},
    AcuityCode.P2.value: {"label": "Emergent", "score": 2, "care_tier": "Emergency Department"},
    AcuityCode.P3.value: {"label": "Urgent", "score": 3, "care_tier": "Urgent Care"},
    AcuityCode.P4.value: {"label": "Non-urgent", "score": 4, "care_tier": "GP"},
    AcuityCode.P5.value: {"label": "Self-care", "score": 5, "care_tier": "Telehealth"},
}

ACUITY_ORDER = [AcuityCode.P1.value, AcuityCode.P2.value, AcuityCode.P3.value, AcuityCode.P4.value, AcuityCode.P5.value]


def acuity_rank(code: str) -> int:
    """Lower rank = more severe. Used to compare/escalate acuity."""
    try:
        return ACUITY_ORDER.index(code)
    except ValueError:
        return len(ACUITY_ORDER)


def more_severe(a: str, b: str) -> str:
    """Return whichever of a/b is more severe (lower rank)."""
    return a if acuity_rank(a) <= acuity_rank(b) else b


class Acuity(BaseModel):
    code: str
    label: str
    score: int

    @classmethod
    def from_code(cls, code: str) -> Acuity:
        row = ACUITY_TABLE.get(code, ACUITY_TABLE[AcuityCode.P3.value])
        return cls(code=code, label=row["label"], score=row["score"])


# --------------------------------------------------------------------------
# API request/response models
# --------------------------------------------------------------------------
# Upper bound on free-text symptom input. This MUST stay equal to
# `guardrail._MAX_INPUT_CHARS`: the guardrail's own cap only fires inside the SSE
# generator, i.e. after the request was accepted, a case id minted and an audit
# trail opened, so it cannot bound what the server parses and holds in memory.
# Enforcing the same number on the model rejects an oversized body at the edge
# with a 422. This module is deliberately free of intra-app imports (see the
# module docstring), so the constant is declared here and `test_api_e2e.py`
# asserts the two never drift apart.
MAX_INPUT_CHARS = 6000


# The clarifying interview (docs/design/specs/2026-09-26-clarifying-chat-interview-
# design.md). A resumed turn carries the ORIGINAL complaint in `text` plus every
# question asked so far with the patient's answer — no server-side session, so a
# turn is a stateless, replayable request. The bounds are the loop's safety
# rails: at most INTERVIEW_MAX_QUESTIONS answers (mirrors agents/base.py; the
# eval asserts the two agree), each short enough that an "answer" cannot become a
# second free-text channel around the guardrail's input cap.
MAX_CLARIFICATIONS = 4
MAX_ANSWER_CHARS = 300


class ClarificationAnswer(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    answer: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)
    # The classifier's gap name (a symptom feature, or an interview dimension
    # such as "onset"); lets the classifier avoid re-asking the same gap.
    feature: str | None = Field(default=None, max_length=80)
    source: Literal["template", "llm", "fallback"] | None = None
    # The symptom terms a yes/no answer asserts ("fever, difficulty breathing");
    # intake folds "no" into "no fever, no difficulty breathing".
    statement: str | None = Field(default=None, max_length=120)


class TriageRequest(BaseModel):
    sessionId: str | None = None
    text: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    language: str = "en"
    isVoice: bool = False
    clarifications: list[ClarificationAnswer] = Field(default_factory=list, max_length=MAX_CLARIFICATIONS)
    # Optional structured intake — demographics feed the age-aware severity model.
    ageBand: str | None = None  # "0-17" | "18-39" | "40-64" | "65+"
    sex: str | None = None       # "Female" | "Male" | None
    # Coordinates supplied by a location picker / browser geolocation.  The
    # routing worker uses these only to rank clinics from the public dataset.
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    transportMode: Literal["unknown", "walk", "cycle", "public", "drive", "taxi"] = "unknown"
    # P2 routes are withheld until the user explicitly confirms safe
    # self-transport. This is not a clinical clearance and never applies to P1.
    emergencySelfTransportConfirmed: bool = False
    maxTravelTimeMin: int | None = Field(default=None, ge=1, le=240)
    preferredClinicId: str | None = Field(default=None, max_length=120)
    accessibilityNeed: Literal["none", "step_free", "wheelchair"] = "none"
    preferredLanguage: str | None = Field(default=None, min_length=2, max_length=12)
    affordabilityPreference: Literal["standard", "chas_subsidy"] = "standard"

    @field_validator("transportMode", mode="before")
    @classmethod
    def normalise_ride_hailing_transport(cls, value: object) -> object:
        """Map a patient-facing Singapore ride-hailing brand to ``taxi``.

        This is deliberately exact-label normalisation, not free-text NLP:
        symptom intake/conversation components must extract the transport value
        before it reaches this API field.
        """
        if not isinstance(value, str):
            return value
        label = value.strip().casefold()
        aliases = {
            "grab", "grabcar", "uber", "ryde", "tada", "gojek", "private hire",
            "private-hire", "private hire car", "phv", "ride hailing", "ride-hailing",
            "comfortdelgro", "comfort delgro", "cdg zig", "zig", "transcab", "trans-cab",
        }
        return "taxi" if label in aliases else label


class Citation(BaseModel):
    title: str
    snippet: str
    source: str


class DecisionRequest(BaseModel):
    decision: str
    note: str = ""
    clinician: str = ""
    # [MLOps][HITL] The clinician's FINAL acuity for the case (an AcuityCode
    # value). Optional + additive: when supplied it becomes a ground-truth
    # label — the model's prediction vs the human decision — feeding live
    # production accuracy and the retraining dataset (the HITL label loop).
    finalAcuity: str | None = None


class HealthResponse(BaseModel):
    status: str
    llm: bool  # an LLM provider (the hosted OpenAI API) is available
    model: str
    # [MLOps] The model each router tier resolves to ({"fast", "deep", "max"}); empty
    # when no provider is available. `model` above is only the base model.
    tiers: dict[str, str] = Field(default_factory=dict)
    # [AI-Security] Runtime-control visibility (additive/optional).
    killSwitch: bool = False
    rateLimitPerMin: int | None = None
    metrics: bool = False
    # [AI-Security][ASI08] Per-provider circuit-breaker state. A provider can be
    # reachable and still be skipped because it kept failing, and an operator who
    # cannot see that reads the skip as an outage of the whole chain. Empty until
    # a provider has actually failed once.
    breakers: dict[str, dict] = Field(default_factory=dict)
    # [Microservices] Per-agent-container breaker state when AGENT_TRANSPORT=http.
    agents: dict[str, dict] = Field(default_factory=dict)
    # [AI-Security] Whether the staff endpoints are key-protected ("api-key")
    # or open ("open"). Visible so an unprotected deployment is never silent.
    staffAuth: str = "open"
    # [Ops] OneMap readiness: {"configured", "circuit", "routing"}. Same reason
    # as staffAuth — a deployment missing ONEMAP_EMAIL/ONEMAP_PASSWORD still
    # serves map tiles and a labelled straight-line estimate, so the degradation
    # is invisible from the outside. See agents.routing.onemap_status.
    oneMap: dict[str, str | bool] = Field(default_factory=dict)
    # [Ops] Which backend is ACTIVE, not which is configured: both degrade
    # silently (Safety-NLP to its rules, retrieval to TF-IDF), so a deployment
    # is verifiable from outside only here. See scripts/check_active_backends.py.
    safetyNlp: str = "rules"
    retrieval: str = "tfidf"


class FeedbackRequest(BaseModel):
    """[Responsible-AI] PDPC Stakeholder-Interaction pillar: lets a patient
    challenge / rate an AI decision. Recorded into the case audit trail."""

    helpful: bool | None = None
    comment: str = ""


class EscalationSummary(BaseModel):
    id: str
    caseId: str
    reason: str
    confidence: float
    acuity: Acuity
    createdAt: str
    status: Literal["pending", "decided"]
    patientSummary: str
    # [HITL] SLA, evaluated on read (store.apply_sla). Additive/optional.
    slaDueAt: str | None = None
    slaBreached: bool = False


class EscalationDetail(EscalationSummary):
    rationale: str
    citations: list[Citation]
    evidence: list[str]
    normalisedSymptoms: str
    language: str
    decision: str | None = None
    note: str | None = None
    clinician: str | None = None
    decidedAt: str | None = None
    # [MLOps][HITL] Ground-truth fields (additive, default None so existing
    # clients are unaffected): the clinician's final acuity and whether it
    # agreed with the model's triage — live labelled accuracy per case.
    finalAcuity: str | None = None
    modelAgreed: bool | None = None
    # [Responsible-AI] SHAP-surrogate signed feature contributions from the
    # Severity-Classifier (see agents.SeverityClassifierAgent.explain()).
    # Additive field: defaults to [] so existing clients are unaffected.
    explanation: list[dict[str, Any]] = Field(default_factory=list)
    # [Responsible-AI] Provenance of `explanation`: "shap" | "llm" | "keyword".
    explanationSource: str | None = None
    # [Agentic] Clinician-Handoff packet (see agents.ClinicianHandoffAgent).
    # The plain-language summary a reviewing clinician reads first, its
    # grounding citations, and up to two suggested follow-up questions.
    # Additive: defaults keep existing clients unaffected. The summary has
    # already passed the LLM05 output guardrail in main.py by the time it
    # reaches here.
    handoffSummary: str = ""
    handoffCitations: list[Citation] = Field(default_factory=list)
    handoffQuestions: list[str] = Field(default_factory=list)


class FairnessSubgroup(BaseModel):
    name: str
    accuracy: float
    n: int


class FairnessDrift(BaseModel):
    data: float
    target: float
    concept: float


class FairnessResponse(BaseModel):
    overallAccuracy: float
    redFlagRecall: float
    fairnessGapBefore: float
    fairnessGapAfter: float
    subgroups: list[FairnessSubgroup]
    drift: FairnessDrift
    modelVersion: str
    updatedAt: str
    # [Responsible-AI] Named classification-fairness metrics (XRAI Day 2) +
    # counterfactual-fairness audit. Optional/additive so the seeded snapshot
    # and older clients still validate.
    demographicParity: dict[str, Any] | None = None
    equalOpportunity: dict[str, Any] | None = None
    counterfactual: dict[str, Any] | None = None
    # [Responsible-AI] Equalized Odds (TPR *and* FPR gap), Disparate Impact
    # (four-fifths rule) and the calibration block (method, ECE, Brier) are all
    # computed by ml/model.build_artifact() — but until 2026-09-15 they were not
    # declared here, and pydantic silently DROPPED them from the response. The
    # docs claimed they were served; the endpoint disagreed. Declared now so the
    # served payload matches the audit that was actually computed.
    equalizedOdds: dict[str, Any] | None = None
    disparateImpact: dict[str, Any] | None = None
    calibration: dict[str, Any] | None = None
    # [XRAI] Global explanation: top features by mean |SHAP| + impurity
    # importance, and partial dependence of P(urgent) for the top three.
    globalExplanation: dict[str, Any] | None = None
    # [XRAI] 5-fold stratified cross-validation of the served pipeline: mean,
    # std and per-fold values for accuracy, red-flag recall and fairness gap.
    crossValidation: dict[str, Any] | None = None
    # [XRAI] SHAP vs LIME-style local-surrogate agreement on held-out rows
    # (top-3 overlap, Spearman) — explanation faithfulness evidence.
    explanationAgreement: dict[str, Any] | None = None
    # [Responsible-AI] Post-processing mitigation: per-group severe thresholds
    # and before/after fairness on the calibrated pipeline.
    postProcessing: dict[str, Any] | None = None


class CaseRecord(BaseModel):
    """Internal record persisted in the in-memory store."""

    caseId: str
    sessionId: str | None = None
    rawText: str
    language: str
    isVoice: bool
    normalisedSymptoms: str
    acuity: Acuity
    confidence: float
    careTier: str
    clinic: str | None = None
    waitTimeMin: int | None = None
    travelEstimateSource: str | None = None
    routeInstructions: list[str] = Field(default_factory=list)
    routeAvailable: bool = False
    escalated: bool
    rationale: str
    citations: list[Citation]
    evidence: list[str]
    safetyTriggered: bool
    safetyRule: str | None = None
    createdAt: str
    # [Responsible-AI] SHAP-surrogate signed feature contributions from the
    # Severity-Classifier. Additive field: defaults to [] so existing
    # consumers of CaseRecord are unaffected.
    explanation: list[dict[str, Any]] = Field(default_factory=list)
    # [Responsible-AI] Provenance of `explanation`: "shap" | "llm" | "keyword".
    explanationSource: str | None = None
    # [Agentic] Reflection/Critic verdict; [AI-Security] PII kinds redacted.
    reflection: dict[str, Any] = Field(default_factory=dict)
    redactedPii: list[str] = Field(default_factory=list)
    # [Agentic] Clinician-Handoff packet — see EscalationDetail for the field
    # notes. Empty on every non-escalated case, which is the common one.
    handoffSummary: str = ""
    handoffCitations: list[Citation] = Field(default_factory=list)
    handoffQuestions: list[str] = Field(default_factory=list)
