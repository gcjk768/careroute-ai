---
tags: [handover, infra, todo, careroute]
updated: 2026-09-02
---
# Infra-Dependent Work — 2026-09-02

Back to [[Home]]. Related: [[Open Work 2026-09-01]] · [[MLOps Pipeline]] · [[Changelog]]

A whole-codebase audit on 2026-09-02 (four parallel reviewers: agents package, API/services, ML/CI/ops,
frontend; every finding verified against the code before being accepted) produced 48 findings. The
code-only ones were fixed the same day — see [[Changelog]]. This note holds the rest: items that need
infrastructure the team has **not set up yet**, credentials, or a decision. Nothing here was changed in
code. Ordered by risk.

---

## 1. No authentication on clinical endpoints — DECISION NEEDED

`GET /api/escalations` returns every escalation with the patient's free-text presentation;
`/api/escalations/{id}` adds rationale, evidence, normalised symptoms and the handoff summary;
`/api/cases/{id}/audit` returns the full trail; `POST /api/escalations/{id}/decision` lets any caller
close a clinician review. There is no `Depends`, no auth middleware, no router dependency anywhere.
CORS only restricts browsers; `curl` is unaffected.

**Why not fixed today:** it changes the API contract the staff portal relies on and needs a choice of
mechanism. Minimum viable: a shared bearer/API key (`CAREROUTE_STAFF_API_KEY`) as a FastAPI dependency on
`/api/escalations*`, `/api/cases/*/audit`, `/api/sessions/*`, sent by the staff pages from a server-side
env var. Real clinician identity needs an IdP (Cloudflare Access / Entra / Keycloak) — infra.

**Do before any deployment that is reachable beyond localhost.**

---

## 2. Containerised deployment — never run, several fixes unverified

The Docker/compose path has never been exercised end to end (Docker Desktop is not running on the dev
machine). Three defects were found by reading and **fixed in the files** today, but none is verified:

| Fix | File | What to check when Docker exists |
|---|---|---|
| `BACKEND_URL` baked at build time — Next `rewrites()` runs at `next build`; the frontend image proxied `/api` to its own localhost | `frontend/Dockerfile` (build-stage `ARG`), `docker-compose.yml` (`build.args`) | `docker compose up --build`, run a triage on :8080, confirm the backend logs a `POST /api/triage/stream` |
| Hours snapshot missing from the image — `.dockerignore` excluded `data/`, so the closed-clinic filter was silently disabled in the container | `backend/Dockerfile`, `backend/.dockerignore` | `docker compose exec backend ls /app/data/gpgowhere_hours.json`; logs must not show the new "hours snapshot missing" warning |
| Alertmanager inhibition never matched — aggregate alert rules dropped the `service` label | `monitoring/alert.rules.yml`, `monitoring/alertmanager.yml` | Stop the backend with the stack up; only `BackendMetricsDown` should page at `:9093` |

Also still open from [[Open Work 2026-09-01]] §4: Alertmanager receivers are empty by design; a real
webhook goes in a mounted file (`*_file` option), never in the committed YAML.

---

## 3. GitLab runner — CI edits that cannot be verified locally

Edited today, unverifiable without a runner:

- `build:images` — added `DOCKER_TLS_VERIFY=1` and `DOCKER_CERT_PATH=/certs/client`; without them the
  docker CLI cannot complete the TLS handshake to dind on :2376, so image build and both container scans
  (`allow_failure`) were silently skipped. Needs a runner with the docker executor and privileged dind.
- `scan:trivy-fs` — now `needs: []` / `cache: []` like gitleaks, so the blocking scan no longer covers the
  54 MB model pickle or the pip cache. Confirm the job still finds the source tree.
- `data:version` — now runs `export_dataset --check` against the **committed** `meta.json` before
  regenerating, so a generator change that nobody re-pinned fails the pipeline instead of passing by
  construction. First run tells you whether the committed snapshot still reproduces (it does locally).

