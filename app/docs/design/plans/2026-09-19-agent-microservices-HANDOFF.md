# Agent microservices — handoff (all 9 tasks DONE)

Tasks 1-4 were written at the end of the office session on 2026-09-19; Tasks 5-9 were
completed later the same day. **Nothing in this plan is outstanding.** The remaining work is
the separate infra plan in `careroute_ai_infra` (see "Next" below).

- **Spec:** `docs/design/specs/2026-09-19-agent-microservices-design.md`
- **Plan:** `docs/design/plans/2026-09-19-agent-microservices.md`
- **Branch:** `SIT` (everything below is on it, **unpushed**)

## Done

| Task | What | Commits |
|---|---|---|
| 1 | transport config, `AgentUnavailableError`, wire format | `ccf057f` |
| 2 | one agent per service: `/v1/invoke`, probes, `serve.py` | `f07d036`, `066330f` |
| 3 | `RemoteAgent`: lane + COMMS checks, breaker, one retry | `7c37749`, `2726688` |
| 3b | Grafana panels for `careroute_agent_calls_total` | `2610374` |
| 4 | orchestrator over either transport; degradation; parity test | `d77e49e`, `06055cc` |
| 5 | `llm-gateway` (only key holder) + `rag-service` | `9c2a4dd` |
| 6 | Redis write-through: escalations + audit survive a restart | `a86da84` |
| 7 | one image per agent, Compose, `AgentDown`, smoke script | `6d4cd61`, `4000726` |
| 8 | CI image matrix, scans, push, compose smoke | `9e9962a` |
| 9 | ARCHITECTURE §5a, spec fixes, vault | `e6134a0` |

## Verification (all re-run after the last commit)

- Full backend suite: **1315 passed / 18 skipped / 1 xfailed** (13m19s). Run it with
  `python -m pytest -q` from `backend/` (the suite's kill switch keeps every LLM call on the
  deterministic path). Redirect to a file rather than piping through `tail` — a pipe buffers and
  the output looks stalled for the whole run.
- `ruff check app` clean. (The blocking CI gate is `backend/app` only; `backend/tests` is
  scanned non-blocking, and every test file trips `S101` by design.)
- `promtool check rules monitoring/alert.rules.yml`: 16 rules, SUCCESS.
- All **12** Docker targets build; the in-build model release gate passes.
- `docker compose up -d --wait intake-gateway`: all **10** containers healthy, only `:8000`
  published.
- `python scripts/compose_smoke.py --base-url http://localhost:8000`:
  `OK: escalated=True acuity=P1_RESUSCITATION` with a successful call recorded to all six
  agent containers.

## Decisions made during Tasks 1-4 (still authoritative)

1. **RemoteAgent validates the whole reply before recording success.** A non-dict body, a
   non-dict `result`/`carry`, a non-bool prescreen result, a bad announcement — all raise
   `AgentUnavailableError`. Exception messages carry only slug, op and exception type.
2. **A down Reflection escalates and keeps the tier floor** — never lowers the care tier.
3. **A decision agent whose `run` succeeds but whose announcement fails also escalates.**
4. **The legacy `backend` image is still built, scanned and pushed** (done in Task 8): the live
   ECS service deploys the `monolith` default target until the infra plan switches that task
   definition.
5. **Infra repo dashboard copy** — regenerated (it was 5.5 KB from Sep 11 vs the current
   97.7 KB / 79 panels, which would have failed its `config_drift` job) but **left uncommitted**
   in `careroute_ai_infra`, which is on branch `test-with-floci`. Commit it there before that
   pipeline runs.

## Decisions made during Tasks 5-9

6. **The two shared services fail differently on purpose.** A dead `llm-gateway` raises
   `LLMUnavailableError` (every caller already falls back to deterministic logic); a dead
   `rag-service` returns `None` and retrieval runs locally, because a case must never lose its
   citations.
7. **Both requirements manifests are resolved in ONE pip run** in the `rag-builder` stage, and
   `RUN pip check` guards it. A second, separate `pip install` resolves only its own file and
   upgraded `starlette` past `fastapi`'s constraint, which killed every FastAPI app in that venv.
8. **`rag-service` removes the base venv before copying its own.** COPY merges into an existing
   directory, so a venv copied over a venv leaves two versions of every differing package.

## Still outstanding (deliberately)

- **Deferred minors, for a final review pass:** `_timed_sync` in `orchestration.py` is now
  unused; `observe_agent` gained a `source="unavailable"` label value.
- Nothing is pushed. 10 commits sit on local `SIT` ahead of `origin/SIT`.

## Next

The infra plan in `careroute_ai_infra`: ECR repos for the 12 images; the backend `ecs_service`
task definition takes 10 containers with `dependsOn` HEALTHY, per-container secrets (OpenAI only
on `llm-gateway`, OneMap only on `routing-agent`), `essential` only on intake-gateway,
safety-agent and redis; 2 vCPU / 8 GB; `scheduled_task` runs `ml-jobs` for batch-scorer and
drift-monitor; Prometheus targets `:8000,:8101-8108` through Cloud Map; nightly Redis snapshot to
the artifacts bucket.

## Deployment shape (for the presentation)

Two ECS services: **backend** = one task with ten containers (intake-gateway, classifier, safety,
routing, reflection, hitl, handoff, llm-gateway, rag-service, redis), **frontend** = unchanged.
One compute for the backend; splitting into one ECS service per agent later changes only the task
definition.
