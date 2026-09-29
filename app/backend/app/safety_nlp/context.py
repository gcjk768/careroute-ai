"""CareRoute subject and temporality context rules for Safety NLP shadow mode."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .contracts import AdapterConfig, ContextResult, MentionSpan, OptionalModelAdapter

CAREROUTE_CONTEXT_RULE_VERSION = "careroute-context-rules-0.1.0"
CAREROUTE_CONTEXT_CLINICAL_REVIEW = "not_clinically_reviewed"

_CARE_SUBJECT_RE = re.compile(r"\b(my child|my daughter|my son|my baby|my mother|my father)\b", re.IGNORECASE)
_OTHER_PERSON_RE = re.compile(r"\b(my brother|my sister|someone|another person|a stranger|my friend)\b", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\b(years ago|ten years ago|last year|previous history|as a child)\b", re.IGNORECASE)
_RECENT_RE = re.compile(r"\b(minutes ago|five minutes ago|just now|earlier today|recently|this morning)\b", re.IGNORECASE)
_UNKNOWN_TIME_RE = re.compile(r"\b(sometimes|not sure when|comes and goes|off and on)\b", re.IGNORECASE)
_CONDITIONAL_RE = re.compile(r"\b(if|would|could|might|what if|suppose)\b", re.IGNORECASE)


@dataclass
class CareRouteContextRuleAdapter(OptionalModelAdapter):
    """Rule-based medspaCy ConText-compatible adapter in shadow mode.

    The first release keeps these rules local and explicit. They are shaped like
    a future medspaCy-backed adapter but do not require medspaCy for normal
    tests. The rules are synthetic project scaffolding and are not clinically
    reviewed.
    """

    config: AdapterConfig = field(default_factory=lambda: AdapterConfig(
        model_name="careroute-medspacy-context-rules",
        model_revision=CAREROUTE_CONTEXT_RULE_VERSION,
    ))

    def classify(self, text: str, mention: MentionSpan) -> ContextResult:
        started = time.perf_counter()
        status = self.availability_status(text)
        if status != "success":
            return self._result(mention.mention_id, "unknown", "unknown", False, status, started)

        before = text[:mention.start]
        window = text[max(0, mention.start - 80):min(len(text), mention.end + 80)]
        subject = _subject_from_text(before)
        temporality = _temporality_from_text(window)
        conditional = bool(_CONDITIONAL_RE.search(window))
        return self._result(mention.mention_id, subject, temporality, conditional, "success", started)

    def _result(
        self,
        mention_id: str,
        subject: str,
        temporality: str,
        conditional: bool,
        status: str,
        started: float,
    ) -> ContextResult:
        return ContextResult(
            mention_id=mention_id,
            subject=subject,
            temporality=temporality,
            conditional=conditional,
            model_name=self.model_name,
            model_revision=self.model_revision,
            status=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _subject_from_text(before_mention: str) -> str:
    if _CARE_SUBJECT_RE.search(before_mention):
        return "care_subject"
    if _OTHER_PERSON_RE.search(before_mention):
        return "other_person"
    if before_mention.strip():
        return "patient"
    return "unknown"


def _temporality_from_text(window: str) -> str:
    if _REMOTE_RE.search(window):
        return "remote"
    if _RECENT_RE.search(window):
        return "recent"
    if _UNKNOWN_TIME_RE.search(window):
        return "unknown"
    return "current"
