"""Opt-in live CHAS nearest-clinic smoke test."""
import os

import pytest
from app.tools.clinic_lookup import ClinicLookupTool

pytestmark = pytest.mark.integration


def test_live_chas_lookup_returns_nearby_programme_matches():
    if os.environ.get("RUN_LIVE_TOOL_TESTS") != "1":
        pytest.skip("set RUN_LIVE_TOOL_TESTS=1 to query the live CHAS directory")

    clinics = ClinicLookupTool().find_candidate_clinics(
        latitude=1.3521,
        longitude=103.8198,
        programme="CHAS",
    )
    assert clinics
    assert all("CHAS" in clinic.programmes for clinic in clinics)
