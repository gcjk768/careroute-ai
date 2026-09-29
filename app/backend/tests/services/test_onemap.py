"""Opt-in OneMap smoke test; never contact OneMap or print tokens at import time."""
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from app.services.onemap import TOKEN_REFRESH_BUFFER, OneMapClient, OneMapError

# Fake fixture credential: OneMap auth is mocked, nothing is sent anywhere.
_FAKE_PASSWORD = "not-a-real-password"  # noqa: S105  # nosec B105

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# Test doubles — a scripted requests.Session. Nothing here touches the network.
# --------------------------------------------------------------------------
class _Response:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else str(payload)

    def json(self):
        if self._payload is None:
            # Mirrors requests' behaviour for a non-JSON body (an HTML error page).
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _FakeSession:
    """Records every call and replays a scripted sequence of responses."""

    def __init__(self, auth_responses=None, get_responses=None):
        self.auth_responses = list(auth_responses or [])
        self.get_responses = list(get_responses or [])
        self.posts: list[dict] = []
        self.gets: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        return self.auth_responses.pop(0)

    def get(self, url, headers=None, params=None, timeout=None):
        self.gets.append({"url": url, "headers": headers, "params": params})
        return self.get_responses.pop(0)


def _client(session) -> OneMapClient:
    client = OneMapClient(email="test@example.com", password=_FAKE_PASSWORD)
    client.session = session
    return client


def _auth_ok(token="tok-1", expiry=None):  # noqa: S107 - fake fixture token
    payload = {"access_token": token}
    if expiry is not None:
        payload["expiry_timestamp"] = expiry
    return _Response(200, payload)


# --------------------------------------------------------------------------
# (a) Token lifetime must come from OneMap, not from a hard-coded guess
# --------------------------------------------------------------------------
def test_token_expiry_uses_the_value_onemap_returned():
    """OneMap's getToken response carries ``expiry_timestamp`` (unix seconds).
    Assuming a flat 3 days instead means that when OneMap issues a shorter-lived
    token, every routing call inside the gap fails with a 401 that the client
    reports as "no route available" — the patient silently loses their travel
    estimate. Trust the server's own expiry."""
    expires_at = time.time() + 3600
    session = _FakeSession(auth_responses=[_auth_ok(expiry=str(int(expires_at)))])
    client = _client(session)

    client.authenticate()

    assert client._expiry == pytest.approx(expires_at - TOKEN_REFRESH_BUFFER, abs=2)


def test_token_expiry_falls_back_when_onemap_omits_or_mangles_it():
    for payload_expiry in (None, "not-a-number", ""):
        session = _FakeSession(auth_responses=[_auth_ok(expiry=payload_expiry)])
        client = _client(session)
        client.authenticate()
        # The documented 3-day lifetime, still minus the refresh buffer.
        assert client._expiry == pytest.approx(
            time.time() + (60 * 60 * 24 * 3) - TOKEN_REFRESH_BUFFER, abs=5
        )


def test_a_still_valid_token_is_reused_without_re_authenticating():
    session = _FakeSession(auth_responses=[_auth_ok(expiry=str(int(time.time() + 3600)))])
    client = _client(session)
    assert client.token() == "tok-1"
    assert client.token() == "tok-1"
    assert len(session.posts) == 1


def test_an_expired_token_triggers_exactly_one_re_authentication():
    session = _FakeSession(
        auth_responses=[
            _auth_ok("tok-1", expiry=str(int(time.time() - 10))),
            _auth_ok("tok-2", expiry=str(int(time.time() + 3600))),
        ]
    )
    client = _client(session)
    assert client.token() == "tok-1"
    assert client.token() == "tok-2"
    assert len(session.posts) == 2


