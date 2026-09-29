"""Symptom extraction adapters for the Safety NLP shadow pipeline."""
from __future__ import annotations

import concurrent.futures
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import AdapterConfig, MentionSpan, OptionalModelAdapter, run_with_timeout

BIOMEDICAL_NER_MODEL_NAME = "d4data/biomedical-ner-all"
BIOMEDICAL_NER_LICENSE = "Apache-2.0"
BIOMEDICAL_NER_ENTITY_TYPES = frozenset({
    "sign_symptom",
    "signs_symptoms",
    "symptom",
    "symptoms",
})
SPACY_MENTION_ID_ATTR = "safety_mention_id"
SPACY_MENTION_METADATA_ATTR = "safety_mention"


def biomedical_ner_config_from_manifest(
    manifest_entry: dict[str, Any] | None = None,
    *,
    enabled: bool = False,
    timeout_ms: int = 1000,
    device: str = "cpu",
    max_input_chars: int = 2000,
) -> AdapterConfig:
    """Build a local-files-only biomedical NER adapter config."""
    entry = manifest_entry or {}
    return AdapterConfig(
        model_name=str(entry.get("model") or BIOMEDICAL_NER_MODEL_NAME),
        model_revision=str(entry.get("revision") or "TBD"),
        enabled=enabled,
        requires_artifact=True,
        artifact_path=entry.get("artifact_path"),
        timeout_ms=timeout_ms,
        device=device,
        max_input_chars=max_input_chars,
    )


@dataclass
class BiomedicalNerAdapter(OptionalModelAdapter):
    """Local-files-only adapter for `d4data/biomedical-ner-all`.

    Missing dependencies, missing artifacts, timeouts and malformed model output
    produce no mentions. The pipeline therefore keeps deterministic Safety as
    the floor and never raises through triage because NER is unavailable.
    """

    config: AdapterConfig = field(default_factory=biomedical_ner_config_from_manifest)
    cache_dir: str | None = None
    _pipeline: Any = field(default=None, init=False, repr=False)
    last_status: str = field(default="unavailable", init=False)

    def __post_init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
        """Load the token-classification pipeline from local artifacts only."""
        if not self.config.enabled:
            return
        artifact_path = self.config.artifact_path
        if not artifact_path or not Path(artifact_path).exists():
            self.loaded = False
            return
        try:
            from transformers import (  # type: ignore
                AutoModelForTokenClassification,
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
            model = AutoModelForTokenClassification.from_pretrained(
                artifact_path,
                revision=self.config.model_revision if self.config.model_revision != "TBD" else None,
                cache_dir=self.cache_dir,
                local_files_only=True,
            )
            device = -1 if self.config.device == "cpu" else 0
            self._pipeline = pipeline(
                "token-classification",
                model=model,
                tokenizer=tokenizer,
                aggregation_strategy="simple",
                device=device,
            )
            self.loaded = True
        except Exception:  # noqa: BLE001 - malformed/missing local artifact
            self.loaded = False

    def extract(self, text: str, language: str) -> list[MentionSpan]:
        started = time.perf_counter()
        status = self.availability_status(text)
        if status == "unavailable" and self.config.enabled and not self.loaded:
            self.load()
            status = self.availability_status(text)
        if status != "success":
            self.last_status = status
            return []
        if not self.loaded:
            self.load()
        status = self.availability_status(text)
        if status != "success":
            self.last_status = status
            return []

        try:
            entities = self._with_timeout(lambda: self._extract_loaded(text))
        except concurrent.futures.TimeoutError:
            self.last_status = "timeout"
            return []
        except Exception:  # noqa: BLE001 - bad model output must not escape triage
            self.last_status = "invalid_output"
            return []
        self.last_status = "success"
        return self._mentions_from_entities(text, language, entities, started)

    def _extract_loaded(self, text: str) -> list[dict]:
        if self._pipeline is None:
            raise RuntimeError("biomedical NER model is not loaded")
        output = self._pipeline(text)
        if not isinstance(output, list):
            raise TypeError("biomedical NER output must be a list")
        return output

    def _with_timeout(self, func) -> list[dict]:
        return run_with_timeout(func, self.config.timeout_ms)

    def _mentions_from_entities(
        self,
        text: str,
        language: str,
        entities: list[dict],
        started: float,
    ) -> list[MentionSpan]:
        mentions: list[MentionSpan] = []
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            if not _is_symptom_entity(entity):
                continue
            start = entity.get("start")
            end = entity.get("end")
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            if start < 0 or end <= start or end > len(text):
                continue
            mention_text = text[start:end]
            try:
                mentions.append(
                    MentionSpan(
                        mention_id=f"ner-{len(mentions) + 1}",
                        text=mention_text,
                        start=start,
                        end=end,
                        entity_type=str(entity.get("entity_group") or entity.get("entity") or "Sign_symptom"),
                        source_language=language,
                        model_name=self.model_name,
                        model_revision=self.model_revision,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                )
            except ValueError:
                continue
        return mentions


def _is_symptom_entity(entity: dict) -> bool:
    label = str(entity.get("entity_group") or entity.get("entity") or "").strip().casefold()
    normalized = label.replace("-", "_").replace(" ", "_")
    return normalized in BIOMEDICAL_NER_ENTITY_TYPES


def bridge_mentions_to_spacy_doc(doc: Any, mentions: Sequence[MentionSpan]) -> Any:
    """Attach transformer mention spans to a spaCy-like `Doc`.

    This function avoids importing spaCy at module import time. With real spaCy
    it registers span extensions lazily; in tests it works with a small fake Doc
    that implements `char_span` and `ents`.
    """
    _ensure_spacy_span_extensions()
    existing = list(getattr(doc, "ents", ()))
    bridged = []
    for mention in mentions:
        span = doc.char_span(
            mention.start,
            mention.end,
            label=mention.entity_type,
            alignment_mode="expand",
        )
        if span is None:
            continue
        _set_span_metadata(span, mention)
        bridged.append(span)
    doc.ents = tuple(existing + bridged)
    return doc


def _ensure_spacy_span_extensions() -> None:
    try:
        from spacy.tokens import Span  # type: ignore
    except Exception:  # noqa: BLE001 - spaCy is optional in normal tests
        return
    for attr in (SPACY_MENTION_ID_ATTR, SPACY_MENTION_METADATA_ATTR):
        if not Span.has_extension(attr):
            Span.set_extension(attr, default=None)


def _set_span_metadata(span: Any, mention: MentionSpan) -> None:
    metadata = {
        "mention_id": mention.mention_id,
        "source_language": mention.source_language,
        "model_name": mention.model_name,
        "model_revision": mention.model_revision,
        "status": mention.status,
    }
    extension_proxy = getattr(span, "_", None)
    if extension_proxy is not None:
        try:
            setattr(extension_proxy, SPACY_MENTION_ID_ATTR, mention.mention_id)
            setattr(extension_proxy, SPACY_MENTION_METADATA_ATTR, metadata)
            return
        except Exception:  # noqa: BLE001,S110 - fake spans may expose `_` differently; falls through to setattr below  # nosec B110
            pass
    setattr(span, SPACY_MENTION_ID_ATTR, mention.mention_id)
    setattr(span, SPACY_MENTION_METADATA_ATTR, metadata)
