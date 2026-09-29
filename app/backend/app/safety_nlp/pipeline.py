"""Safety NLP shadow pipeline facade."""
from __future__ import annotations

from ..agents.safety import SEMANTIC_CATEGORIES, SafetySignal
from .contracts import CategoryCandidate, ContextResult, MentionSpan, SafetyNlpAdapters

_REVIEW_CONTEXTS = {"possible", "unknown"}


def classify(
    text: str,
    language: str = "en",
    *,
    adapters: SafetyNlpAdapters | None = None,
    enabled: bool = True,
) -> list[SafetySignal]:
    """Classify red-flag mentions into SafetySignal objects in shadow mode.

    The live caller must supply the model-backed adapter bundle constructed at
    application startup. Tests may explicitly pass fake adapters. A missing
    bundle is a typed runtime failure, never an implicit fake-model fallback.
    """
    if not enabled:
        return [
            SafetySignal(
                channel="nlp",
                triggered=False,
                category=None,
                source_language=language,
                stage="classifier",
                model_name="safety_nlp.classify",
                model_revision="local-shadow",
                status="disabled",
            )
        ]

    if adapters is None:
        return [_failure_signal(language, "classifier", "safety-nlp-runtime", "unavailable")]

    bundle = adapters
    translation = bundle.translation.translate(text, language)
    working_text = translation.working_text
    if translation.status != "success":
        return [
            SafetySignal(
                channel="nlp",
                triggered=False,
                category=None,
                source_language=language,
                stage="translation",
                model_name=translation.model_name,
                model_revision=translation.model_revision,
                latency_ms=translation.latency_ms,
                status=translation.status,
            )
        ]

    mentions = bundle.ner.extract(working_text, language)
    if not mentions:
        status = _adapter_status(bundle.ner, working_text)
        if status != "success":
            return [_failure_signal(
                language,
                "ner",
                getattr(bundle.ner, "model_name", "safety-nlp-ner"),
                status,
                getattr(bundle.ner, "model_revision", "unknown"),
            )]
        return []

    signals: list[SafetySignal] = []
    for mention in mentions:
        assertion = bundle.assertion.classify(working_text, mention)
        context = bundle.context.classify(working_text, mention)
        candidates = bundle.category.rank(working_text, mention)
        signals.append(_signal_from_stage(language, mention, assertion, context, candidates))
        for comparator in bundle.category_comparators:
            comparator_candidates = comparator.rank(text, mention)
            signals.append(_signal_from_stage(
                language, mention, assertion, context, comparator_candidates
            ))
    return signals


def _signal_from_stage(
    language: str,
    mention: MentionSpan,
    assertion,
    context: ContextResult,
    candidates: list[CategoryCandidate],
) -> SafetySignal:
    candidate = candidates[0] if candidates else CategoryCandidate(
        mention_id=mention.mention_id,
        category=None,
        confidence=0.0,
        status="invalid_output",
    )
    category = candidate.category if candidate.category in SEMANTIC_CATEGORIES else None
    status = _first_failure_status(
        mention.status,
        assertion.status,
        context.status,
        candidate.status,
    )
    normalized_assertion = "conditional" if context.conditional else assertion.assertion
    triggered = _accepted_trigger(
        status=status,
        category=category,
        assertion=normalized_assertion,
        subject=context.subject,
        temporality=context.temporality,
    )
    return SafetySignal(
        channel="nlp",
        triggered=triggered,
        category=category,
        assertion=normalized_assertion,
        temporality=context.temporality,
        subject=context.subject,
        confidence=float(candidate.confidence),
        evidence=f"mention:{mention.mention_id}",
        mention_id=mention.mention_id,
        source_language=language,
        stage="classifier" if status == "success" else "context",
        model_name=candidate.model_name,
        model_revision=candidate.model_revision,
        latency_ms=mention.latency_ms + assertion.latency_ms + context.latency_ms + candidate.latency_ms,
        status=status,
        metadata={
            "assertion_model": assertion.model_name,
            "context_model": context.model_name,
            "mention_model": mention.model_name,
        },
    )


def _accepted_trigger(
    *,
    status: str,
    category: str | None,
    assertion: str,
    subject: str,
    temporality: str,
) -> bool:
    if status != "success" or category is None:
        return False
    if category == "suicidal_ideation" and assertion == "conditional":
        return subject in {"patient", "care_subject"}
    if assertion in {"negated", "conditional"} or assertion in _REVIEW_CONTEXTS:
        return False
    if subject not in {"patient", "care_subject"}:
        return False
    return temporality not in {"remote", "unknown"}


def _first_failure_status(*statuses: str) -> str:
    for status in statuses:
        if status != "success":
            return status
    return "success"


def _adapter_status(adapter, text: str) -> str:
    last_status = getattr(adapter, "last_status", None)
    if last_status in {"disabled", "unavailable", "timeout", "invalid_output"}:
        return str(last_status)
    availability = getattr(adapter, "availability_status", None)
    if not callable(availability):
        return "success"
    try:
        return str(availability(text))
    except Exception:  # noqa: BLE001 - optional adapters must fail closed
        return "invalid_output"


def _failure_signal(
    language: str,
    stage: str,
    model_name: str,
    status: str,
    model_revision: str = "unknown",
) -> SafetySignal:
    return SafetySignal(
        channel="nlp",
        triggered=False,
        category=None,
        assertion="unknown",
        temporality="unknown",
        subject="unknown",
        confidence=0.0,
        source_language=language,
        stage=stage,
        model_name=model_name,
        model_revision=model_revision,
        status=status,
    )
