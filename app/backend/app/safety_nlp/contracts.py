"""Typed contracts for the Safety NLP shadow pipeline.

These types describe the handoff between small adapters. They are intentionally
plain Python dataclasses so normal tests can exercise the pipeline without model
artifacts, network access or heavyweight NLP dependencies.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from typing import Protocol

AdapterStatus = str
VALID_ADAPTER_STATUSES = frozenset({"success", "disabled", "unavailable", "timeout", "invalid_output"})


# ONE long-lived worker for every adapter's bounded model call. A fresh thread
# per call (the old per-call ThreadPoolExecutor) leaked torch's per-calling-thread
# intra-op pool: 22 -> 754 threads and 1.1 -> 2.3 GB RSS over 30 local triages.
# ponytail: one worker serialises inference across concurrent triages; calls
# queued past their budget report "timeout" (shadow only). Add workers if
# shadow timeouts show up under load.
_MODEL_WORKER = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="safety-nlp-model")


def run_with_timeout(func, timeout_ms: int):
    """Run a blocking model call on the shared worker, bounded by `timeout_ms`.

    On timeout the call is dropped if still queued; a call already running
    finishes in the background (a thread cannot be killed)."""
    future = _MODEL_WORKER.submit(func)
    try:
        return future.result(timeout=timeout_ms / 1000)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise


def _validate_status(status: AdapterStatus) -> None:
    if status not in VALID_ADAPTER_STATUSES:
        raise ValueError(f"invalid adapter status: {status!r}")


@dataclass(frozen=True)
class AdapterConfig:
    """Common runtime configuration for an optional Safety NLP adapter."""

    model_name: str
    model_revision: str
    enabled: bool = True
    requires_artifact: bool = False
    artifact_path: str | None = None
    timeout_ms: int = 1000
    device: str = "cpu"
    max_input_chars: int = 2000

    def __post_init__(self) -> None:
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        if self.max_input_chars <= 0:
            raise ValueError("max_input_chars must be positive")


@dataclass
class OptionalModelAdapter:
    """Common base for fake and real optional model adapters.

    Real adapters should set `loaded=True` only after their local artifact is
    ready. They should return a typed status instead of raising through triage.
    Tests may use `status_override` to force timeout/unavailable/invalid_output
    paths without loading any model.
    """

    config: AdapterConfig
    loaded: bool = True
    status_override: AdapterStatus | None = None

    @property
    def model_name(self) -> str:
        return self.config.model_name

    @property
    def model_revision(self) -> str:
        return self.config.model_revision

    def availability_status(self, text: str = "") -> AdapterStatus:
        if self.status_override is not None:
            _validate_status(self.status_override)
            return self.status_override
        if not self.config.enabled:
            return "disabled"
        if self.config.requires_artifact and not self.loaded:
            return "unavailable"
        if text and len(text) > self.config.max_input_chars:
            return "invalid_output"
        return "success"


@dataclass(frozen=True)
class TranslationResult:
    original_text: str
    language: str
    translated_text: str
    model_name: str
    model_revision: str
    status: AdapterStatus = "success"
    latency_ms: int = 0

    def __post_init__(self) -> None:
        _validate_status(self.status)

    @property
    def working_text(self) -> str:
        return self.translated_text or self.original_text


@dataclass(frozen=True)
class MentionSpan:
    mention_id: str
    text: str
    start: int
    end: int
    entity_type: str = "Sign_symptom"
    source_language: str = "unknown"
    status: AdapterStatus = "success"
    model_name: str = "fake-ner"
    model_revision: str = "local-test"
    latency_ms: int = 0

    def __post_init__(self) -> None:
        _validate_status(self.status)
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"invalid mention offsets: {self.start}:{self.end}")
        if len(self.text) != self.end - self.start:
            raise ValueError("mention text length must match start/end offsets")


@dataclass(frozen=True)
class AssertionResult:
    mention_id: str
    assertion: str
    confidence: float
    model_name: str = "fake-assertion"
    model_revision: str = "local-test"
    status: AdapterStatus = "success"
    latency_ms: int = 0

    def __post_init__(self) -> None:
        _validate_status(self.status)
        if self.assertion not in {"present", "possible", "negated", "conditional", "unknown"}:
            raise ValueError(f"invalid assertion: {self.assertion!r}")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(f"invalid assertion confidence: {self.confidence!r}")


@dataclass(frozen=True)
class ContextResult:
    mention_id: str
    subject: str = "unknown"
    temporality: str = "unknown"
    conditional: bool = False
    model_name: str = "fake-context"
    model_revision: str = "local-test"
    status: AdapterStatus = "success"
    latency_ms: int = 0

    def __post_init__(self) -> None:
        _validate_status(self.status)
        if self.subject not in {"patient", "care_subject", "other_person", "unknown"}:
            raise ValueError(f"invalid subject: {self.subject!r}")
        if self.temporality not in {"current", "recent", "remote", "unknown"}:
            raise ValueError(f"invalid temporality: {self.temporality!r}")


@dataclass(frozen=True)
class CategoryCandidate:
    mention_id: str
    category: str | None
    confidence: float
    model_name: str = "fake-category"
    model_revision: str = "local-test"
    status: AdapterStatus = "success"
    latency_ms: int = 0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_status(self.status)
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(f"invalid category confidence: {self.confidence!r}")


class TranslationAdapter(Protocol):
    def translate(self, text: str, language: str) -> TranslationResult:
        """Return a working English copy when translation is enabled/available."""


class NerAdapter(Protocol):
    def extract(self, text: str, language: str) -> list[MentionSpan]:
        """Return symptom/entity mentions in the working text."""


class AssertionAdapter(Protocol):
    def classify(self, text: str, mention: MentionSpan) -> AssertionResult:
        """Return whether this mention is asserted, negated, possible, etc."""


class ContextAdapter(Protocol):
    def classify(self, text: str, mention: MentionSpan) -> ContextResult:
        """Return subject and temporality context for this mention."""


class CategoryAdapter(Protocol):
    def rank(self, text: str, mention: MentionSpan) -> list[CategoryCandidate]:
        """Return candidate red-flag categories for this mention."""


@dataclass(frozen=True)
class SafetyNlpAdapters:
    translation: TranslationAdapter
    ner: NerAdapter
    assertion: AssertionAdapter
    context: ContextAdapter
    category: CategoryAdapter
    category_comparators: tuple[CategoryAdapter, ...] = ()
