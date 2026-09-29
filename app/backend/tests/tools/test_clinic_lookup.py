"""Opt-in live CHAS directory smoke test."""
import os

import pytest
from app.tools.clinic_lookup import ClinicLookupTool

pytestmark = pytest.mark.integration


def test_live_chas_dataset_contains_clinics():
    if os.environ.get("RUN_LIVE_TOOL_TESTS") != "1":
        pytest.skip("set RUN_LIVE_TOOL_TESTS=1 to download the live CHAS directory")

    clinics = ClinicLookupTool().fetch_dataset()
    assert clinics
