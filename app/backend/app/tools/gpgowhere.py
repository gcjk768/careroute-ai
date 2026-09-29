"""Versioned GoWhere operating-hours snapshot.

GoWhere's public directory is JavaScript-rendered and does not publish a stable
backend API.  Routing therefore reads a reviewed JSON snapshot rather than
scraping a patient-facing website during triage.  Refresh the file with a
reviewed export and update its ``retrieved_at`` value.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("careroute.tools.gpgowhere")

SINGAPORE = ZoneInfo("Asia/Singapore")
SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "data" / "gpgowhere_hours.json"


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _parse_clock(value: str) -> time:
    # Opening hours are local wall-clock times, not instants: a naive
    # `time` is the correct type here, so DTZ007 does not apply. Exported
    # snapshots write a window that ends at midnight as "24:00".
    if value.strip() == "24:00":
        return time(23, 59, 59, 999999)
    return datetime.strptime(value.strip(), "%H:%M").time()  # noqa: DTZ007


@dataclass(frozen=True)
class HoursProfile:
    postal: str
    name: str
    hours: dict[str, list[list[str]]]
    after_hours: bool = False
    twenty_four_hours: bool = False
    source: str = "GoWhere snapshot"
    verified_at: str = ""

    def is_open(self, at: datetime | None = None) -> bool:
        """Open at `at` (Singapore time). A window whose end is not after its
        start runs overnight ("22:00"-"02:00"): it is open after the start on
        its own day and before the end on the following day. A window the
        parser cannot read counts as closed, never as an error."""
        at = at.astimezone(SINGAPORE) if at else datetime.now(SINGAPORE)
        if self.twenty_four_hours:
            return True
        now = at.time()
        today = at.strftime("%a").lower()
        yesterday = (at - timedelta(days=1)).strftime("%a").lower()
        for start, end in self.hours.get(today, []):
            try:
                opens, closes = _parse_clock(start), _parse_clock(end)
            except (ValueError, TypeError, AttributeError):
                continue
            if opens < closes:
                if opens <= now < closes:
                    return True
            elif now >= opens:  # overnight window, the evening side
                return True
        for start, end in self.hours.get(yesterday, []):
            try:
                opens, closes = _parse_clock(start), _parse_clock(end)
            except (ValueError, TypeError, AttributeError):
                continue
            if opens >= closes and now < closes:  # overnight window, the morning side
                return True
        return False


class GPGoWhereHoursDirectory:
    """Loads and matches the reviewed hours snapshot by postal code and name."""

    def __init__(self, path: Path = SNAPSHOT_PATH) -> None:
        self.path = path
        self._profiles: list[HoursProfile] | None = None

    def profiles(self) -> list[HoursProfile]:
        if self._profiles is None:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                # An empty directory is NOT the same as "every clinic is open",
                # but that is exactly how it behaves downstream: match() returns
                # None for every lookup and the closed-clinic filter silently
                # stops filtering. This was the container's actual behaviour —
                # backend/.dockerignore excluded data/, so the snapshot was not
                # in the image at all and nothing said so. Say so.
                logger.warning(
                    "GoWhere hours snapshot missing at %s — the closed-clinic "
                    "filter is DISABLED and routing may return shut clinics",
                    self.path,
                )
                raw = {"clinics": []}
            self._profiles = [HoursProfile(**item) for item in raw.get("clinics", [])]
        return self._profiles

    def match(self, *, postal: str, name: str) -> HoursProfile | None:
        postal_matches = [p for p in self.profiles() if p.postal == postal]
        if not postal_matches:
            return None
        wanted = _normalise(name)
        return next((p for p in postal_matches if _normalise(p.name) == wanted), None)
