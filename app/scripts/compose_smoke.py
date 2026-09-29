"""Compose smoke test: one real triage through every agent container.

    python scripts/compose_smoke.py --base-url http://localhost:8000

Exits 0 only if the gateway is healthy, a red-flag triage completes and is
escalated, and the gateway's own metrics show at least one SUCCESSFUL call to
every agent container — so a degraded run (an agent down, the case escalated
by default) cannot pass as a healthy one. Stdlib only: runs in any CI image.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request

AGENTS = ("classifier", "safety", "routing", "hitl", "reflection", "handoff")
TEXT = "Crushing chest pain spreading to my left arm and sweating, started 20 minutes ago"


def _get(url: str) -> tuple[int, str]:
    # main() rejects any non-http(s) --base-url, so no file:// / custom scheme.
    # nosemgrep: bandit.B310-1
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310  # nosec B310
        return resp.status, resp.read().decode()


def wait_healthy(base: str, seconds: int) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if _get(f"{base}/api/health")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(3)
    sys.exit(f"gateway not healthy at {base} after {seconds}s")


def triage(base: str) -> dict:
    body = json.dumps({"text": TEXT, "language": "en", "isVoice": False}).encode()
    req = urllib.request.Request(  # noqa: S310 - fixed http URL from the CLI
        f"{base}/api/triage/stream", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    final = None
    # main() rejects any non-http(s) --base-url, so no file:// / custom scheme.
    # nosemgrep: bandit.B310-1
    with urllib.request.urlopen(req, timeout=240) as resp:  # noqa: S310  # nosec B310
        for raw in resp:
            line = raw.decode().strip()
            if line.startswith("data:"):
                event = json.loads(line[len("data:"):].strip())
                if event.get("event") == "final":
                    final = event
    if final is None:
        sys.exit("triage stream ended without a final event")
    return final


def ok_calls(base: str) -> dict[str, float]:
    _, text = _get(f"{base}/metrics")
    pattern = r'careroute_agent_calls_total\{agent="(\w+)",outcome="ok"\} ([0-9.e+]+)'
    return {m.group(1): float(m.group(2)) for m in re.finditer(pattern, text)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--wait", type=int, default=300)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        sys.exit(f"--base-url must be http(s), got {base!r}")
    wait_healthy(base, args.wait)
    final = triage(base)
    if not final.get("escalated"):
        sys.exit(f"red-flag case was not escalated: {final.get('acuity')}")
    calls = ok_calls(base)
    missing = [a for a in AGENTS if calls.get(a, 0) < 1]
    if missing:
        sys.exit(f"no successful call reached: {missing} (calls seen: {calls})")
    print(f"OK: escalated={final['escalated']} acuity={final['acuity']['code']} calls={calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
