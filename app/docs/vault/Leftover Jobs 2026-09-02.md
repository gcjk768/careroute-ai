---
tags: [handover, todo, careroute]
updated: 2026-09-02
---
# Leftover Jobs — 2026-09-02

Back to [[Home]]. Related: [[Infra-Dependent Work 2026-09-02]] · [[Open Work 2026-09-01]] · [[Changelog]] · [[Roadmap]]

Everything still open at the end of the 2026-09-02 session, in the order to do it. Branch
`integration-all-agents-2026-08-31` is pushed and clean (`git log -1` for the head). Tick items off here as they close.

---

## A. Needs James — do these first

- [ ] **A1. Look at the GitLab pipelines.** Nine pushes today (`afbd09e` → `68357c4`), none inspected.
  `7f6171e` (the audit) is the largest change and the first where `test:e2e` is BLOCKING. Also tells you whether
  `scan:secrets-gitleaks` finally passes (it now prints the finding in the job log if not).
  → Pipelines page for the branch.
- [ ] **A2. Open the merge request to `main`.** `main` has had no commits since 2026-07-21; every
  teammate's branch is inside the integration branch and verified (770 backend tests, 20 e2e specs).
  Use the top [[Changelog]] entries as the description; ask Marcus (routing) and Heriz (HITL/handoff)
  to review their agents. GitLab prints the link on every push.
- [x] **A3. Decide the authentication mechanism.** ~~`/api/escalations*`, `/api/cases/*/audit`,
  `/api/sessions/*` and the decision POST are open to anyone who can reach port 8000 — patient free
  text included.~~ **Done 2026-09-15 (`e6530d9`)** exactly as the quick option described: a shared
  key `CAREROUTE_STAFF_API_KEY` as a FastAPI dependency (`X-Staff-Key`, constant-time compare), sent
  by a server-side Next Route Handler so the browser never holds it. Open when unset, and
  `/api/health` reports `staffAuth: "open"` so that state is never silent. **Set the variable on both
  services before any deployment reachable beyond localhost.** Real clinician identity still needs
  an IdP — see [[Changelog]] 2026-09-15.
- [ ] **A4. Ask Marcus to rotate the OneMap password.** It was pasted into the team chat on 2026-09-01.
  Free account; everyone then updates `ONEMAP_EMAIL` / `ONEMAP_PASSWORD` in the **repository-root** `.env`
  (that is the file the app loads — not `backend/.env`).

---

## A-bis. Added 2026-09-12 by the courseware pass — needs James

Context and full gate mapping in [[Courseware Alignment]].

- [ ] **A5. Decide on GitHub Actions.** Deck 02's appendix and a separate `githubAction.pdf` teach it;
  this project runs GitLab CI with 69 jobs. Mirroring those into a second CI system is a real,
  permanent maintenance cost for what is essentially a portability demo. Options: (a) skip and say why
  in the report, (b) port a *representative slice* (lint → train → gate → test) as `.github/workflows/`
  to show the concept without duplicating the whole pipeline, (c) full mirror. **(b) is the cheap
  honest answer** if the marking rubric wants GitHub Actions represented.
- [ ] **A6. Look at the new pipeline run.** Six gates added today are BLOCKING and have never been
  executed by a runner: `ai-security:guardrail-score`, `scan:pii-egress`, `scan:no-live-credentials`,
  `test:agent-graph`, plus `notify:pipeline-outcome` and `deploy:canary-production`. The YAML parses
  and every `needs` resolves, but only GitLab can confirm the images pull and the flags are right.
- [ ] **A7. Set `CAREROUTE_NOTIFY_WEBHOOK`** (any Slack-compatible incoming webhook) if you want the
  pipeline outcome pushed rather than printed in the job log. Matters most for the scheduled retrain,
  which nobody watches at 02:00.

## B. Needs infrastructure — edited in the repo, unverified until the infra exists

Each row has the exact check in [[Infra-Dependent Work 2026-09-02]].

- [ ] **B1. Docker Desktop running** → `docker compose up --build`, then verify three fixes:
  frontend `/api` proxy actually reaches the backend container (`BACKEND_URL` build arg); the backend
  image contains `/app/data/gpgowhere_hours.json`; Alertmanager inhibition works when the backend is
  stopped (only `BackendMetricsDown` pages at `:9093`). Alertmanager receivers are still empty by design —
  a real webhook goes in a mounted file.
- [ ] **B2. GitLab runner with docker executor + privileged dind** → verify `build:images` now completes
  the TLS handshake (`DOCKER_TLS_VERIFY` / `DOCKER_CERT_PATH`), `scan:trivy-fs` still scans the source
  tree after `needs: []` / `cache: []`, and `data:version --check` passes on the committed snapshot.
