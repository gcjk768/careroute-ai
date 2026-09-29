"""Safety NLP model manifest loading and release guardrails."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .translation import NLLB_LICENSE, NLLB_MODEL_NAME

PRODUCTION_ACTIVATION_MODES = frozenset({"active", "additive", "production"})


class SafetyModelManifestError(ValueError):
    """Raised when a Safety model manifest violates release guardrails."""


def load_safety_model_manifest(path: str | Path) -> dict:
    """Load a Safety model manifest from disk."""
    with Path(path).open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SafetyModelManifestError("Safety model manifest must be a JSON object")
    return data


def validate_safety_model_manifest(manifest: Mapping) -> None:
    """Validate Safety NLP release guardrails.

    Phase 4 manifests are expected to remain shadow-only. If a future manifest
    enables production/additive activation, NLLB must carry explicit license and
    governance approvals because its CC-BY-NC-4.0 license is research-only for
    this project until reviewed.
    """
    activation_mode = str(manifest.get("activationMode", "shadow")).strip().lower()
    if activation_mode not in PRODUCTION_ACTIVATION_MODES:
        return

    nllb = _nllb_entry(manifest)
    if not nllb or not _truthy(nllb.get("enabled")):
        return

    license_id = str(nllb.get("license", "")).strip()
    if license_id != NLLB_LICENSE:
        raise SafetyModelManifestError(
            f"{NLLB_MODEL_NAME} manifest entry must record license {NLLB_LICENSE}; got {license_id!r}"
        )

    approvals = nllb.get("approvals") if isinstance(nllb.get("approvals"), Mapping) else {}
    license_approved = _truthy(approvals.get("license")) or _truthy(nllb.get("productionActivationApproved"))
    governance_approved = _truthy(approvals.get("governance")) or _truthy(nllb.get("governanceApproval"))
    if not license_approved or not governance_approved:
        raise SafetyModelManifestError(
            "NLLB is CC-BY-NC-4.0/research-only in this project and cannot be production/additive "
            "activated without explicit license and governance approval."
        )


def _nllb_entry(manifest: Mapping) -> Mapping | None:
    translation = manifest.get("translation")
    if not isinstance(translation, Mapping):
        return None
    direct = translation.get("nllb")
    if isinstance(direct, Mapping):
        return direct
    for entry in translation.values():
        if isinstance(entry, Mapping) and entry.get("model") == NLLB_MODEL_NAME:
            return entry
    return None


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "approved"}
    return bool(value)
