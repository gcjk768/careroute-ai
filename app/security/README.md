# Security scanning — CareRoute AI

All scanners below are **free / open-source** and wired into the CI/CD pipeline
([`.gitlab-ci.yml`](../.gitlab-ci.yml) — GitLab CI is the only pipeline; there is
no `.github/` directory). Together they cover the AI-specific *and* classic
application-security surface for the MLSecOps aspect.

Every scanner runs on **every push**. Several deliberately overlap — four secret
scanners, four dependency scanners, two IaC scanners, four DAST scanners — because
each carries a different rule corpus or advisory database, so a finding one misses
another catches. The full per-tool table (gate, artifact, why it is not redundant)
is in the root [README](../README.md#every-security--ai-scanner-detailed).

## AI / LLM red-teaming (the distinguishing MLSecOps layer)
| Tool | Role | Cadence in CI |
|------|------|---------------|
| **our pytest guardrail suite** | prompt-injection is blocked; the deterministic safety-override is un-overridable | every run (always-green gate) |
| **Promptfoo** (MIT) | LLM red-team **regression against the live app** — every attack is POSTed to `/api/triage/stream`, so it must get past the input guardrail, redaction and output screen (`promptfooconfig.yaml`, `promptfoo_sse_transform.js`) | every push / PR (needs `OPENAI_API_KEY` for promptfoo's own attack generation + grading) |
| **Garak** (NVIDIA, Apache-2.0) | full LLM **vulnerability scan** — 120+ probes ("nmap for LLMs"): prompt-injection, encoding, DAN, leak-replay, toxicity, malware-gen. **Targets the raw provider model**, not the app | per release / `main` (manual — 10-30 min) |
| **DeepTeam** (Confident AI, Apache-2.0) | **OWASP LLM Top 10** mapped red-team (`deepteam_scan.py`). **Targets the raw provider model with a stand-in system prompt**, not the app | manual |
| **PyRIT** (Microsoft, MIT) | novel **multi-turn** attack orchestration (Crescendo / TAP). **Placeholder job — not yet implemented** | scheduled / quarterly |

**Which of these test CareRoute, and which test the vendor?** Only the pytest
guardrail suite, `guardrail_score` and (since 2026-09-15) Promptfoo exercise the
application's own controls. Garak and DeepTeam measure the *underlying model's*
resilience, which is useful for provider selection but says nothing about the
guardrail in front of it; read their reports that way. Complementary runtime
control: **LLM Guard** in front of the model (the deterministic guardrail here
plays that role).

## Responsible-AI gate
| Tool | Covers | Where |
|------|--------|-------|
| **Fairlearn** (course tool) | subgroup **accuracy-parity gate** on the deployed model — fails if it regresses to an unfair (age-blind) model | `backend/tests/test_fairness_gate.py`, CI `ai-security:fairness-gate` |

## Classic AppSec scanners
| Tool | Covers |
|------|--------|
| **Gitleaks** | committed secrets / API keys |
| **Semgrep** | SAST (Python + JS security rulesets) |
| **Bandit** | Python SAST |
| **Trivy** | dependency CVEs + secrets + IaC misconfig (filesystem), and container images |
| **pip-audit / Safety / npm audit** | Python + Node dependency vulnerabilities (SCA) — *Safety & the model scan below are from the AI-Security course notes* |
| **modelscan** (ProtectAI) | malicious code in serialized ML models (pickle/torch/keras deserialization) |
| **OWASP ZAP** | **DAST** — dynamic scan of the running API (set `DAST_TARGET` / auto-started in GitHub Actions) |
| **Hadolint** | Dockerfile best-practice / security lint (source-level) |
| **TruffleHog** | secrets, then **verifies** each candidate against the live provider — a VERIFIED hit is an active leaked credential, not a guess |
| **detect-secrets** (Yelp) | secrets against a committed **baseline**, so only findings *new* since triage are reported |
| **GitLab Secret Detection** | GitLab's native secret analyzer — **runs on the Free tier** (only the MR security widget needs Ultimate) |
| **GitLab SAST** | GitLab's native SAST analyzer selection — likewise Free-tier |
| **Ruff (`S` ruleset)** | the Bandit rule family over `backend/scripts` + `backend/tests`, which the Bandit job does not scan |
| **njsscan** | Node/Next.js-specific SAST (template injection, insecure crypto, framework misconfig) |
| **eslint-plugin-security** | frontend SAST, run with an inline config so `npm run lint` keeps its current meaning |
| **Horusec** | multi-language SAST aggregator — cross-checks the single-language scanners |
| **SonarQube** (course tool) | code quality + security hotspots. The one scanner that cannot be self-contained (its CLI always uploads to a server), so the job is gated on `SONAR_TOKEN` + `SONAR_HOST_URL`; **SonarCloud is free for public projects** |
| **OSV-Scanner** (Google) | dependency CVEs against osv.dev — a different advisory corpus from Trivy/pip-audit |
| **Grype** (Anchore) | dependency CVEs against Anchore's feed — third opinion on the same dependency set |
| **OWASP Dependency-Check** | dependency CVEs against the NVD; slow cold (full feed download), cached between pipelines |
| **Retire.js** | known-vulnerable JS libraries that are **bundled or vendored** — which lockfile scanners miss |
| **Checkov** | IaC/config policy over Dockerfiles, `docker-compose.yml` and **`.gitlab-ci.yml` itself** (this repo has no `.tf` files) |
| **KICS** (Checkmarx) | the same config targets as Checkov, with an independent query set |
| **pip-licenses / license-checker** | **licence compliance** — a copyleft dependency in a clinical product is a release blocker of a different kind |
| **Fickling** (Trail of Bits) | decompiles the **pickle program** inside the model artifact and reports what it would execute on load — the depth complement to modelscan |
| **ZAP api-scan** | API-aware DAST driven by the OpenAPI schema at `/openapi.json` — for an API with no crawlable HTML, this is the ZAP mode that carries signal |
| **ZAP full-scan** | active attack scan (injection, traversal, XSS), safe because the target is a throwaway backend booted inside the job |
| **Nikto** | web-server misconfiguration — dangerous methods, leaked headers, stale files |
| **Nuclei** (ProjectDiscovery) | community template-corpus sweep — breadth-first, complementing ZAP's depth |
| **Trivy (image)** | **container-image** scan of the BUILT backend + frontend images — OS + Python/Node CVEs, secrets, misconfig (`scan:container-image`, scans the exported image tars) |
| **Dockle** | container-image **CIS hardening** lint — no root user, no secrets baked into layers, minimal capabilities (`scan:container-dockle`) |

> Tools tagged *course tool* / *course notes* were sourced from the module notes
> (AI & Cybersecurity, Integrating & Deploying, Explainable & Responsible AI):
> Safety, modelscan, OWASP ZAP, SonarQube, and Fairlearn/AIF360. **All five are now
> wired in**; SonarQube is the only one that needs configuration before it runs
> (see its row above).

## Performance & API robustness
| Tool | Covers | Where |
|------|--------|-------|
| **Locust** | **load test** against a backend booted inside the job — p95 latency + error-rate SLOs. Advisory until a CI baseline exists; the check itself is real (verified failing on breach) | [`backend/tests/load/locustfile.py`](../backend/tests/load/locustfile.py), CI `test:load-locust` / `loadtest:staging` |
| **Schemathesis** | **property-based API fuzzing** generated from our own OpenAPI contract: the server must never 500 and never violate its declared schema. Excludes the SSE endpoint, which streams by design | CI `test:api-fuzz-schemathesis` |

The four DAST scanners and both runtime tests **boot their own disposable backend**
(no LLM key is configured in CI, so the agents take their deterministic path and the
app is healthy without any secret). That is why they produce evidence on every
pipeline instead of waiting on a deployed target — the gap that left the original
`dast:owasp-zap` dormant, since `DAST_TARGET` was never set.

## Scored agentic gates (added with the DOAIS courseware pass)

These are the gates the course's agentic CI quality table names, in the form it names
them — RATES over labelled data, not pass/fail over hand-picked payloads. Full
gate-by-gate mapping in [`docs/vault/Courseware Alignment.md`](../docs/vault/Courseware%20Alignment.md).

| Gate | Threshold | Covers | Where |
|------|-----------|--------|-------|
| **Guardrail effectiveness (E9)** | injection bypass ≤ 2%, recall ≥ 0.95, false positives ≤ 0.05 | Scores `guardrail.screen()` over a 55-case labelled corpus (37 attack / 18 benign). **Both halves matter**: a guardrail that blocks everything scores perfect recall and turns patients away | [`app/evals/guardrail_score.py`](../backend/app/evals/guardrail_score.py), CI `ai-security:guardrail-score` |
| **PII egress** | 0 events | Every artifact that LEAVES the trust boundary — inference log, ground-truth log, drift report, the report's Markdown twin. Detector is `redact.redact()` itself, so the gate cannot drift from the redactor it audits. Presidio adds NER for names/addresses when installed | [`app/ml/pii_egress.py`](../backend/app/ml/pii_egress.py), CI `scan:pii-egress` |
| **No live credentials** | 0 outside production | Agent-reachable live credentials only (OneMap, LLM provider, Onyx). CI plumbing tokens are deliberately excluded | [`app/credentials_guard.py`](../backend/app/credentials_guard.py), CI `scan:no-live-credentials` |
| **Agent graph + loop safety** | every node reachable; ≤ 9 worker steps | Measured from a real `orchestrate()` run, not a mock | [`tests/test_agent_graph.py`](../backend/tests/test_agent_graph.py), CI `test:agent-graph` |
| **Canary auto-rollback** | error < 1%, tool failures < 2% | 5/25/50/100 ramp; **fails closed** — a step with missing metrics aborts, because an empty scrape and a healthy service are the same empty dict | [`app/ml/canary.py`](../backend/app/ml/canary.py) (thresholds). On AWS the error-rate gate is a CloudWatch alarm ECS rolls back on (`careroute_ai_infra`, `deploy_safety.tf`); the tool-failure gate is not wired there yet |

**Two artifacts that must never leak what they find.** The PII findings file stores the
**redacted** line as its excerpt, and the credential guard reports credential **names**
only. Both are downloaded by dependent jobs and kept as CI artifacts; a findings file
that quoted the NRIC it caught would have re-published it into exactly the store the
gate exists to protect. Masking by construction, not by remembering to mask.

**What scoring found.** The pass/fail guardrail job had always been green. Scoring the
corpus put the false-positive rate at **11%**: `"sprained my ankle playing football"`
was rejected as off-scope (the clinical lexicon had no joints or extremities at all)
and `"i forget everything when the migraine starts"` was rejected as prompt injection.
Both fixed in `app/guardrail.py`; the imperative attack forms still block.

## Running locally
```bash
# LLM red-team (needs OPENAI_API_KEY)
npm install -g promptfoo && promptfoo redteam run -c security/promptfooconfig.yaml
pip install garak && garak --model_type openai --model_name gpt-4o-mini --probes promptinject,encoding,dan
pip install deepteam && python security/deepteam_scan.py

# AppSec
gitleaks detect --source . --no-git
pip install semgrep && semgrep scan --config auto backend frontend/src
trivy fs --scanners vuln,secret,misconfig .

# Added scanners
pip install njsscan detect-secrets && njsscan frontend && detect-secrets scan --all-files
docker run --rm -v "$PWD:/src" trufflesecurity/trufflehog:latest filesystem /src
docker run --rm -v "$PWD:/src" bridgecrew/checkov -d /src --framework dockerfile docker_compose gitlab_ci
docker run --rm -v "$PWD:/src" ghcr.io/google/osv-scanner --lockfile /src/backend/requirements.txt
npx retire --path frontend

# Load test + API fuzzing (needs the backend running on :8000)
pip install locust schemathesis
locust -f backend/tests/load/locustfile.py --headless -u 20 -r 5 -t 60s --host http://127.0.0.1:8000
st run http://127.0.0.1:8000/openapi.json --checks all --exclude-path-regex '/api/triage/stream'

# Scored agentic gates (all offline — no provider, no secret, no network)
cd backend
python -m app.evals.guardrail_score          # E9: bypass / recall / false positives
python -m app.ml.pii_egress                  # PII egress over the published artifacts
CI=true python -m app.credentials_guard      # arms the non-production credential rule
pytest tests/test_agent_graph.py tests/test_canary.py -q
```
