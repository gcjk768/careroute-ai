"""[Agentic][MLOps] Correlation IDs — AAS Day 3 AM slide 17, and Lecture 03's
aggregated-logging problem.

OWNER: platform (James).

WHY THIS EXISTS
---------------
Slide 17 (API Gateway) asks for one thing this app did not have: *"stamp every
request with a correlation ID; propagate it to all downstream spans and logs."*

CareRoute already had something that LOOKED like this and was not. `case_id` is
minted inside the triage handler and passed by hand into the audit log and into
every `logger.info("case=%s ...")` in `main.py`. That correlates one endpoint's
log lines, and only the ones whose call site remembered to include it. The
modules that do the interesting work — `llm.py`, `agents/tool_gateway.py`,
`guardrail.py` — are never handed it, so their lines carry no case at all. With
six workers, a provider chain and a tool loop per triage, those are exactly the
lines you want when something goes wrong, and they were the uncorrelated ones.

Lecture 03's answer to "many agents, many containers" is a central log server.
A central log server is only as useful as the key you can group by, so this is
the cheap half of Gap 3 and worth having even before the ELK container exists.

HOW IT WORKS
------------
A ContextVar, set once per request by the middleware in `main.py`, read by a
logging filter that injects `correlation_id` into EVERY LogRecord. Call sites do
not change and cannot forget: a module that never heard of this file still emits
a correlated line. Same mechanism as the tool gateway's per-case budget, and for
the same reason — a request is an async task, so a ContextVar is exactly the
right scope, while a module-level global would bleed between concurrent triages.

`case_id` is NOT replaced. It stays the business identifier that the audit trail
and the clinician UI key on, and it is bound alongside the correlation ID once
the case exists. The two answer different questions: "which HTTP request was
this?" and "which patient episode was this?" — usually one-to-one, but not when
a client retries.

THE UNTRUSTED-INPUT PART
------------------------
An inbound `X-Correlation-ID` is honoured so a request keeps its identity across
a gateway or a front end — and it is attacker-controlled data that lands in log
files. Two specific risks, both handled in `sanitise()`:

* **Log injection.** A newline in the header would let a caller forge whole log
  lines, including a fake "escalation approved" record. Only `[A-Za-z0-9_.-]`
  survives.
* **Unbounded length.** A 10 MB header would be written to disk on every line of
  the request. Hard cap at 64 characters.

A header that does not survive sanitisation is REPLACED, not rejected: the
request is fine, only its label was unusable, and failing a triage over a
malformed debugging header would be the wrong trade in a clinical app.
"""
from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar

#: Header read on the way in and echoed on the way out. `X-Request-ID` is
#: accepted as an alias because that is what most load balancers emit.
HEADER = "X-Correlation-ID"
ALT_HEADER = "X-Request-ID"

#: Long enough to be unique in a log file, short enough to sit on every line.
MAX_LENGTH = 64

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")

#: "-" rather than "" so a log line always has a value in the column. An empty
#: field reads as a formatting bug; a dash reads as "outside any request", which
#: is what a startup or background-task line actually is.
UNSET = "-"

_CORRELATION_ID: ContextVar[str] = ContextVar("careroute_correlation_id", default=UNSET)
_CASE_ID: ContextVar[str] = ContextVar("careroute_case_id", default=UNSET)


def new_correlation_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def sanitise(raw: str | None) -> str | None:
    """Make a caller-supplied correlation ID safe to write to a log.

    Returns None if there is nothing usable, so the caller mints a fresh one.
    Strips rather than rejects: a truncated-but-valid label still correlates,
    and this is not an authentication input.
    """
    if not raw:
        return None
    cleaned = _SAFE.sub("", str(raw))[:MAX_LENGTH]
    return cleaned or None


def bind(correlation_id: str):
    """Bind a correlation ID to this async context; returns the reset token."""
    return _CORRELATION_ID.set(correlation_id)


def reset(token) -> None:
    _CORRELATION_ID.reset(token)


def bind_case(case_id: str):
    """Attach the business identifier once the case exists. Returns the token."""
    return _CASE_ID.set(case_id)


def reset_case(token) -> None:
    _CASE_ID.reset(token)


def get() -> str:
    return _CORRELATION_ID.get()


def get_case() -> str:
    return _CASE_ID.get()


class CorrelationFilter(logging.Filter):
    """Inject `correlation_id` / `case_id` into every record.

    A Filter rather than a LoggerAdapter or a custom Logger class, because those
    two only cover call sites that opt in — and the whole point is the modules
    that never opted in.

    Attached to the HANDLERS, not just the root logger, and that distinction is
    load-bearing: a filter on a logger runs only for records logged *directly*
    on it, NOT for records propagated up from child loggers. Since every line in
    this app comes from a child (`careroute.llm`, `uvicorn.access`, ...), a
    root-logger filter alone would stamp almost nothing. Handler filters run on
    everything that reaches the handler, which is what makes "group by this
    field" work across a whole request.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _CORRELATION_ID.get()
        record.case_id = _CASE_ID.get()
        return True


def install(level: int = logging.INFO) -> None:
    """Attach the filter and a format that shows the IDs.

    Idempotent: calling it twice must not double-stamp or double-handle, because
    the app module can be imported more than once under a test runner.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [cid=%(correlation_id)s case=%(case_id)s] %(name)s: %(message)s",
        # Belt and braces: a handler added AFTER us (uvicorn reconfigures logging
        # on startup) can deliver a record our filter never saw. `defaults` makes
        # the formatter total rather than raising KeyError mid-request — a
        # logging config error must never be able to fail a triage.
        defaults={"correlation_id": UNSET, "case_id": UNSET},
    )
    for handler in root.handlers:
        if any(isinstance(f, CorrelationFilter) for f in handler.filters):
            continue
        handler.setFormatter(formatter)
        handler.addFilter(CorrelationFilter())