# --------------------------------------------------------------------------
# (b) A non-JSON error body must surface as OneMapError, not JSONDecodeError
# --------------------------------------------------------------------------
def test_non_json_5xx_body_raises_onemaperror_not_a_json_decode_error():
    """``response.json()`` ran BEFORE the status check and outside the try, so a
    gateway's HTML 502 page raised ``JSONDecodeError`` straight out of the
    service. The routing agent catches ``OneMapError``, so that escaped into the
    triage pipeline as an unhandled exception instead of a graceful fallback to
    the local travel estimate."""
    session = _FakeSession(
        auth_responses=[_auth_ok(expiry=str(int(time.time() + 3600)))],
        get_responses=[_Response(502, payload=None, text="<html>502 Bad Gateway</html>")],
    )
    client = _client(session)

    with pytest.raises(OneMapError) as excinfo:
        client.search("Singapore General Hospital")

    assert "502" in str(excinfo.value)


def test_non_json_200_body_also_raises_onemaperror():
    session = _FakeSession(
        auth_responses=[_auth_ok(expiry=str(int(time.time() + 3600)))],
        get_responses=[_Response(200, payload=None, text="not json at all")],
    )
    client = _client(session)
    with pytest.raises(OneMapError):
        client.search("anything")


def test_a_transport_failure_is_still_a_onemaperror():
    class _Boom(_FakeSession):
        def get(self, *a, **kw):
            raise requests.RequestException("connection reset")

    session = _Boom(auth_responses=[_auth_ok(expiry=str(int(time.time() + 3600)))])
    with pytest.raises(OneMapError):
        _client(session).search("anything")


def test_error_field_in_a_200_body_is_still_raised():
    session = _FakeSession(
        auth_responses=[_auth_ok(expiry=str(int(time.time() + 3600)))],
        get_responses=[_Response(200, payload={"error": "invalid searchVal"})],
    )
    with pytest.raises(OneMapError, match="invalid searchVal"):
        _client(session).search("anything")


# --------------------------------------------------------------------------
# (c) A 401 mid-session must re-authenticate once and retry once
# --------------------------------------------------------------------------
def test_a_401_reauthenticates_once_and_retries_the_request():
    """A cached token can be revoked server-side before its stated expiry. One
    transparent retry turns a hard routing failure into a normal response."""
    long_life = str(int(time.time() + 86400))
    session = _FakeSession(
        auth_responses=[_auth_ok("tok-1", expiry=long_life), _auth_ok("tok-2", expiry=long_life)],
        get_responses=[
            _Response(401, payload=None, text="Unauthorized"),
            _Response(200, payload={"results": [{"SEARCHVAL": "SGH"}]}),
        ],
    )
    client = _client(session)

    assert client.search("Singapore General Hospital") == [{"SEARCHVAL": "SGH"}]
    assert len(session.posts) == 2, "expected exactly one re-authentication"
    assert len(session.gets) == 2, "expected exactly one retry"
    assert session.gets[0]["headers"]["Authorization"] == "tok-1"
    assert session.gets[1]["headers"]["Authorization"] == "tok-2"


def test_a_persistent_401_gives_up_after_one_retry():
    """No retry loop: a genuinely bad credential must fail fast and loudly
    rather than hammering OneMap on every clinic in the shortlist."""
    long_life = str(int(time.time() + 86400))
    session = _FakeSession(
        auth_responses=[_auth_ok("tok-1", expiry=long_life), _auth_ok("tok-2", expiry=long_life)],
        get_responses=[
            _Response(401, payload=None, text="Unauthorized"),
            _Response(401, payload=None, text="Unauthorized"),
        ],
    )
    client = _client(session)

    with pytest.raises(OneMapError) as excinfo:
        client.search("anything")

    assert "401" in str(excinfo.value)
    assert len(session.gets) == 2
    assert len(session.posts) == 2


# --------------------------------------------------------------------------
# (d) route() is called from asyncio.to_thread workers -> auth must be locked
# --------------------------------------------------------------------------
def test_concurrent_first_use_authenticates_once():
    """``CareRoutingAgent`` fans out its clinic shortlist over
    ``asyncio.to_thread``, so several worker threads hit ``token()`` on a cold
    client simultaneously. Unsynchronised, they each POST to getToken and each
    overwrite ``_token``/``_expiry`` — a token stampede, and a torn pair where
    one thread's token is stored against another's expiry."""
    long_life = str(int(time.time() + 86400))
    workers = 8
    start = threading.Barrier(workers)

    class _SlowAuthSession(_FakeSession):
        def post(self, url, json=None, timeout=None):
            time.sleep(0.02)  # widen the window a racy implementation would lose
            return super().post(url, json=json, timeout=timeout)

    session = _SlowAuthSession(auth_responses=[_auth_ok("tok-1", expiry=long_life)] * workers)
    client = _client(session)

    tokens: list[str] = []
    errors: list[BaseException] = []

    def worker():
        try:
            start.wait(timeout=5)  # all threads hit a cold token() together
            tokens.append(client.token())
        except BaseException as exc:  # noqa: BLE001 - re-raised in the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert tokens == ["tok-1"] * workers
    assert len(session.posts) == 1, f"token stampede: {len(session.posts)} getToken calls"


