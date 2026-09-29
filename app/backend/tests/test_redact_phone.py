"""[AI-Security] LLM02 — the phone matcher must mask numbers, not vitals.

WHY THIS EXISTS
---------------
The original ``_PHONE`` pattern was "8+ digits with any run of spaces/dashes
between them", which is not a phone number, it is *any long numeric sequence*.
Two clinically important inputs were being destroyed before the agents ever saw
them:

  * "my blood pressure readings were 140 90 110 70 today" -> the whole vitals
    run collapsed into ``[REDACTED_PHONE]``, so the Severity Classifier lost the
    hypertensive reading entirely.
  * "chest pain since 12-05-2026" -> the dashed date became ``[REDACTED_PHONE]``,
    so onset/duration (a P-code input) was lost.

And it *under*-matched a real identifier: a bare 8-digit Singapore mobile
("91234567") is only 8 characters, one short of the 9 the pattern required, so
it fell through to the MRN rule and was mislabelled in the audit trail.

The replacement recognises the actual Singapore numbering plan (mobile/landline
prefixes 3/6/8/9 + 8 digits, optional +65) plus an international form that must
carry an explicit ``+`` country prefix, with at most ONE separator between digit
groups. Vitals runs and dashed dates satisfy neither.
"""
from __future__ import annotations

import pytest

from app.redact import redact


# --------------------------------------------------------------------------
# Clinical numerics must survive redaction untouched
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "my blood pressure readings were 140 90 110 70 today",
        "chest pain since 12-05-2026",
        "temperature 38.5 for 3 days",
        "pulse 110 bp 160 100 sats 94",
        "I fell on 01-01-2026 and again on 15-02-2026",
    ],
)
def test_clinical_numerics_are_not_masked(text):
    masked, found = redact(text)
    assert masked == text, f"redaction damaged clinical content: {masked!r}"
    assert "PHONE" not in found


# --------------------------------------------------------------------------
# Real Singapore numbers must be masked, and labelled PHONE (not MRN)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, number",
    [
        ("call me on 91234567", "91234567"),
        ("call me on +65 9123 4567", "9123 4567"),
        ("reach my son at +6591234567", "91234567"),
        ("home line 6123-4567 if no answer", "6123-4567"),
        ("my number is 8123 4567", "8123 4567"),
        ("clinic hotline 31234567", "31234567"),
    ],
)
def test_singapore_numbers_are_masked_as_phone(text, number):
    masked, found = redact(text)
    assert number not in masked, masked
    assert "PHONE" in found, found
    assert "MRN" not in found, f"an 8-digit SG number must be PHONE, not MRN: {found}"


def test_international_number_with_explicit_plus_is_masked():
    masked, found = redact("ring my daughter on +44 20 7946 0958 please")
    assert "7946 0958" not in masked
    assert "PHONE" in found


# --------------------------------------------------------------------------
# The other identifier kinds keep their existing behaviour
# --------------------------------------------------------------------------
def test_nric_is_still_nric():
    masked, found = redact("patient S1234567D with fever")
    assert "S1234567D" not in masked
    assert found == ["NRIC"], found


def test_bare_long_digit_run_is_still_mrn():
    masked, found = redact("record number 1234567 in the system")
    assert "1234567" not in masked
    assert "MRN" in found


def test_email_is_still_email():
    masked, found = redact("email john@mail.com for the report")
    assert "john@mail.com" not in masked
    assert "EMAIL" in found


def test_mixed_identifiers_and_clinical_text():
    """The regression case from test_new_features, plus vitals in the same string."""
    text = (
        "I'm S1234567D, call me on +65 9123 4567 or john@mail.com — "
        "chest pain for 2 days, bp was 140 90"
    )
    masked, found = redact(text)
    assert "S1234567D" not in masked
    assert "john@mail.com" not in masked
    assert "9123 4567" not in masked
    assert "chest pain" in masked
    assert "140 90" in masked, f"vitals lost: {masked!r}"
    assert set(found) >= {"NRIC", "EMAIL", "PHONE"}
