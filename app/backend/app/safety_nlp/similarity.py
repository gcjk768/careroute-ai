"""Medical similarity adapters for Safety NLP shadow mode."""
from __future__ import annotations

import concurrent.futures
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import (
    AdapterConfig,
    CategoryCandidate,
    MentionSpan,
    OptionalModelAdapter,
    run_with_timeout,
)

BIOLORD_MODEL_NAME = "FremyCompany/BioLORD-2023"
LABSE_MODEL_NAME = "sentence-transformers/LaBSE"
MDEBERTA_NLI_MODEL_NAME = "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli"
BIOLORD_LICENSE_REVIEW_STATUS = "pending_umls_snomed_ihtsdo_nlm_review"
BIOLORD_USAGE = "shadow_only_synthetic_examples_not_clinically_reviewed"

SYNTHETIC_REDFLAG_EXAMPLES: dict[str, tuple[str, ...]] = {
    "cardiac_chest_pain": (
        "crushing chest pain radiating to the left arm",
        "severe chest pressure with sweating",
    ),
    "breathlessness": (
        "cannot breathe",
        "severe shortness of breath",
    ),
    "anaphylaxis": (
        "throat closing after allergen exposure",
        "tongue swelling and difficulty breathing",
    ),
    "stroke_signs": (
        "face drooping with slurred speech",
        "one sided weakness and speech difficulty",
    ),
    "severe_bleeding": (
        "uncontrolled bleeding",
        "vomiting blood",
    ),
    "suicidal_ideation": (
        "I want to kill myself",
        "thoughts of ending my life",
    ),
    "seizure": (
        "seizure with loss of consciousness",
        "convulsion happening now",
    ),
}


def biolord_config_from_manifest(
    manifest_entry: dict[str, Any] | None = None,
    *,
    enabled: bool = False,
    timeout_ms: int = 1000,
    device: str = "cpu",
    max_input_chars: int = 2000,
) -> AdapterConfig:
    """Build a local-files-only BioLORD similarity adapter config."""
    entry = manifest_entry or {}
    return AdapterConfig(
        model_name=str(entry.get("model") or BIOLORD_MODEL_NAME),
        model_revision=str(entry.get("revision") or "TBD"),
        enabled=enabled,
        requires_artifact=True,
        artifact_path=entry.get("artifact_path"),
        timeout_ms=timeout_ms,
        device=device,
        max_input_chars=max_input_chars,
    )


def labse_config_from_manifest(
    manifest_entry: dict[str, Any] | None = None,
    *,
    enabled: bool = False,
    timeout_ms: int = 1000,
    device: str = "cpu",
    max_input_chars: int = 2000,
) -> AdapterConfig:
    entry = manifest_entry or {}
    return AdapterConfig(
        model_name=str(entry.get("model") or LABSE_MODEL_NAME),
        model_revision=str(entry.get("revision") or "TBD"),
        enabled=enabled,
        requires_artifact=True,
        artifact_path=entry.get("artifact_path"),
        timeout_ms=timeout_ms,
        device=device,
        max_input_chars=max_input_chars,
    )


def mdeberta_nli_config_from_manifest(
    manifest_entry: dict[str, Any] | None = None,
    *,
    enabled: bool = False,
    timeout_ms: int = 1000,
    device: str = "cpu",
    max_input_chars: int = 2000,
) -> AdapterConfig:
    entry = manifest_entry or {}
    return AdapterConfig(
        model_name=str(entry.get("model") or MDEBERTA_NLI_MODEL_NAME),
        model_revision=str(entry.get("revision") or "TBD"),
        enabled=enabled,
        requires_artifact=True,
        artifact_path=entry.get("artifact_path"),
        timeout_ms=timeout_ms,
        device=device,
        max_input_chars=max_input_chars,
    )


