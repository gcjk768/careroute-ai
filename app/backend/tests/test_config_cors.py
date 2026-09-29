"""CORS origins are deployment configuration, not source code.

WHY THIS EXISTS
---------------
``main.py`` hard-coded ``allow_origins=["http://localhost:5173",
"http://localhost:3000"]``, so the only way to run the backend behind a real
frontend hostname was to edit and redeploy the image — and with
``allow_credentials=True`` an over-broad "just use *" edit is a genuine security
regression waiting to happen. The list now comes from
``CAREROUTE_CORS_ORIGINS`` and keeps the two localhost defaults.

The middleware is attached at import time, so the parsing helper is what is
worth testing; a test that re-imports ``app.main`` under a patched environment
would leave a second FastAPI app (and a second rate limiter) behind for every
later test in the session.
"""
from __future__ import annotations

from app import config, main


def test_default_origins_are_the_two_local_dev_servers():
    assert config._csv("CAREROUTE_CORS_ORIGINS_UNSET_KEY", config._DEFAULT_CORS_ORIGINS) == [
        "http://localhost:5173",
        "http://localhost:3000",
    ]


def test_cors_origins_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("CAREROUTE_CORS_ORIGINS", "https://care.example.sg, https://staging.example.sg")
    assert config._csv("CAREROUTE_CORS_ORIGINS", config._DEFAULT_CORS_ORIGINS) == [
        "https://care.example.sg",
        "https://staging.example.sg",
    ]


def test_blank_entries_are_dropped(monkeypatch):
    monkeypatch.setenv("CAREROUTE_CORS_ORIGINS", "https://a.example, ,,https://b.example,")
    assert config._csv("CAREROUTE_CORS_ORIGINS", "") == ["https://a.example", "https://b.example"]


def test_an_empty_setting_falls_back_to_the_defaults(monkeypatch):
    """An operator who blanks the variable must not accidentally get an
    origin-less (i.e. silently broken) frontend."""
    monkeypatch.setenv("CAREROUTE_CORS_ORIGINS", "   ")
    assert config._csv("CAREROUTE_CORS_ORIGINS", config._DEFAULT_CORS_ORIGINS) == [
        "http://localhost:5173",
        "http://localhost:3000",
    ]


def test_the_running_app_uses_the_configured_list():
    cors = [m for m in main.app.user_middleware if "CORSMiddleware" in str(m)]
    assert cors, "CORS middleware missing"
    assert cors[0].kwargs["allow_origins"] == config.CORS_ORIGINS
