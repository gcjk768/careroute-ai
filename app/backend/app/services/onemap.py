"""
OneMap Service

Author: Marcus Teh

Wrapper around the Singapore OneMap APIs.

Supports

- Authentication
- Token refresh
- Search API
- Routing API
- Coordinate conversion

This service is intentionally reusable so future agents
(Ambulance, Pharmacy, AED etc.) can all use it.

https://www.onemap.gov.sg/apidocs
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parents[3]
load_dotenv(ROOT_DIR / ".env")
ONEMAP_EMAIL = os.getenv("ONEMAP_EMAIL")
ONEMAP_PASSWORD = os.getenv("ONEMAP_PASSWORD")

AUTH_URL = "https://www.onemap.gov.sg/api/auth/post/getToken"

SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"

ROUTE_URL = "https://www.onemap.gov.sg/api/public/routingsvc/route"

CONVERT_URL = "https://www.onemap.gov.sg/api/common/convert/3414to4326"

# Refresh this many seconds BEFORE the token actually dies, so a call that is
# already in flight cannot be overtaken by the expiry.
TOKEN_REFRESH_BUFFER = 60

# Documented OneMap token lifetime, used only when the getToken response does
# not tell us the real expiry.
DEFAULT_TOKEN_LIFETIME = 60 * 60 * 24 * 3

SINGAPORE_TIMEZONE = ZoneInfo("Asia/Singapore")


# ---------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------


class OneMapError(Exception):
    pass


class AuthenticationError(OneMapError):
    pass


class RoutingError(OneMapError):
    pass


class SearchError(OneMapError):
    pass


# ---------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------


class OneMapClient:

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
    ):

        self.email = email or ONEMAP_EMAIL
        self.password = password or ONEMAP_PASSWORD

        if not self.email:
            raise AuthenticationError(
                "ONEMAP_EMAIL not configured."
            )

        if not self.password:
            raise AuthenticationError(
                "ONEMAP_PASSWORD not configured."
            )

        self.session = requests.Session()

        self._token: str | None = None
        # The moment we should REFRESH at (the real expiry, already reduced by
        # TOKEN_REFRESH_BUFFER), not the raw expiry — so the buffer is applied
        # exactly once, where the token is stored.
        self._expiry: float = 0

        # CareRoutingAgent fans its clinic shortlist out over asyncio.to_thread,
        # so several worker THREADS reach token() on the same client at once.
        # Without this lock a cold client produces a token stampede (one getToken
        # per thread) and, worse, torn state: thread A's token can end up stored
        # against thread B's expiry.
        self._auth_lock = threading.Lock()

    # -------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------

    def authenticate(self) -> str:
        """
        Request a JWT from OneMap and cache it in memory.

        Thread-safe: concurrent callers collapse onto a single getToken call,
        and a caller that was queued behind one reuses the fresh token instead
        of asking again.
        """

        with self._auth_lock:
            # Someone else may have refreshed while we waited for the lock.
            if self._token is not None and time.time() < self._expiry:
                return self._token
            return self._authenticate_locked()

    def _authenticate_locked(self) -> str:
        """The actual getToken round-trip. Caller must hold `self._auth_lock`."""

        payload = {
            "email": self.email,
            "password": self.password,
        }

        try:

            response = self.session.post(
                AUTH_URL,
                json=payload,
                timeout=30,
            )

        except requests.RequestException as exc:

            raise AuthenticationError(
                f"Unable to contact OneMap: {exc}"
            ) from exc

        if response.status_code != 200:

            raise AuthenticationError(
                f"Authentication failed: {response.text}"
            )

        data = response.json()

        if "access_token" not in data:

            raise AuthenticationError(
                "OneMap returned no access token."
            )

        token = data["access_token"]

        self._token = token
        self._expiry = self._refresh_deadline(data)

        logger.info("OneMap token obtained.")

        return token

    # -------------------------------------------------------------

    @staticmethod
    def _refresh_deadline(data: dict) -> float:
        """When to refresh, derived from OneMap's own `expiry_timestamp`.

        Hard-coding 3 days meant that whenever OneMap issued a shorter-lived
        token, every routing call in the gap failed with a 401 that the patient
        saw as "no travel estimate available". The response tells us the real
        expiry (unix seconds, as a string), so use it; fall back to the
        documented lifetime only when it is absent or unparseable.
        """
        raw = data.get("expiry_timestamp")
        try:
            expires_at = float(raw)
        except (TypeError, ValueError):
            expires_at = time.time() + DEFAULT_TOKEN_LIFETIME
        return expires_at - TOKEN_REFRESH_BUFFER

    # -------------------------------------------------------------

    def token(self) -> str:

        # `_expiry` already has TOKEN_REFRESH_BUFFER subtracted, so this is a
        # plain comparison. Read once: another thread may swap both fields.
        cached = self._token

        if cached is None or time.time() >= self._expiry:

            return self.authenticate()

        return cached

    # -------------------------------------------------------------

    def _force_reauthenticate(self) -> str:
        """Discard the cached token and get a new one (used on a 401)."""

        with self._auth_lock:
            return self._authenticate_locked()

    # -------------------------------------------------------------

    @property
    def headers(self) -> dict[str, str]:

        return {
            "Authorization": self.token()
        }

    # -------------------------------------------------------------
    # Internal GET helper
    # -------------------------------------------------------------

    def _get(
        self,
        url: str,
        params: dict[str, Any],
    ) -> dict:
        """GET an OneMap endpoint, re-authenticating once on a 401.

        A cached token can be revoked server-side before its stated expiry. One
        transparent retry turns a hard routing failure into a normal response;
        there is deliberately no retry LOOP, so a genuinely bad credential fails
        fast rather than hammering OneMap once per clinic in the shortlist.
        """

        response = self._request(url, params)

        if response.status_code == 401:
            logger.info("OneMap returned 401; refreshing token and retrying once.")
            self._force_reauthenticate()
            response = self._request(url, params)

        # Every decode below is inside the try: OneMap's 5xx and 401 responses
        # are HTML/plain-text error pages, and calling .json() on one BEFORE the
        # status check (as this used to) raised a JSONDecodeError straight out of
        # the service. The routing agent only catches OneMapError, so that
        # escaped into the triage pipeline instead of falling back gracefully to
        # the local travel estimate.
        try:

            if response.status_code != 200:
                raise OneMapError(
                    f"{response.status_code}: {response.text}"
                )

            data = response.json()

        except ValueError as exc:
            # OneMapError is not a ValueError, so the raise above passes through.
            raise OneMapError(
                f"OneMap returned a non-JSON body (HTTP {response.status_code})"
            ) from exc

        # OneMap sometimes returns HTTP 200 with an "error" field
        if isinstance(data, dict) and data.get("error"):
            raise OneMapError(data["error"])

        return data

    # -------------------------------------------------------------

    def _request(self, url: str, params: dict[str, Any]):

        try:

            return self.session.get(
                url,
                headers=self.headers,
                params=params,
                timeout=30,
            )

        except requests.RequestException as exc:
            raise OneMapError(str(exc)) from exc

    def search(
        self,
        search_value: str,
        page: int = 1,
    ) -> list[dict]:
        """
        Search OneMap.

        Example

        client.search("Singapore General Hospital")
        """

        params = {
            "searchVal": search_value,
            "returnGeom": "Y",
            "getAddrDetails": "Y",
            "pageNum": page,
        }

        data = self._get(
            SEARCH_URL,
            params,
        )

        return data.get("results", [])

    def convert_svy21(
        self,
        x: float,
        y: float,
    ) -> tuple[float, float]:

        data = self._get(
            CONVERT_URL,
            {
                "X": x,
                "Y": y,
            },
        )

        return (
            float(data["latitude"]),
            float(data["longitude"]),
        )

    def route(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
        route_type: str = "walk",
        *,
        departure_at: datetime | None = None,
    ) -> dict:

        params = {
            "start": f"{start_lat},{start_lon}",
            "end": f"{end_lat},{end_lon}",
            "routeType": route_type,
        }
        # OneMap requires a date, time and mode for public-transport routing,
        # unlike walk/drive/cycle.  ``TRANSIT`` permits a mixed bus/MRT plan.
        # Use Singapore local time because the network and user are in SG.
        if route_type == "pt":
            when = departure_at or datetime.now(SINGAPORE_TIMEZONE)
            if when.tzinfo is None:
                when = when.replace(tzinfo=SINGAPORE_TIMEZONE)
            else:
                when = when.astimezone(SINGAPORE_TIMEZONE)
            params.update({
                "date": when.strftime("%m-%d-%Y"),
                "time": when.strftime("%H:%M:%S"),
                "mode": "TRANSIT",  # documented values are upper case: TRANSIT, BUS, RAIL
            })

        data = self._get(
            ROUTE_URL,
            params,
        )

        if isinstance(data.get("plan"), dict):
            return _transit_route(data["plan"])

        summary = data.get("route_summary", {})

        return {
            "distance": summary.get("total_distance"),
            "time": summary.get("total_time"),
            "geometry": data.get("route_geometry"),
            "instructions": data.get(
                "route_instructions",
                [],
            ),
        }

    def walking_time(
        self,
        start_lat,
        start_lon,
        end_lat,
        end_lon,
    ) -> int:

        return self.route(
            start_lat,
            start_lon,
            end_lat,
            end_lon,
            "walk",
        )["time"]


_TRANSIT_MODES = {"BUS": "bus", "SUBWAY": "MRT/LRT", "RAIL": "train", "TRAM": "LRT"}


def _transit_route(plan: dict) -> dict:
    """Public-transport routes come back in OpenTripPlanner shape
    (``plan.itineraries[].legs[]``), not the ``route_summary`` walk/drive/cycle
    use. Reading only ``route_summary`` returned ``time=None`` for every "pt"
    request, so each one fell back to a straight-line estimate AND counted as a
    provider failure against the routing circuit breaker, which then degraded
    later walk/drive cases on the same task too.

    Same keys as the other route types: ``time`` (s), ``distance`` (m),
    ``geometry`` (one encoded polyline per leg; the router decodes and joins
    them) and ``instructions`` (one step per leg, text in the last element as
    the router's formatter expects). No itinerary -> ``time=None`` -> the
    router's honest fallback, as before.
    """
    itineraries = plan.get("itineraries") or []
    if not itineraries or not isinstance(itineraries[0], dict):
        return {"distance": None, "time": None, "geometry": None, "instructions": []}
    itinerary = itineraries[0]
    legs = [leg for leg in itinerary.get("legs") or [] if isinstance(leg, dict)]
    steps = []
    for leg in legs:
        mode = str(leg.get("mode") or "").upper()
        to_name = str((leg.get("to") or {}).get("name") or "").strip()
        if mode == "WALK":
            metres = leg.get("distance")
            text = f"Walk {round(metres)} m" if isinstance(metres, (int, float)) else "Walk"
        else:
            service = str(leg.get("routeShortName") or leg.get("route") or "").strip()
            text = f"Take {_TRANSIT_MODES.get(mode, mode.lower() or 'transit')} {service}".rstrip()
            from_name = str((leg.get("from") or {}).get("name") or "").strip()
            if from_name:
                text += f" from {from_name}"
        if to_name and to_name.lower() != "destination":
            text += f" to {to_name}"
        steps.append([text])
    distance = sum(leg["distance"] for leg in legs if isinstance(leg.get("distance"), (int, float)))
    return {
        "distance": distance or None,
        "time": itinerary.get("duration"),
        "geometry": [
            (leg.get("legGeometry") or {}).get("points") for leg in legs
            if isinstance((leg.get("legGeometry") or {}).get("points"), str)
        ],
        "instructions": steps,
    }