- [ ] **B2-bis. Training-serving skew gate** (do this *before* Feast — [[Courseware Alignment]]).
  Train/serve consistency currently rests on both paths importing the same `ml/features.py`, which is
  the property a feature store buys; nothing asserts the two have not drifted apart. An explicit skew
  gate is the cheap version and needs no new infrastructure.
- [x] **B3. DVC remote** — **Done 2026-09-15.** No bucket needed: the remote is this project's own
  GitLab **generic package registry** (`https://gitlab.com/api/v4/projects/84456994/packages/generic/dvc`),
  used through DVC's HTTP remote type (`method PUT`, `auth custom`, header `PRIVATE-TOKEN`). Committed in
  `.dvc/config`; the token lives in `.dvc/config.local` (gitignored). `data:version` now does
  `dvc pull` before the reproducibility check and `dvc push` after the export, authenticating with
  `CI_JOB_TOKEN` via a `--local` override. **Remaining manual step:** the first push needs a personal or
  project access token with the `api` scope — see README §12.
- [ ] **B4. MLflow in CI** — set `MLFLOW_TRACKING_URI`, `MLFLOW_TRACKING_TOKEN` (protected CI
  variables) and `CAREROUTE_PIPELINE_TRIGGER_TOKEN`. Until then the champion-challenger step and the
  retrain trigger are inert.
- [ ] **B5. Evidently pin** — `evidently==0.4.33` fails to import on pydantic 2.10 / Python 3.13 (now
  logged at WARNING instead of "not available"). Bump and verify in an environment that matches the CI
  image (`python:3.12-slim`). Until then the PSI fallback is the only drift signal.
- [ ] **B6. Trusted proxies** — when the backend sits behind a load balancer, set
  `CAREROUTE_TRUSTED_PROXIES` to its IP/CIDR, otherwise every client rate-limits as the proxy.
- [ ] **B7. TLS certificate on the dev machine** — the interceptor is Cloudflare Gateway. Export its root
  CA from the Windows store to a PEM; point `git config http.sslCAInfo`, `NODE_EXTRA_CA_CERTS` and
  `REQUESTS_CA_BUNDLE` at it; unset `http.sslVerify=false`. Fixes the git warning, Playwright browser
  downloads, and lets CI drop the pre-baked browser image. Security setting — a human does it.

---

## C. Watch, no action

- **Red-flag recall margin** is 0.9572 against a 0.95 release gate. One data or feature change from
  failing. Be careful editing `app/ml/data.py` / `features.py`.
- **Two pre-existing lint notes** outside the CI gates: an unsorted import block in
  `tests/agents/test_routing.py` / `test_reflection.py`, and the custom-font warning in
  `frontend/app/layout.jsx`. Harmless.

---

## D. Roadmap (larger, from [[Roadmap]])

- Grafana dashboard over `/metrics`; MLflow → GitLab registry (B4 is the prerequisite); clinician
  final-acuity picker; clinician handoff summary in the dashboard; Onyx CE or pgvector retrieval.

---

## Done today, for the record (see [[Changelog]])

Merged Marcus's OneMap failsafe · fixed the browser SSE hang at its root (Next's 30 s proxy idle timeout;
backend heartbeat) · made `test:e2e` blocking · re-pinned the DVC pointer · alternative-clinics panel in
the UI · Reflection tests 3 → 14 · whole-codebase audit: 48 findings, all code-only ones fixed
(cross-patient state bleed, fabricated results on HTTP errors, inert drift detection, container wiring,
privacy leaks, and ~30 more) · full backend suite 770 passed, e2e 20 passed · README brought up to
date (test counts, 85% coverage remeasured, gates, e2e status, config, API contract) · architecture
diagram rebuilt in draw.io (Symptom-Intake as orchestrator, 7 agents, routing tools, per-request
worker set, keepalive) and re-exported to PNG/SVG · this tick-list.

## Local environment notes

- The app loads `.env` from the **repository root**; it is gitignored there now.
- Live OneMap calls need `pip-system-certs` in `backend/.venv` (installed) because of the TLS interceptor.
- Run the full backend suite as CI sees it:
  `.venv/Scripts/python.exe -m pytest tests -q --ignore=tests/test_routing_live.py` from `backend/`
  (~14 min). The suite's kill switch keeps the eval fixtures on the deterministic path.
- Playwright uses your installed Chrome (`channel: 'chrome'`); it cannot download its own browsers here (B7).
