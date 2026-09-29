"""[Agentic] Correlation IDs (AAS Day 3 AM slide 17; Lecture 03 aggregated logging).

The properties worth pinning:
  * a module that never heard of this file still emits a correlated line —
    otherwise the feature is just another thing call sites must remember;
  * an inbound header is UNTRUSTED: no log injection, no unbounded length;
  * a malformed header is replaced, never fatal — a debugging label must not be
    able to fail a triage;
  * concurrent requests do not see each other's ID.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app import correlation

pytestmark = pytest.mark.observability


@pytest.fixture(autouse=True)
def _clean_binding():
    token = correlation.bind(correlation.UNSET)
    case = correlation.bind_case(correlation.UNSET)
    yield
    correlation.reset_case(case)
    correlation.reset(token)


# ---------------------------------------------------------------------------
# Untrusted input
# ---------------------------------------------------------------------------
def test_a_newline_in_the_header_cannot_forge_a_log_line():
    """Log injection: without this, a caller could write their own audit-looking
    lines into the file an incident is later reconstructed from."""
    forged = "ok\nWARNING [cid=x] careroute: escalation approved by clinician"
    cleaned = correlation.sanitise(forged)
    assert "\n" not in cleaned
    assert " " not in cleaned
    print("[CORRELATION PASS] Newlines and spaces are stripped, so a caller-supplied ID cannot")
    print("                   forge a second log line.")


def test_an_oversized_header_is_capped():
    assert len(correlation.sanitise("x" * 10_000)) == correlation.MAX_LENGTH
    print(f"[CORRELATION PASS] A hostile header is capped at {correlation.MAX_LENGTH} chars rather than being")
    print("                   written to disk on every line of the request.")


@pytest.mark.parametrize("raw", ["", None, "!!!", "\n\n", "   "])
def test_an_unusable_header_yields_none_so_a_fresh_id_is_minted(raw):
    assert correlation.sanitise(raw) is None


def test_a_usable_header_is_preserved_so_ids_survive_a_gateway():
    assert correlation.sanitise("trace-abc_123.4") == "trace-abc_123.4"
    print("[CORRELATION PASS] A well-formed upstream ID is kept, so one request keeps one identity")
    print("                   across a front end or load balancer.")


# ---------------------------------------------------------------------------
# Propagation — the whole point
# ---------------------------------------------------------------------------
def test_a_module_that_never_opted_in_still_logs_correlated(caplog):
    """`careroute.llm` does not import this module and is never handed an ID."""
    correlation.install()
    stray = logging.getLogger("careroute.llm")
    token = correlation.bind("req_deadbeef")
    correlation.bind_case("case_0001")
    # caplog installs its own handler AFTER install() ran, so it needs the filter
    # too. Removed again below: leaving it attached leaks into every later test
    # sharing pytest's handler — which is exactly what made the idempotency test
    # below fail the first time this file ran.
    injected = correlation.CorrelationFilter()
    caplog.handler.addFilter(injected)
    try:
        with caplog.at_level(logging.INFO):
            stray.info("served by provider=openai")
    finally:
        caplog.handler.removeFilter(injected)
        correlation.reset(token)
    record = caplog.records[-1]
    assert record.correlation_id == "req_deadbeef"
    assert record.case_id == "case_0001"
    print("[CORRELATION PASS] A module with no knowledge of correlation still emits a correlated")
    print("                   record — the filter cannot be forgotten by a call site.")


def test_records_outside_a_request_are_marked_not_blank():
    """A blank column reads as a formatting bug; '-' reads as 'no request'."""
    assert correlation.get() == correlation.UNSET
    assert correlation.UNSET == "-"


def test_install_is_idempotent():
    """The app module can be imported twice under a test runner; stamping twice
    or stacking handlers would corrupt every line."""
    correlation.install()
    correlation.install()
    root = logging.getLogger()
    for handler in root.handlers:
        filters = [f for f in handler.filters if isinstance(f, correlation.CorrelationFilter)]
        assert len(filters) <= 1
    print("[CORRELATION PASS] install() is idempotent — a double import cannot double-stamp.")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_concurrent_requests_do_not_share_an_id():
    """A ContextVar is per-task; a module-level global would have leaked one
    patient's correlation ID into another's log lines."""
    seen: dict[str, str] = {}

    async def _request(name: str) -> None:
        token = correlation.bind(f"req_{name}")
        try:
            await asyncio.sleep(0)  # force interleaving
            seen[name] = correlation.get()
        finally:
            correlation.reset(token)

    async def _drive() -> None:
        await asyncio.gather(*(_request(n) for n in ("a", "b", "c")))

    asyncio.run(_drive())
    assert seen == {"a": "req_a", "b": "req_b", "c": "req_c"}
    print("[CORRELATION PASS] Three interleaved requests keep three distinct IDs — no cross-patient")
    print("                   bleed in the log trail.")


def test_every_response_carries_the_security_headers_zap_asked_for():
    """ZAP API scan 2026-09-25: X-Content-Type-Options and CORP were missing.
    Set in the same middleware that stamps the correlation id."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        for path in ("/api/health", "/metrics", "/api/cases/nope/status"):
            r = client.get(path)
            assert r.headers["X-Content-Type-Options"] == "nosniff", path
            assert r.headers["Cross-Origin-Resource-Policy"] == "same-origin", path