def test_public_transport_route_supplies_onemap_required_journey_parameters(monkeypatch):
    """PT calls include departure context; walk/drive do not need these fields."""
    client = OneMapClient(email="test@example.com", password=_FAKE_PASSWORD)
    captured = {}

    def fake_get(_url, params):
        captured.update(params)
        return {"route_summary": {"total_distance": 1200, "total_time": 900}}

    monkeypatch.setattr(client, "_get", fake_get)
    client.route(
        1.3000, 103.8000, 1.3100, 103.8100, "pt",
        departure_at=datetime(2026, 8, 31, 9, 30, tzinfo=ZoneInfo("Asia/Singapore")),
    )

    assert captured["routeType"] == "pt"
    assert captured["mode"] == "TRANSIT"
    assert captured["date"] == "08-31-2026"
    assert captured["time"] == "09:30:00"


def test_public_transport_response_is_parsed_from_the_otp_plan(monkeypatch):
    """OneMap answers "pt" in OpenTripPlanner shape, not route_summary. Reading
    only route_summary gave time=None for every public-transport request."""
    client = OneMapClient(email="test@example.com", password=_FAKE_PASSWORD)
    otp = {"plan": {"itineraries": [{
        "duration": 1500,
        "legs": [
            {"mode": "WALK", "distance": 300.4, "to": {"name": "Tampines Stn Exit A"},
             "legGeometry": {"points": "_p~iF~ps|U_ulLnnqC"}},
            {"mode": "BUS", "route": "22", "routeShortName": "22", "distance": 4200.0,
             "from": {"name": "Tampines Stn Exit A"}, "to": {"name": "Blk 151"},
             "legGeometry": {"points": "_mqNvxq`@"}},
            {"mode": "WALK", "distance": 80.0, "to": {"name": "Destination"},
             "legGeometry": {"points": "_c`|@_seK"}},
        ],
    }]}}
    monkeypatch.setattr(client, "_get", lambda _url, _params: otp)

    route = client.route(1.35, 103.94, 1.34, 103.95, "pt")

    assert route["time"] == 1500
    assert route["distance"] == 4580.4
    assert route["geometry"] == ["_p~iF~ps|U_ulLnnqC", "_mqNvxq`@", "_c`|@_seK"]
    assert [step[-1] for step in route["instructions"]] == [
        "Walk 300 m to Tampines Stn Exit A",
        "Take bus 22 from Tampines Stn Exit A to Blk 151",
        "Walk 80 m",
    ]


def test_public_transport_without_an_itinerary_reports_no_time(monkeypatch):
    client = OneMapClient(email="test@example.com", password=_FAKE_PASSWORD)
    monkeypatch.setattr(client, "_get", lambda _url, _params: {"plan": {"itineraries": []}})
    assert client.route(1.35, 103.94, 1.34, 103.95, "pt")["time"] is None


def test_live_onemap_smoke():
    if os.environ.get("RUN_LIVE_TOOL_TESTS") != "1":
        pytest.skip("set RUN_LIVE_TOOL_TESTS=1 to call live OneMap")

    client = OneMapClient()
    assert client.token()  # Never print a bearer token.
    assert client.search("Singapore General Hospital")

    lat, lon = client.convert_svy21(28983.788791, 33554.509813)
    assert isinstance(lat, float)
    assert isinstance(lon, float)

    route = client.route(1.319728, 103.8421, 1.319728905, 103.8421581, "walk")
    assert "distance" in route
    assert "time" in route
