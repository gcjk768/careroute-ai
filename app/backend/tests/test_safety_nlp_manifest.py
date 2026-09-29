"""Safety NLP manifest release-guard tests."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from app.safety_nlp import (
    NLLB_LICENSE,
    NLLB_MODEL_NAME,
    SafetyModelManifestError,
    load_safety_model_manifest,
    validate_safety_model_manifest,
)

pytestmark = [pytest.mark.safety, pytest.mark.eval]

MANIFEST = Path(__file__).parents[1] / "models" / "safety" / "manifest.json"


def _manifest(*, activation_mode: str, nllb_enabled: bool, approvals: dict | None = None) -> dict:
    return {
        "schemaVersion": 1,
        "activationMode": activation_mode,
        "translation": {
            "nllb": {
                "enabled": nllb_enabled,
                "model": NLLB_MODEL_NAME,
                "license": NLLB_LICENSE,
                "approvals": approvals or {"license": False, "governance": False},
            }
        },
    }


def test_checked_in_manifest_records_nllb_research_only_restriction():
    manifest = load_safety_model_manifest(MANIFEST)

    nllb = manifest["translation"]["nllb"]
    assert manifest["activationMode"] == "shadow"
    assert nllb["license"] == NLLB_LICENSE
    assert nllb["usage"] == "research_shadow_only"
    assert nllb["productionActivationApproved"] is False
    assert nllb["approvals"] == {"license": False, "governance": False, "clinical": False}
    validate_safety_model_manifest(manifest)


def test_shadow_manifest_allows_nllb_without_production_approvals():
    manifest = _manifest(activation_mode="shadow", nllb_enabled=True)

    validate_safety_model_manifest(manifest)


def test_production_manifest_blocks_nllb_without_license_and_governance_approval():
    manifest = _manifest(activation_mode="production", nllb_enabled=True)

    with pytest.raises(SafetyModelManifestError, match="license and governance approval"):
        validate_safety_model_manifest(manifest)


def test_additive_manifest_allows_nllb_only_when_license_and_governance_approval_are_explicit():
    manifest = _manifest(
        activation_mode="additive",
        nllb_enabled=True,
        approvals={"license": True, "governance": True},
    )

    validate_safety_model_manifest(manifest)


def test_manifest_loader_rejects_non_object_json(monkeypatch):
    def fake_open(self, encoding=None):
        return io.StringIO(json.dumps(["not", "an", "object"]))

    monkeypatch.setattr(Path, "open", fake_open)

    with pytest.raises(SafetyModelManifestError, match="JSON object"):
        load_safety_model_manifest("manifest.json")