**The pipeline itself:** several pushes today (`afbd09e` onwards). None has been looked at.
The gitleaks job now prints its finding in the log if it fails.

---

## 4. DVC remote — deferred by decision (2026-09-02)

The pointer is re-pinned and correct (`b7db306`), but `.dvc/config` is empty: `dvc push` has nowhere to
go, `dvc pull` cannot restore the `.npz` on a fresh clone, and CI never invokes `dvc` at all (it
regenerates from code). James decided to wait for infrastructure. When a target exists:

```bash
cd backend
dvc remote add -d origin <s3://bucket/path | gdrive://<folder-id> | ssh://host/path>
git add .dvc/config && git commit -m "chore(dvc): add remote"
dvc push
```

Credentials in `.dvc/config.local` (gitignored) or CI variables. Then add `dvc pull` to `data:version`
and point `test:data-lineage` at the pulled file — that is what turns Pillar 2 from "reproducible" into
"versioned".

---

## 5. MLflow in CI — inert until variables exist

Training tracks to `backend/mlflow.db` locally, which is never exported as a job artifact, so runs do not
survive between CI jobs. `deploy:promote-production`'s champion-challenger lookup therefore always prints
"no champion metrics available … proceeding". Set `MLFLOW_TRACKING_URI` and `MLFLOW_TRACKING_TOKEN` as
protected CI variables pointing at GitLab's MLflow-compatible registry; `CAREROUTE_PIPELINE_TRIGGER_TOKEN`
for the retrain trigger. See [[Roadmap]] "Wire MLflow → GitLab registry".

---

## 6. Evidently is installed but broken in this environment

`import evidently.report` raises `TypeError: multiple bases have instance lay-out conflict`
(evidently 0.4.33 predates pydantic 2.10 / Python 3.13). The monitor now logs this at WARNING as "installed
but failed to import" instead of the old INFO "not available", and records the backend in the drift
report — but the primary monitoring path still has never run. Bumping the pin needs an environment to
verify it in (the CI image is Python 3.12-slim, which may differ from the dev box). Until then the PSI
fallback is the only drift signal — which is why fixing its binary-feature bug today mattered.

---

## 7. OneMap client — live behaviour unverified

Fixed in code today with mocked tests: token expiry now honours the server's `expiry_timestamp`, a 401
triggers one re-authentication and retry, non-JSON error bodies raise `OneMapError`, and auth is
lock-protected across `to_thread` workers. The live re-auth path has not been exercised against the real
API (it needs a token to expire or be revoked). Also: Marcus's OneMap password was shared in the team
chat on 2026-09-01 and should be rotated; everyone then updates the repository-root `.env`.

---

## 8. Trusted proxies — needs the deployment topology

Rate limiting no longer trusts `X-Forwarded-For` from anyone (`CAREROUTE_TRUSTED_PROXIES` defaults to
empty). When the backend sits behind a load balancer or reverse proxy, set that variable to the proxy's
IP/CIDR, otherwise every client rate-limits as the proxy's address.

---

## 9. TLS interception on the dev machine (from [[Open Work 2026-09-01]] §5)

Identified today as Cloudflare Gateway ("Gateway CA - Cloudflare Managed G1"). Python was fixed with
`pip-system-certs` in `backend/.venv`. Git still runs with `http.sslVerify=false` and Playwright cannot
download browsers. Export that root CA from the Windows store to a PEM and point `http.sslCAInfo`,
`NODE_EXTRA_CA_CERTS` and `REQUESTS_CA_BUNDLE` at it — a security setting, so a human does it.

---

## Verified good, no action

- The DVC pointer, `meta.json` and the on-disk `.npz` agree byte-for-byte; the model's recorded training
  hash equals the dataset hash.
- Training is fully seeded; the release gate is deterministic (red-flag recall margin is thin, 0.9572 vs
  0.95, but not flaky).
- No secrets tracked in git; both Dockerfiles are multi-stage and non-root; `.env` at the repo root is
  gitignored.