@dataclass
class BioLordSimilarityAdapter(OptionalModelAdapter):
    """Local-files-only BioLORD category ranker for shadow benchmarking."""

    config: AdapterConfig = field(default_factory=biolord_config_from_manifest)
    examples: dict[str, tuple[str, ...]] = field(default_factory=lambda: SYNTHETIC_REDFLAG_EXAMPLES)
    cache_dir: str | None = None
    _model: Any = field(default=None, init=False, repr=False)
    _example_vectors: dict[str, list[list[float]]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
        """Load BioLORD from local artifacts only."""
        if not self.config.enabled:
            return
        artifact_path = self.config.artifact_path
        if not artifact_path or not Path(artifact_path).exists():
            self.loaded = False
            return
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except Exception:  # noqa: BLE001 - optional dependency may be absent
            self.loaded = False
            return

        try:
            self._model = SentenceTransformer(
                artifact_path,
                cache_folder=self.cache_dir,
                device=self.config.device,
                local_files_only=True,
            )
            self._example_vectors = {
                category: self._encode(list(examples))
                for category, examples in self.examples.items()
            }
            self.loaded = True
        except Exception:  # noqa: BLE001 - malformed/missing local artifact
            self.loaded = False

    def rank(self, text: str, mention: MentionSpan) -> list[CategoryCandidate]:
        started = time.perf_counter()
        status = self.availability_status(text)
        if status == "unavailable" and self.config.enabled and not self.loaded:
            self.load()
            status = self.availability_status(text)
        if status != "success":
            return [self._candidate(mention.mention_id, None, 0.0, status, started)]
        if not self.loaded:
            self.load()
        status = self.availability_status(text)
        if status != "success":
            return [self._candidate(mention.mention_id, None, 0.0, status, started)]

        try:
            mention_vector = self._with_timeout(lambda: self._encode([mention.text])[0])
        except concurrent.futures.TimeoutError:
            return [self._candidate(mention.mention_id, None, 0.0, "timeout", started)]
        except Exception:  # noqa: BLE001 - bad model output must not escape triage
            return [self._candidate(mention.mention_id, None, 0.0, "invalid_output", started)]

        ranked = [
            self._candidate(
                mention.mention_id,
                category,
                max(_cosine_similarity(mention_vector, example_vector) for example_vector in vectors),
                "success",
                started,
            )
            for category, vectors in self._example_vectors.items()
            if vectors
        ]
        return sorted(ranked, key=lambda candidate: candidate.confidence, reverse=True)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            raise RuntimeError("BioLORD model is not loaded")
        vectors = self._model.encode(texts, convert_to_numpy=False, normalize_embeddings=True)
        return [list(vector) for vector in vectors]

    def _with_timeout(self, func) -> list[float]:
        return run_with_timeout(func, self.config.timeout_ms)

    def _candidate(
        self,
        mention_id: str,
        category: str | None,
        confidence: float,
        status: str,
        started: float,
    ) -> CategoryCandidate:
        return CategoryCandidate(
            mention_id=mention_id,
            category=category,
            confidence=max(0.0, min(1.0, float(confidence))),
            model_name=self.model_name,
            model_revision=self.model_revision,
            status=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
            metadata={
                "usage": BIOLORD_USAGE,
                "license_review_status": BIOLORD_LICENSE_REVIEW_STATUS,
            },
        )


@dataclass
class LabseSimilarityAdapter(BioLordSimilarityAdapter):
    """LaBSE general multilingual similarity baseline."""

    config: AdapterConfig = field(default_factory=labse_config_from_manifest)

    def _candidate(
        self,
        mention_id: str,
        category: str | None,
        confidence: float,
        status: str,
        started: float,
    ) -> CategoryCandidate:
        candidate = super()._candidate(mention_id, category, confidence, status, started)
        candidate.metadata.update({"usage": "shadow_benchmark_general_similarity_baseline"})
        return candidate


@dataclass
class MDebertaNliAdapter(OptionalModelAdapter):
    """Direct multilingual NLI category baseline."""

    config: AdapterConfig = field(default_factory=mdeberta_nli_config_from_manifest)
    hypotheses: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        category: tuple(f"The patient has {example}." for example in examples)
        for category, examples in SYNTHETIC_REDFLAG_EXAMPLES.items()
    })
    cache_dir: str | None = None
    _tokenizer: Any = field(default=None, init=False, repr=False)
    _model: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
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
            )
        except Exception:  # noqa: BLE001
            self.loaded = False
            return
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                artifact_path,
                revision=self.config.model_revision if self.config.model_revision != "TBD" else None,
                cache_dir=self.cache_dir,
                local_files_only=True,
            )
            self._model = AutoModelForSequenceClassification.from_pretrained(
                artifact_path,
                revision=self.config.model_revision if self.config.model_revision != "TBD" else None,
                cache_dir=self.cache_dir,
                local_files_only=True,
            )
            if self.config.device != "cpu" and hasattr(self._model, "to"):
                self._model.to(self.config.device)
            self.loaded = True
        except Exception:  # noqa: BLE001
            self.loaded = False

    def rank(self, text: str, mention: MentionSpan) -> list[CategoryCandidate]:
        started = time.perf_counter()
        status = self.availability_status(text)
        if status == "unavailable" and self.config.enabled and not self.loaded:
            self.load()
            status = self.availability_status(text)
        if status != "success":
            return [self._candidate(mention.mention_id, None, 0.0, status, started)]
        if not self.loaded:
            self.load()
        status = self.availability_status(text)
        if status != "success":
            return [self._candidate(mention.mention_id, None, 0.0, status, started)]
        try:
            scores = self._with_timeout(lambda: {
                category: max(self._score_entailment(text, hypothesis) for hypothesis in hypotheses)
                for category, hypotheses in self.hypotheses.items()
            })
        except concurrent.futures.TimeoutError:
            return [self._candidate(mention.mention_id, None, 0.0, "timeout", started)]
        except Exception:  # noqa: BLE001
            return [self._candidate(mention.mention_id, None, 0.0, "invalid_output", started)]
        return sorted(
            [
                self._candidate(mention.mention_id, category, score, "success", started)
                for category, score in scores.items()
            ],
            key=lambda candidate: candidate.confidence,
            reverse=True,
        )

    def _with_timeout(self, func) -> dict[str, float]:
        return run_with_timeout(func, self.config.timeout_ms)

    def _score_entailment(self, premise: str, hypothesis: str) -> float:
        if self._tokenizer is None or self._model is None:
            raise RuntimeError("mDeBERTa NLI model is not loaded")
        inputs = self._tokenizer(premise, hypothesis, return_tensors="pt", truncation=True)
        if self.config.device != "cpu" and hasattr(inputs, "to"):
            inputs = inputs.to(self.config.device)
        self._model.eval()
        with _torch_no_grad():
            output = self._model(**inputs)
        logits = output.logits[0]
        probs = _softmax([float(value) for value in logits.detach().cpu()])
        entailment_index = _entailment_index(getattr(self._model.config, "id2label", {}))
        return probs[entailment_index]

    def _candidate(
        self,
        mention_id: str,
        category: str | None,
        confidence: float,
        status: str,
        started: float,
    ) -> CategoryCandidate:
        return CategoryCandidate(
            mention_id=mention_id,
            category=category,
            confidence=max(0.0, min(1.0, float(confidence))),
            model_name=self.model_name,
            model_revision=self.model_revision,
            status=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
            metadata={"usage": "shadow_benchmark_direct_multilingual_nli_baseline"},
        )


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("embedding vectors must be non-empty and equal length")
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm <= 0.0 or right_norm <= 0.0:  # norms are non-negative
        return 0.0
    return numerator / (left_norm * right_norm)


def _softmax(logits: list[float]) -> list[float]:
    max_logit = max(logits)
    exps = [math.exp(value - max_logit) for value in logits]
    total = sum(exps)
    return [value / total for value in exps]


def _entailment_index(id2label: Mapping | dict) -> int:
    for index, label in id2label.items():
        if "entail" in str(label).casefold():
            return int(index)
    return len(id2label) - 1 if id2label else 2


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, traceback):
        return False


def _torch_no_grad():
    try:
        import torch  # type: ignore
    except Exception:  # noqa: BLE001
        return _NullContext()
    return torch.no_grad()
