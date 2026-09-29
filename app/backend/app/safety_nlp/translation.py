"""Translation adapters for the Safety NLP shadow pipeline.

The real adapters in this module are optional and lazy. Importing this file must
not require transformers, PyTorch or local model artifacts; those are loaded only
when an enabled adapter is explicitly used.
"""
from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import AdapterConfig, OptionalModelAdapter, TranslationResult, run_with_timeout

NLLB_MODEL_NAME = "facebook/nllb-200-distilled-600M"
NLLB_LICENSE = "CC-BY-NC-4.0"
NLLB_TARGET_LANGUAGE = "eng_Latn"
MADLAD_MODEL_NAME = "google/madlad400-3b-mt"
MADLAD_LICENSE = "Apache-2.0"
MADLAD_TARGET_PREFIX = "<2en>"

NLLB_SOURCE_LANGUAGES: dict[str, str] = {
    "zh": "zho_Hans",
    "zh-cn": "zho_Hans",
    "zh-hans": "zho_Hans",
    "cmn": "zho_Hans",
    "ms": "zsm_Latn",
    "msa": "zsm_Latn",
    "ta": "tam_Taml",
    "tam": "tam_Taml",
}


def nllb_config_from_manifest(
    manifest_entry: dict[str, Any] | None = None,
    *,
    enabled: bool = False,
    timeout_ms: int = 1000,
    device: str = "cpu",
    max_input_chars: int = 2000,
) -> AdapterConfig:
    """Build an adapter config from a Safety model manifest entry.

    The manifest may point at a local artifact directory. The adapter never uses
    this value to download files at request time.
    """
    entry = manifest_entry or {}
    return AdapterConfig(
        model_name=str(entry.get("model") or NLLB_MODEL_NAME),
        model_revision=str(entry.get("revision") or "TBD"),
        enabled=enabled,
        requires_artifact=True,
        artifact_path=entry.get("artifact_path"),
        timeout_ms=timeout_ms,
        device=device,
        max_input_chars=max_input_chars,
    )


def madlad_config_from_manifest(
    manifest_entry: dict[str, Any] | None = None,
    *,
    enabled: bool = False,
    timeout_ms: int = 1000,
    device: str = "cpu",
    max_input_chars: int = 2000,
) -> AdapterConfig:
    """Build a MADLAD adapter config from a Safety model manifest entry."""
    entry = manifest_entry or {}
    return AdapterConfig(
        model_name=str(entry.get("model") or MADLAD_MODEL_NAME),
        model_revision=str(entry.get("revision") or "TBD"),
        enabled=enabled,
        requires_artifact=True,
        artifact_path=entry.get("artifact_path"),
        timeout_ms=timeout_ms,
        device=device,
        max_input_chars=max_input_chars,
    )


