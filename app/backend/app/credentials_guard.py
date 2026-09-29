"""[AI-Security] "Nothing below production spends money" — the no-live-credentials guard.

An agent that can reach a live external service from CI is one bad fixture away
from spending real money, burning a rate-limited quota, or writing to a partner
system. The rule from the course's reference pipeline is that every
non-production environment asserts it holds no credential that would let that
happen; CI and staging run against mocks or capped sandboxes instead.

SCOPE IS THE WHOLE DESIGN HERE. Only credentials an AGENT can reach are covered:

* `OPENAI_API_KEY`  - the LLM provider; costs money per call.
* `ONEMAP_EMAIL` / `ONEMAP_PASSWORD` - a rate-limited free account shared by the
  team, whose password has already leaked into a chat once.
* `ONYX_API_KEY`    - retrieval backend.

CI PLUMBING credentials are deliberately excluded — the SonarQube token, the
pipeline trigger token, the MLflow token, the NVD key. Those belong to the
pipeline, no agent can reach them, and they MUST be present for their jobs to
work. A gate that fires on them would be wrong every single run, and a gate
that is wrong every run is one everybody learns to ignore.

The guard is also inert on a developer's machine: `tests/test_routing_live.py`
exists precisely so a human can exercise the real OneMap path, and failing their
local suite for holding the credentials that test needs would be nonsense. It
arms when the environment says it is CI (or says it is a non-production
deployment), which is exactly where the rule applies.

Run:  python -m app.credentials_guard [--out no-live-credentials.json]
Exit: 0 clean or not applicable, 1 a non-production environment holds live credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping

# Credentials an agent can reach that touch a live, paid, or rate-limited service.
AGENT_LIVE_CREDENTIALS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ONEMAP_EMAIL",
    "ONEMAP_PASSWORD",
    "ONYX_API_KEY",
)

# Values that are obviously not a working credential. Compared case-insensitively
# against the whole (stripped) value, plus a couple of common prefixes.
_PLACEHOLDERS = frozenset({
    "changeme", "change-me", "dummy", "test", "testing", "fake", "placeholder",
    "example", "none", "null", "unset", "xxx", "xxxx", "todo", "redacted",
    "not-available", "not_available", "n/a", "na", "-",
})
_PLACEHOLDER_PREFIXES = ("your-", "your_", "<", "${", "sk-test-", "example@")


def _is_placeholder(value: str) -> bool:
    v = value.strip()
    if not v:
        return True
    low = v.lower()
    return low in _PLACEHOLDERS or low.startswith(_PLACEHOLDER_PREFIXES)


def is_non_production(env: Mapping[str, str] | None = None) -> bool:
    """True when this environment is one the rule applies to.

    Armed by GitLab's `CI`, or by an explicit non-production `CAREROUTE_ENV`.
    A bare developer shell is NOT non-production for this purpose — see module
    docstring. `CAREROUTE_ENV=production` always wins.
    """
    env = os.environ if env is None else env
    declared = (env.get("CAREROUTE_ENV") or "").strip().lower()
    if declared in {"production", "prod"}:
        return False
    if declared in {"ci", "staging", "test", "dev", "development"}:
        return True
    return (env.get("CI") or "").strip().lower() in {"true", "1", "yes"}


def allowed_live_credentials(env: Mapping[str, str] | None = None) -> set[str]:
    """Credentials this environment has DECLARED it is allowed to hold.

    `ai-security:garak` and `ai-security:promptfoo` need a live `OPENAI_API_KEY`
    in CI to do anything at all — which is precisely what this gate forbids. The
    course's answer is a self-hosted model in CI (its reference pipeline runs
    "vLLM, pinned small", money at risk: none), and CareRoute has no such thing
    yet.

    So the compromise is declared rather than hidden. Deleting `OPENAI_API_KEY`
    from the gate's scope would make the exception invisible and permanent;
    naming it in the job that needs it keeps the exception in the pipeline
    definition, reported in the artifact, and easy to withdraw the day a
    self-hosted model exists.
    """
    env = os.environ if env is None else env
    raw = env.get("CAREROUTE_ALLOWED_LIVE_CREDENTIALS") or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def find_live_credentials(env: Mapping[str, str] | None = None) -> list[str]:
    """Names (never values) of agent-reachable live credentials that are set to
    something that looks real. Declared exemptions are excluded."""
    env = os.environ if env is None else env
    allowed = allowed_live_credentials(env)
    return [name for name in AGENT_LIVE_CREDENTIALS
            if name not in allowed and not _is_placeholder(env.get(name) or "")]


def find_exempted_credentials(env: Mapping[str, str] | None = None) -> list[str]:
    """Declared exemptions that are ACTUALLY set — the ones being used. An
    exemption for a credential nobody supplied is not worth reporting."""
    env = os.environ if env is None else env
    allowed = allowed_live_credentials(env)
    return [name for name in AGENT_LIVE_CREDENTIALS
            if name in allowed and not _is_placeholder(env.get(name) or "")]


def build_report(env: Mapping[str, str] | None = None) -> dict:
    """A findings payload carrying credential NAMES only. The value is the thing
    we are protecting; it must never reach a CI artifact."""
    env = os.environ if env is None else env
    applicable = is_non_production(env)
    live = find_live_credentials(env)
    exempted = find_exempted_credentials(env)
    return {
        "applicable": applicable,
        "environment": (env.get("CAREROUTE_ENV") or ("ci" if env.get("CI") else "developer")),
        "checked": list(AGENT_LIVE_CREDENTIALS),
        "live": live,
        # Reported, always: an exemption that does not show up in the artifact is
        # indistinguishable from a gate that was quietly narrowed.
        "exempted": exempted,
        "verdict": "FAIL" if (applicable and live) else "PASS",
    }


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert no live credentials outside production.")
    parser.add_argument("--out", default="no-live-credentials.json", help="where to write the JSON report")
    args = parser.parse_args(argv)

    report = build_report(env)

    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    except OSError as exc:
        print(f"WARNING: could not write {args.out}: {exc}")

    print("=== CareRoute no-live-credentials guard ===")
    print(f"  environment : {report['environment']}")
    print(f"  checked     : {', '.join(report['checked'])}")
    if report["exempted"]:
        print(f"  EXEMPTED    : {', '.join(report['exempted'])}"
              " (declared via CAREROUTE_ALLOWED_LIVE_CREDENTIALS)")
        print("                This is a live credential in a non-production environment,")
        print("                allowed on purpose. Withdraw it once CI has a self-hosted model.")
    if not report["applicable"]:
        print("  not applicable here (production, or a developer machine) - reporting only")
        return 0
    if report["live"]:
        print(f"  FAILED      : live credentials present -> {', '.join(report['live'])}")
        print("                A non-production environment must use mocks or a capped sandbox.")
        return 1
    print("  PASSED      : no agent-reachable live credentials in this environment")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
