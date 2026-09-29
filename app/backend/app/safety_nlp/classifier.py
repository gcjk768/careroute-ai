"""Future Safety NLP classifier contract.

Phase 4 deliberately keeps the unfine-tuned biomedical encoder disabled as a
classifier. This module records the training contract a future fine-tuned
multi-label classifier must satisfy before it can replace this disabled stub.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..agents.safety import SEMANTIC_CATEGORIES
from .contracts import (
    AdapterConfig,
    CategoryCandidate,
    MentionSpan,
    OptionalModelAdapter,
)

BIO_CLINICALBERT_MODEL_NAME = "emilyalsentzer/Bio_ClinicalBERT"
BIO_CLINICALBERT_CLASSIFIER_STATUS = "disabled_until_fine_tuned"


@dataclass(frozen=True)
class BioClinicalBertTrainingContract:
    """Minimum contract for a future multi-label red-flag classifier."""

    base_model: str = BIO_CLINICALBERT_MODEL_NAME
    output_labels: tuple[str, ...] = SEMANTIC_CATEGORIES
    input_fields: tuple[str, ...] = (
        "original_text",
        "working_text",
        "mention_text",
        "assertion",
        "subject",
        "temporality",
    )
    required_metrics: tuple[str, ...] = (
        "per_label_f1",
        "macro_f1",
        "calibration_error",
        "abstention_rate",
        "per_language_recall",
        "specificity",
    )
    required_artifacts: tuple[str, ...] = (
        "training_dataset_hash",
        "holdout_dataset_hash",
        "model_revision",
        "tokenizer_revision",
        "artifact_sha256",
        "threshold_report",
    )
    disabled_reason: str = (
        "The base Bio_ClinicalBERT encoder is not a red-flag classifier until "
        "fine-tuned and benchmarked on the Safety NLP label contract."
    )


def bio_clinicalbert_config_from_manifest(
    manifest_entry: dict | None = None,
    *,
    enabled: bool = False,
    timeout_ms: int = 1000,
    device: str = "cpu",
    max_input_chars: int = 2000,
) -> AdapterConfig:
    """Build a disabled-by-default config for the future classifier."""
    entry = manifest_entry or {}
    return AdapterConfig(
        model_name=str(entry.get("model") or BIO_CLINICALBERT_MODEL_NAME),
        model_revision=str(entry.get("revision") or "unfine-tuned-base-disabled"),
        enabled=enabled,
        requires_artifact=True,
        artifact_path=entry.get("artifact_path"),
        timeout_ms=timeout_ms,
        device=device,
        max_input_chars=max_input_chars,
    )


@dataclass
class DisabledBioClinicalBertClassifier(OptionalModelAdapter):
    """Disabled classifier stub used to prevent accidental base-encoder use."""

    config: AdapterConfig = field(default_factory=bio_clinicalbert_config_from_manifest)
    contract: BioClinicalBertTrainingContract = field(default_factory=BioClinicalBertTrainingContract)

    def __post_init__(self) -> None:
        self.loaded = False

    def rank(self, text: str, mention: MentionSpan) -> list[CategoryCandidate]:
        status = self.availability_status(text)
        if status == "success":
            status = "disabled"
        return [
            CategoryCandidate(
                mention_id=mention.mention_id,
                category=None,
                confidence=0.0,
                model_name=self.model_name,
                model_revision=self.model_revision,
                status=status,
                metadata={
                    "classifier_status": BIO_CLINICALBERT_CLASSIFIER_STATUS,
                    "disabled_reason": self.contract.disabled_reason,
                    "required_metrics": list(self.contract.required_metrics),
                    "required_artifacts": list(self.contract.required_artifacts),
                },
            )
        ]