@dataclass
class NllbTranslationAdapter(OptionalModelAdapter):
    """Local-files-only NLLB translation adapter for shadow mode.

    The adapter keeps the original text intact and returns an English working
    copy only when the local NLLB artifact is present and inference succeeds.
    """

    config: AdapterConfig = field(default_factory=nllb_config_from_manifest)
    cache_dir: str | None = None
    _tokenizer: Any = field(default=None, init=False, repr=False)
    _model: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
        """Load NLLB from local artifacts only.

        Missing optional dependencies or artifacts leave `loaded=False`; the
        request path will report `unavailable` instead of raising through triage.
        """
        if not self.config.enabled:
            return
        artifact_path = self.config.artifact_path
        if not artifact_path or not Path(artifact_path).exists():
            self.loaded = False
            return
        try:
            from transformers import (  # type: ignore
                AutoModelForSeq2SeqLM,
                AutoTokenizer,
            )
        except Exception:  # noqa: BLE001 - optional dependency may be absent
            self.loaded = False
            return

        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                artifact_path,
                revision=self.config.model_revision if self.config.model_revision != "TBD" else None,
                cache_dir=self.cache_dir,
                local_files_only=True,
            )
            self._model = AutoModelForSeq2SeqLM.from_pretrained(
                artifact_path,
                revision=self.config.model_revision if self.config.model_revision != "TBD" else None,
                cache_dir=self.cache_dir,
                local_files_only=True,
            )
            if self.config.device != "cpu" and hasattr(self._model, "to"):
                self._model.to(self.config.device)
            self.loaded = True
        except Exception:  # noqa: BLE001 - malformed/missing local artifact
            self.loaded = False

    def translate(self, text: str, language: str) -> TranslationResult:
        started = time.perf_counter()
        status = self.availability_status(text)
        if status == "unavailable" and self.config.enabled and not self.loaded:
            self.load()
            status = self.availability_status(text)
        if status != "success":
            return self._result(text, language, "", status, started)

        source_lang = NLLB_SOURCE_LANGUAGES.get(language.strip().lower())
        if source_lang is None:
            return self._result(text, language, "", "invalid_output", started)

        if not self.loaded:
            self.load()
        status = self.availability_status(text)
        if status != "success":
            return self._result(text, language, "", status, started)

        try:
            translated = self._with_timeout(lambda: self._translate_loaded(text, source_lang))
        except concurrent.futures.TimeoutError:
            return self._result(text, language, "", "timeout", started)
        except Exception:  # noqa: BLE001 - bad model output must not escape triage
            return self._result(text, language, "", "invalid_output", started)

        if not translated or not isinstance(translated, str):
            return self._result(text, language, "", "invalid_output", started)
        return self._result(text, language, translated.strip(), "success", started)

    def _translate_loaded(self, text: str, source_lang: str) -> str:
        if self._tokenizer is None or self._model is None:
            raise RuntimeError("NLLB model is not loaded")
        self._tokenizer.src_lang = source_lang
        inputs = self._tokenizer(text, return_tensors="pt", truncation=True)
        if self.config.device != "cpu" and hasattr(inputs, "to"):
            inputs = inputs.to(self.config.device)
        forced_bos_token_id = self._tokenizer.convert_tokens_to_ids(NLLB_TARGET_LANGUAGE)
        generated = self._model.generate(**inputs, forced_bos_token_id=forced_bos_token_id)
        decoded = self._tokenizer.batch_decode(generated, skip_special_tokens=True)
        return str(decoded[0]) if decoded else ""

    def _with_timeout(self, func) -> str:
        return run_with_timeout(func, self.config.timeout_ms)

    def _result(
        self,
        text: str,
        language: str,
        translated_text: str,
        status: str,
        started: float,
    ) -> TranslationResult:
        return TranslationResult(
            original_text=text,
            language=language,
            translated_text=translated_text,
            model_name=self.model_name,
            model_revision=self.model_revision,
            status=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


@dataclass
class MadladTranslationAdapter(OptionalModelAdapter):
    """Local-files-only MADLAD translation adapter for shadow benchmarking.

    MADLAD is intentionally separate from NLLB so benchmark jobs can load one
    candidate at a time and avoid inflating the serving container unnecessarily.
    """

    config: AdapterConfig = field(default_factory=madlad_config_from_manifest)
    cache_dir: str | None = None
    _tokenizer: Any = field(default=None, init=False, repr=False)
    _model: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
        """Load MADLAD from local artifacts only."""
        if not self.config.enabled:
            return
        artifact_path = self.config.artifact_path
        if not artifact_path or not Path(artifact_path).exists():
            self.loaded = False
            return
        try:
            from transformers import (  # type: ignore
                AutoModelForSeq2SeqLM,
                AutoTokenizer,
            )
        except Exception:  # noqa: BLE001 - optional dependency may be absent
            self.loaded = False
            return

        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                artifact_path,
                revision=self.config.model_revision if self.config.model_revision != "TBD" else None,
                cache_dir=self.cache_dir,
                local_files_only=True,
            )
            self._model = AutoModelForSeq2SeqLM.from_pretrained(
                artifact_path,
                revision=self.config.model_revision if self.config.model_revision != "TBD" else None,
                cache_dir=self.cache_dir,
                local_files_only=True,
            )
            if self.config.device != "cpu" and hasattr(self._model, "to"):
                self._model.to(self.config.device)
            self.loaded = True
        except Exception:  # noqa: BLE001 - malformed/missing local artifact
            self.loaded = False

    def translate(self, text: str, language: str) -> TranslationResult:
        started = time.perf_counter()
        status = self.availability_status(text)
        if status == "unavailable" and self.config.enabled and not self.loaded:
            self.load()
            status = self.availability_status(text)
        if status != "success":
            return self._result(text, language, "", status, started)

        if not language.strip():
            return self._result(text, language, "", "invalid_output", started)
        if not self.loaded:
            self.load()
        status = self.availability_status(text)
        if status != "success":
            return self._result(text, language, "", status, started)

        try:
            translated = self._with_timeout(lambda: self._translate_loaded(text))
        except concurrent.futures.TimeoutError:
            return self._result(text, language, "", "timeout", started)
        except Exception:  # noqa: BLE001 - bad model output must not escape triage
            return self._result(text, language, "", "invalid_output", started)

        if not translated or not isinstance(translated, str):
            return self._result(text, language, "", "invalid_output", started)
        return self._result(text, language, translated.strip(), "success", started)

    def _translate_loaded(self, text: str) -> str:
        if self._tokenizer is None or self._model is None:
            raise RuntimeError("MADLAD model is not loaded")
        inputs = self._tokenizer(f"{MADLAD_TARGET_PREFIX} {text}", return_tensors="pt", truncation=True)
        if self.config.device != "cpu" and hasattr(inputs, "to"):
            inputs = inputs.to(self.config.device)
        generated = self._model.generate(**inputs)
        decoded = self._tokenizer.batch_decode(generated, skip_special_tokens=True)
        return str(decoded[0]) if decoded else ""

    def _with_timeout(self, func) -> str:
        return run_with_timeout(func, self.config.timeout_ms)

    def _result(
        self,
        text: str,
        language: str,
        translated_text: str,
        status: str,
        started: float,
    ) -> TranslationResult:
        return TranslationResult(
            original_text=text,
            language=language,
            translated_text=translated_text,
            model_name=self.model_name,
            model_revision=self.model_revision,
            status=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
