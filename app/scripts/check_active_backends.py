"""Assert a running backend reports the backends its image was built with.

    python scripts/check_active_backends.py --base-url http://localhost:8000 \
        [--with-safety-nlp 0|1] [--wait 300]

retrieval must always be "hybrid" (the monolith always bakes the embedder).
safetyNlp must be "model" only for an image built with
`--build-arg WITH_SAFETY_NLP=1`; the default image honestly reports "rules".
Both features degrade silently, so a broken image still answers /api/health
with 200 — this reads the fields that say what actually loaded. Stdlib only:
runs in the docker:27 CI image and against the AWS ALB alike.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def problems(health: dict, *, require_safety_nlp: bool = False) -> list[str]:
    expected = {"retrieval": "hybrid"}
    if require_safety_nlp:
        expected["safetyNlp"] = "model"
    return [f"{key}={health.get(key)!r}, expected {want!r}"
            for key, want in expected.items() if health.get(key) != want]


def fetch(base: str, wait: int) -> dict:
    end = time.monotonic() + wait
    while True:
        try:
            # main() rejects any non-http(s) --base-url, so no file:// / custom scheme.
            # nosemgrep: bandit.B310-1
            with urllib.request.urlopen(f"{base}/api/health", timeout=5) as resp:  # noqa: S310  # nosec B310
                return json.loads(resp.read())
        except OSError:
            if time.monotonic() > end:
                raise
            time.sleep(3)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--with-safety-nlp", default="0",
                        help="1 if the image was built with WITH_SAFETY_NLP=1")
    parser.add_argument("--wait", type=int, default=0, help="seconds to wait for the backend to come up")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        sys.exit(f"--base-url must be http(s), got {base!r}")
    health = fetch(base, args.wait)
    found = problems(health, require_safety_nlp=args.with_safety_nlp == "1")
    print(json.dumps({key: health.get(key) for key in ("safetyNlp", "retrieval")}))
    if found:
        sys.exit("active-backend check FAILED: " + "; ".join(found))


if __name__ == "__main__":
    main()
