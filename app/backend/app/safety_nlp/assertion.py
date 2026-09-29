"""Assertion adapters for the Safety NLP shadow pipeline."""
from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import AdapterConfig, AssertionResult, MentionSpan, OptionalModelAdapter, run_with_timeout

CLINICAL_ASSERTION_MODEL_NAME = "bvanaken/clinical-assertion-negation-bert"
CLINICAL_ASSERTION_LICENSE = "unknown"
ENTITY_START_MARKER = "[entity]"
ENTITY_END_MARKER = "[/entity]"

ASSERTION_LABEL_MAP = {
    "present": "present",
    "present_mention": "present",
    "absent": "negated",
    "negative": "negated",
    "negated": "negated",
    "possible": "possible",
    "uncertain": "possible",
    "hypothetical": "conditional",
    "conditional": "conditional",
}


def clinical_assertion_config_from_manifest(
    manifest_entry: dict[str, Any] | None = None,
    *,
    enabled: bool = False,
    timeout_ms: int = 1000,
    device: str = "cpu",
    max_input_chars: int = 2000,
) -> AdapterConfig:
    """Build a local-files-only clinical assertion adapter config."""
    entry = manifest_entry or {}
    return AdapterConfig(
        model_name=str(entry.get("model") or CLINICAL_ASSERTION_MODEL_NAME),
        model_revision=str(entry.get("revision") or "TBD"),
        enabled=enabled,
        requires_artifact=True,
        artifact_path=entry.get("artifact_path"),
        timeout_ms=timeout_ms,
        device=device,
        max_input_chars=max_input_chars,
    )


@dataclass
class ClinicalAssertionAdapter(OptionalModelAdapter):
    """Local-files-only adapter for entity-marked assertion classification."""

    config: AdapterConfig = field(default_factory=clinical_assertion_config_from_manifest)
    cache_dir: str | None = None
    _pipeline: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
        """Load assertion classifier artifacts from disk only."""
        if not self.config.enabled:
            return
        artifact_path = self.config.artifact_path
        if not artifact_path or not Path(artifact_path).exists():
            self.loaded = False
            return
        try:
            from transformers import (  # type: ignore
                AutoModelForSequenceClassification,
                AutoTokenizer,
                pipeline,
            )
        except Exception:  # noqa: BLE001 - optional dependency may be absent
            self.loaded = False
            return

        try:
            tokenizer = AutoTokenizer.from_pretrained(
                artifact_path,
                revision=self.config.model_revision if self.config.model_revision != "TBD" else None,
                cache_dir=self.cache_dir,
                local_files_only=True,
            )
            model = AutoModelForSequenceClassification.from_pretrained(
                artifact_path,
                revision=self.config.model_revision if self.config.model_revision != "TBD" else None,
                cache_dir=self.cache_dir,
                local_files_only=True,
            )
            device = -1 if self.config.device == "cpu" else 0
            self._pipeline = pipeline(
                "text-classification",
                model=model,
                tokenizer=tokenizer,
                device=device,
                truncation=True,
            )
            self.loaded = True
        except Exception:  # noqa: BLE001 - malformed/missing local artifact
            self.loaded = False

    def classify(self, text: str, mention: MentionSpan) -> AssertionResult:
        started = time.perf_counter()
        marked_text = mark_entity(text, mention)
        status = self.availability_status(marked_text)
        if status == "unavailable" and self.config.enabled and not self.loaded:
            self.load()
            status = self.availability_status(marked_text)
        if status != "success":
            return self._result(mention.mention_id, "unknown", 0.0, status, started)
        if not self.loaded:
            self.load()
        status = self.availability_status(marked_text)
        if status != "success":
            return self._result(mention.mention_id, "unknown", 0.0, status, started)

        try:
            output = self._with_timeout(lambda: self._classify_loaded(marked_text))
        except concurrent.futures.TimeoutError:
            return self._result(mention.mention_id, "unknown", 0.0, "timeout", started)
        except Exception:  # noqa: BLE001 - bad model output must not escape triage
            return self._result(mention.mention_id, "unknown", 0.0, "invalid_output", started)
        return self._result_from_output(mention.mention_id, output, started)

    def _classify_loaded(self, marked_text: str) -> dict:
        if self._pipeline is None:
            raise RuntimeError("clinical assertion model is not loaded")
        output = self._pipeline(marked_text)
        if isinstance(output, list) and output and isinstance(output[0], dict):
            return output[0]
        if isinstance(output, dict):
            return output
        raise RuntimeError("clinical assertion output must be a dict or non-empty list")

    def _with_timeout(self, func) -> dict:
        return run_with_timeout(func, self.config.timeout_ms)

    def _result_from_output(self, mention_id: str, output: dict, started: float) -> AssertionResult:
        label = str(output.get("label") or output.get("entity") or "").strip().casefold()
        assertion = ASSERTION_LABEL_MAP.get(label)
        score = output.get("score", 0.0)
        if assertion is None or not isinstance(score, (int, float)):
            return self._result(mention_id, "unknown", 0.0, "invalid_output", started)
        return self._result(mention_id, assertion, float(score), "success", started)

    def _result(
        self,
        mention_id: str,
        assertion: str,
        confidence: float,
        status: str,
        started: float,
    ) -> AssertionResult:
        return AssertionResult(
            mention_id=mention_id,
            assertion=assertion,
            confidence=confidence,
            model_name=self.model_name,
            model_revision=self.model_revision,
            status=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def mark_entity(text: str, mention: MentionSpan) -> str:
    """Wrap one mention with assertion-model entity markers."""
    if text[mention.start:mention.end] != mention.text:
        raise ValueError("mention offsets do not match text")
    return (
        text[:mention.start]
        + ENTITY_START_MARKER
        + mention.text
        + ENTITY_END_MARKER
        + text[mention.end:]
    )
