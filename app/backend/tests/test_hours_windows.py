"""Opening-hours windows that real snapshots contain and the parser rejected."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.tools.gpgowhere import HoursProfile

_SG = ZoneInfo("Asia/Singapore")


def _at(hour: int, minute: int = 0, day: int = 22) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=_SG)  # 22 Sep 2026 is a Tuesday


def test_a_window_ending_at_midnight_is_written_as_24_00():
    profile = HoursProfile(postal="1", name="x", hours={"tue": [["09:00", "24:00"]]})
    assert profile.is_open(_at(23, 30))
    assert not profile.is_open(_at(8, 0))


def test_an_overnight_window_is_open_on_both_sides_of_midnight():
    profile = HoursProfile(postal="1", name="x", hours={"tue": [["22:00", "02:00"]]})
    assert profile.is_open(_at(23, 0))
    assert profile.is_open(_at(1, 0, day=23))   # early Wednesday, still Tuesday's window
    assert not profile.is_open(_at(12, 0))


def test_a_malformed_window_is_treated_as_closed_not_as_an_error():
    profile = HoursProfile(postal="1", name="x", hours={"tue": [["9am", "5pm"]]})
    assert profile.is_open(_at(10, 0)) is False
