"""[AI-Security] LLM10 — the rate-limit key must not be attacker-controlled.

WHY THIS EXISTS
---------------
``_client_key`` trusted ``X-Forwarded-For`` unconditionally. That header is a
plain request header: anyone talking to the backend can set it, and set it to a
different value on every request. The fixed-window limiter then filed each
request under a different bucket, so the LLM10 control was bypassable with one
extra header — the exact opposite of a DoS control.

XFF is only meaningful when the request actually arrived via a proxy we operate.
So the peer address (``request.client.host``) now has to be in
``CAREROUTE_TRUSTED_PROXIES`` before the header is believed; the default is
empty, i.e. trust nobody, which is the safe posture for a container exposed
directly.

The limiter also never removed a bucket, so every distinct spoofed key leaked a
dict entry for the process's lifetime — an unbounded-memory DoS in its own
right. It now sweeps buckets whose newest hit fell out of the window.
"""
from __future__ import annotations

import ipaddress

import pytest
from starlette.requests import Request

from app import config
from app.main import _client_key
from app.ratelimit import RateLimiter


def _request(peer: str | None, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/triage/stream",
            "raw_path": b"/api/triage/stream",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": (peer, 51234) if peer else None,
            "server": ("testserver", 80),
        }
    )


# --------------------------------------------------------------------------
# config parsing
# --------------------------------------------------------------------------
def test_trusted_proxies_defaults_to_trusting_nobody():
    assert config._trusted_networks("") == []


def test_trusted_proxies_parses_ips_and_cidrs_and_ignores_junk():
    nets = config._trusted_networks("10.0.0.1, 172.16.0.0/12 , not-an-ip, ")
    assert ipaddress.ip_network("10.0.0.1/32") in nets
    assert ipaddress.ip_network("172.16.0.0/12") in nets
    assert len(nets) == 2


# --------------------------------------------------------------------------
# _client_key
# --------------------------------------------------------------------------
def test_spoofed_xff_shares_one_bucket_when_no_proxy_is_trusted(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [])
    keys = {
        _client_key(_request("203.0.113.9", xff=f"10.1.1.{i}"))
        for i in range(20)
    }
    assert keys == {"203.0.113.9"}, keys


def test_xff_is_honoured_when_the_peer_is_a_trusted_proxy(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", config._trusted_networks("203.0.113.9"))
    key = _client_key(_request("203.0.113.9", xff="198.51.100.7, 203.0.113.9"))
    assert key == "198.51.100.7"


def test_xff_from_an_untrusted_peer_inside_a_trusted_cidr_is_still_rejected(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", config._trusted_networks("10.0.0.0/8"))
    assert _client_key(_request("10.4.4.4", xff="1.2.3.4")) == "1.2.3.4"   # inside
    assert _client_key(_request("11.4.4.4", xff="1.2.3.4")) == "11.4.4.4"  # outside


def test_missing_peer_falls_back_to_unknown(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [])
    assert _client_key(_request(None, xff="1.2.3.4")) == "unknown"


def test_a_malformed_peer_address_is_never_treated_as_trusted(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", config._trusted_networks("10.0.0.0/8"))
    assert _client_key(_request("not-an-ip", xff="1.2.3.4")) == "not-an-ip"


# --------------------------------------------------------------------------
# limiter pruning
# --------------------------------------------------------------------------
def test_expired_buckets_are_pruned():
    limiter = RateLimiter(limit=5, window_seconds=60.0)
    for i in range(500):
        assert limiter.allow(f"key-{i}", now=1000.0)
    assert len(limiter._hits) == 500

    # One request a full window later: every earlier bucket is now dead weight.
    assert limiter.allow("late-comer", now=1000.0 + 61.0)
    assert len(limiter._hits) == 1, sorted(limiter._hits)[:5]


def test_pruning_does_not_forget_a_caller_still_inside_the_window():
    limiter = RateLimiter(limit=2, window_seconds=60.0)
    assert limiter.allow("victim", now=1000.0)
    assert limiter.allow("victim", now=1030.0)
    for i in range(50):
        limiter.allow(f"noise-{i}", now=1000.0)
    # Third hit inside the same window must still be refused.
    assert limiter.allow("victim", now=1050.0) is False


def test_reset_still_clears_everything():
    limiter = RateLimiter(limit=1)
    limiter.allow("a")
    limiter.reset()
    assert limiter._hits == {}
