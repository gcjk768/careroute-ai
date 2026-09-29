"""Selected Safety NLP runtime loading helpers.

Benchmark modules may construct many candidate adapters. The live/prototype
runtime must do the opposite: resolve one selected manifest entry, load only
that adapter, and keep all Hugging Face access local-files-only.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .assertion import ClinicalAssertionAdapter, clinical_assertion_config_from_manifest
from .context import CareRouteContextRuleAdapter
from .contracts import AdapterConfig, CategoryCandidate, MentionSpan, SafetyNlpAdapters, TranslationResult
from .manifest import SafetyModelManifestError, load_safety_model_manifest, validate_safety_model_manifest
from .ner import BiomedicalNerAdapter, biomedical_ner_config_from_manifest
from .similarity import (
    BioLordSimilarityAdapter,
    MDebertaNliAdapter,
    biolord_config_from_manifest,
    mdeberta_nli_config_from_manifest,
)
from .translation import (
    MadladTranslationAdapter,
    NllbTranslationAdapter,
    madlad_config_from_manifest,
    nllb_config_from_manifest,
)

SELECTED_PROTOTYPE_CATEGORY_MODEL = "biolord"
BENCHMARK_COMPARATOR_MODELS = ("labse", "mdebertaNli")

CategoryAdapterFactory = Callable[[AdapterConfig, str | None], Any]
AdapterFactory = Callable[[AdapterConfig, str | None], Any]


@dataclass
class RuntimeTranslationAdapter:
    """Pass English through and delegate configured non-English translation."""

    delegate: Any | None = None

    @property
    def model_name(self) -> str:
        return getattr(self.delegate, "model_name", "english-passthrough")

    @property
    def model_revision(self) -> str:
        return getattr(self.delegate, "model_revision", "local-rule-v1")

    def load(self) -> None:
        if self.delegate is not None and hasattr(self.delegate, "load"):
            self.delegate.load()

    def translate(self, text: str, language: str) -> TranslationResult:
        if language.strip().casefold() in {"en", "eng", "en-us", "en-gb"}:
            return TranslationResult(
                original_text=text,
                language=language,
                translated_text=text,
                model_name="english-passthrough",
                model_revision="local-rule-v1",
            )
        if self.delegate is not None:
            return self.delegate.translate(text, language)
        return TranslationResult(
            original_text=text,
            language=language,
            translated_text="",
            model_name="safety-translation-router",
            model_revision="local-rule-v1",
            status="unavailable",
        )


@dataclass(frozen=True)
class SelectedPrototypeModelSet:
    """The minimum model set selected for prototype runtime use."""

    category_model_key: str
    category_model: str
    category_revision: str
    category_artifact_sha256: str
    category_artifact_path: str
    benchmark_comparators: tuple[str, ...] = BENCHMARK_COMPARATOR_MODELS
    semantic_activation_default: bool = False


def select_minimum_prototype_model_set(manifest: Mapping[str, Any]) -> SelectedPrototypeModelSet:
    """Select BioLORD as the only runtime category model from the manifest."""
    similarity = manifest.get("similarity")
    if not isinstance(similarity, Mapping):
        raise SafetyModelManifestError("Safety manifest must include a similarity section")
    entry = similarity.get(SELECTED_PROTOTYPE_CATEGORY_MODEL)
    if not isinstance(entry, Mapping):
        raise SafetyModelManifestError("Safety manifest must include similarity.biolord")

    required = {
        "model": entry.get("model"),
        "revision": entry.get("revision"),
        "artifact_path": entry.get("artifact_path"),
        "artifactSha256": entry.get("artifactSha256"),
    }
    missing = [key for key, value in required.items() if not value or str(value) == "TBD"]
    if missing:
        raise SafetyModelManifestError(f"selected BioLORD manifest entry is missing {missing!r}")

    return SelectedPrototypeModelSet(
        category_model_key=SELECTED_PROTOTYPE_CATEGORY_MODEL,
        category_model=str(required["model"]),
        category_revision=str(required["revision"]),
        category_artifact_sha256=str(required["artifactSha256"]),
        category_artifact_path=str(required["artifact_path"]),
    )


@dataclass
class SafetyNlpRuntime:
    """Live shadow runtime built entirely from pinned local-manifest adapters."""

    selected: SelectedPrototypeModelSet
    adapters: SafetyNlpAdapters
    manifest_path: Path | None = None
    warmed: bool = False
    loaded_model_keys: list[str] = field(default_factory=list)
    stage_statuses: dict[str, str] = field(default_factory=dict)

    @property
    def category_adapter(self) -> Any:
        """Compatibility accessor used by Phase 4 benchmark tests."""
        return self.adapters.category

    @classmethod
    def from_manifest_file(
        cls,
        manifest_path: str | Path,
        *,
        enabled: bool = False,
        timeout_ms: int = 1000,
        device: str = "cpu",
        max_input_chars: int = 2000,
        cache_dir: str | None = None,
        project_root: str | Path | None = None,
        category_factory: CategoryAdapterFactory | None = None,
        ner_factory: AdapterFactory | None = None,
        assertion_factory: AdapterFactory | None = None,
        nllb_factory: AdapterFactory | None = None,
        madlad_factory: AdapterFactory | None = None,
        direct_nli_factory: AdapterFactory | None = None,
        nllb_enabled: bool = False,
        madlad_enabled: bool = False,
        direct_nli_enabled: bool = False,
    ) -> SafetyNlpRuntime:
        manifest_file = Path(manifest_path)
        manifest = load_safety_model_manifest(manifest_file)
        validate_safety_model_manifest(manifest)
        if manifest.get("runtimeDownloadsAllowed") is not False:
            raise SafetyModelManifestError("runtimeDownloadsAllowed must be false for Safety NLP runtime")

        selected = select_minimum_prototype_model_set(manifest)
        root = Path(project_root) if project_root is not None else Path.cwd()
        category_entry = _resolved_entry(
            manifest, ("similarity", SELECTED_PROTOTYPE_CATEGORY_MODEL), root
        )
        category_config = biolord_config_from_manifest(
            category_entry,
            enabled=enabled,
            timeout_ms=timeout_ms,
            device=device,
            max_input_chars=max_input_chars,
        )
        ner_entry = _resolved_entry(manifest, ("ner", "biomedical"), root)
        ner_config = biomedical_ner_config_from_manifest(
            ner_entry,
            enabled=enabled,
            timeout_ms=timeout_ms,
            device=device,
            max_input_chars=max_input_chars,
        )
        assertion_entry = _resolved_entry(manifest, ("assertion", "clinical"), root)
        assertion_config = clinical_assertion_config_from_manifest(
            assertion_entry,
            enabled=enabled,
            timeout_ms=timeout_ms,
            device=device,
            max_input_chars=max_input_chars,
        )

        translation_delegate = None
        if nllb_enabled and madlad_enabled:
            raise SafetyModelManifestError("enable only one live translation adapter at a time")
        if nllb_enabled:
            translation_entry = _resolved_entry(manifest, ("translation", "nllb"), root)
            translation_config = nllb_config_from_manifest(
                translation_entry, enabled=enabled, timeout_ms=timeout_ms,
                device=device, max_input_chars=max_input_chars,
            )
            translation_delegate = (nllb_factory or _default_nllb_factory)(translation_config, cache_dir)
        elif madlad_enabled:
            translation_entry = _resolved_entry(manifest, ("translation", "madlad"), root)
            translation_config = madlad_config_from_manifest(
                translation_entry, enabled=enabled, timeout_ms=timeout_ms,
                device=device, max_input_chars=max_input_chars,
            )
            translation_delegate = (madlad_factory or _default_madlad_factory)(translation_config, cache_dir)

        comparators = ()
        if direct_nli_enabled:
            direct_nli_entry = _resolved_entry(manifest, ("similarity", "mdebertaNli"), root)
            direct_nli_config = mdeberta_nli_config_from_manifest(
                direct_nli_entry,
                enabled=enabled,
                timeout_ms=timeout_ms,
                device=device,
                max_input_chars=max_input_chars,
            )
            comparators = ((direct_nli_factory or _default_direct_nli_factory)(
                direct_nli_config, cache_dir
            ),)

        adapters = SafetyNlpAdapters(
            translation=RuntimeTranslationAdapter(translation_delegate),
            ner=(ner_factory or _default_ner_factory)(ner_config, cache_dir),
            assertion=(assertion_factory or _default_assertion_factory)(assertion_config, cache_dir),
            context=CareRouteContextRuleAdapter(),
            category=(category_factory or _default_biolord_factory)(category_config, cache_dir),
            category_comparators=comparators,
        )
        return cls(
            selected=selected,
            adapters=adapters,
            manifest_path=manifest_file,
        )

    def warmup(self) -> None:
        """Warm each selected live adapter and record typed availability."""
        stages = {
            "translation": self.adapters.translation,
            "ner": self.adapters.ner,
            "assertion": self.adapters.assertion,
            "context": self.adapters.context,
            "category": self.adapters.category,
        }
        stages.update({
            f"category_comparator_{index}": adapter
            for index, adapter in enumerate(self.adapters.category_comparators, start=1)
        })
        loaded: list[str] = []
        statuses: dict[str, str] = {}
        for stage, adapter in stages.items():
            try:
                if hasattr(adapter, "load"):
                    adapter.load()
                status = _adapter_status(adapter)
            except Exception:  # noqa: BLE001 - startup must preserve deterministic triage
                status = "unavailable"
            statuses[stage] = status
            if stage == "category" and status == "success":
                loaded.append(self.selected.category_model_key)
            elif stage in {"ner", "assertion"} and status == "success":
                loaded.append(stage)
            elif stage.startswith("category_comparator_") and status == "success":
                loaded.append("mdebertaNli")
        self.loaded_model_keys = loaded
        self.stage_statuses = statuses
        self.warmed = True

    async def awarmup(self) -> None:
        """Warm the selected adapter outside the async event loop."""
        await asyncio.to_thread(self.warmup)

    async def arank_category(self, text: str, mention: MentionSpan) -> list[CategoryCandidate]:
        """Run CPU-bound category ranking outside the async event loop."""
        return await asyncio.to_thread(self.category_adapter.rank, text, mention)


MODEL_STAGES = ("ner", "assertion", "category")


def active_backend(runtime: SafetyNlpRuntime | None) -> str:
    """"model" only when every model-backed stage loaded, else "rules".

    One failed stage makes `classify()` return an unavailable signal, so a
    partial load is reported as the deterministic rules it effectively is.
    """
    statuses = getattr(runtime, "stage_statuses", None) or {}
    return "model" if all(statuses.get(stage) == "success" for stage in MODEL_STAGES) else "rules"


def _default_biolord_factory(config: AdapterConfig, cache_dir: str | None) -> BioLordSimilarityAdapter:
    return BioLordSimilarityAdapter(config=config, cache_dir=cache_dir)


def _default_ner_factory(config: AdapterConfig, cache_dir: str | None) -> BiomedicalNerAdapter:
    return BiomedicalNerAdapter(config=config, cache_dir=cache_dir)


def _default_assertion_factory(config: AdapterConfig, cache_dir: str | None) -> ClinicalAssertionAdapter:
    return ClinicalAssertionAdapter(config=config, cache_dir=cache_dir)


def _default_nllb_factory(config: AdapterConfig, cache_dir: str | None) -> NllbTranslationAdapter:
    return NllbTranslationAdapter(config=config, cache_dir=cache_dir)


def _default_madlad_factory(config: AdapterConfig, cache_dir: str | None) -> MadladTranslationAdapter:
    return MadladTranslationAdapter(config=config, cache_dir=cache_dir)


def _default_direct_nli_factory(config: AdapterConfig, cache_dir: str | None) -> MDebertaNliAdapter:
    return MDebertaNliAdapter(config=config, cache_dir=cache_dir)


def _resolve_artifact_path(path: str, project_root: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return project_root / candidate


def _resolved_entry(manifest: Mapping[str, Any], keys: tuple[str, str], root: Path) -> dict[str, Any]:
    section = manifest.get(keys[0])
    entry = section.get(keys[1]) if isinstance(section, Mapping) else None
    resolved = dict(entry) if isinstance(entry, Mapping) else {}
    artifact_path = resolved.get("artifact_path")
    if artifact_path:
        resolved["artifact_path"] = str(_resolve_artifact_path(str(artifact_path), root))
    return resolved


def _adapter_status(adapter: Any) -> str:
    delegate = getattr(adapter, "delegate", None)
    if delegate is None and isinstance(adapter, RuntimeTranslationAdapter):
        return "success"
    target = delegate if delegate is not None else adapter
    availability = getattr(target, "availability_status", None)
    if callable(availability):
        return str(availability())
    return "success"
