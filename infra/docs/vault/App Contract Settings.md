---
tags: [active, careroute, infra]
updated: 2026-09-29
---
# App Contract Settings

Back to [[Home]]. Related: [[App Overview]] · [[Changelog]]

Settings in `modules/careroute_stack` that read like tuning knobs but are
**correctness constraints**. Each one has a specific failure mode that looks
like an app bug when it is really an infra default.

§1–7 are imposed by the *application*. §8 is the one imposed by *AWS* — same
shape of trap, different source, so it lives here rather than in its own note.

## 1. `trust_alb_as_proxy` → `CAREROUTE_TRUSTED_PROXIES`

The app's rate limiter keys on the direct peer address and honours
`X-Forwarded-For` **only** from a peer listed in `CAREROUTE_TRUSTED_PROXIES`
(`main.py::_client_key`). The app default is "trust nobody", which is correct
for a container exposed directly and catastrophic behind a load balancer: the
peer is always an ALB node, so every patient in the world shares one
30-request/minute bucket. The second concurrent user gets a 429.

The stack injects the ALB's own subnet CIDRs. Wider trust than a single IP, but
the only other things in those subnets are our own tasks.

**Failure signature:** "the demo started returning 429 when two people used it".

## 2. `alb_idle_timeout` + `sse_heartbeat_seconds`

`POST /api/triage/stream` is Server-Sent Events held open for the whole triage
run. The app measured silent stretches of 24–30 s (Care Routing's OneMap
shortlist; the red-flag LLM path) and emits a comment frame every 10 s to reset
idle timers on the path. The ALB default of 60 s is too close to that; 120 s
leaves headroom even if the heartbeat is disabled.

**Failure signature:** the UI hangs on "Triaging…" with no error in any log.

## 3. API Gateway is incompatible with the triage UI

An HTTP API integration caps at 30 s and buffers the response body rather than
streaming it, so it cuts the SSE stream — the patient console's only path to a
result. Guarded by `accept_api_gateway_sse_break`.

This also means the obvious fix for [[Clinical Endpoint Auth Gap]] — a JWT
authorizer on API Gateway — cannot be applied while triage streams through it.

## 4. One backend task, always

`backend/app/store.py` keeps cases, escalations, session history, fairness and
the audit trail in **process memory**. Two tasks behind the ALB means an
escalation raised on task A is invisible to a clinician whose
`/api/escalations` call lands on task B, and a patient resuming a clarification
gets a 404 half the time. Nothing errors — cases just disappear.

Enforced by a precondition on `terraform_data.guards`.

**Do not treat the old escape hatch as met.** Since 2026-09-19 `store.py` *does*
have a shared backing store — `app/persistence.py` write-throughs to Redis — and
one task is still the only safe count. Redis made a **restart** lossless, not a
second task correct: memory stays authoritative while a task runs and is
reloaded only at start-up, so an escalation task A writes after task B booted is
still invisible to B. Multi-task needs store.py to **read** through to the store
per request. A second precondition asks for a shared store before
`allow_multi_task_backend` is honoured, but note it checks that *some* store
exists, not that the app uses *that* one: enabling DynamoDB satisfies it while
the app talks to Redis, which is the same data loss plus one billed resource.

## 5. `backend_memory >= 1024`

The backend image is not a thin API: scikit-learn, numpy and SHAP, with a
severity model baked in at build time and warmed at boot. The app's own
docker-compose gives it 1.5 vCPU / 1536 MB. At 512 MB the warmup OOM-kills the
task, which presents as an ALB health check that never turns green. Enforced by
a variable validation.

## 6. Safety NLP needs an image built for it, and the memory to load it

Since 2026-09-29 the app's `build:images` builds `careroute-backend` with
`WITH_SAFETY_NLP=1` and `WITH_SAFETY_NLP_DIRECT_NLI=1` (torch + NER, assertion,
BioLORD and mDeBERTa, pinned and hash-checked at build time). Against an image
built without them, `enable_safety_nlp = true` still triages correctly on the
deterministic rules, and `/api/health` says `safetyNlp="rules"`, not `"model"`.
`enable_safety_nlp` needs `backend_memory >= 3072` (`>= 4096` with
`safety_nlp_direct_nli`), enforced by a precondition in `terraform_data.guards`.
Measured on the release image with all four models: 1.85 GiB idle, 2.14 GiB peak.
Roll out with `safety_prototype_semantic_activation = false` first (shadow only),
then a second revision with it `true`; rollback = the previous revision.

## 7. The telemetry paths must include the CLINICIAN LABELS

`local.telemetry_path_env` in `modules/careroute_stack/main.tf` points the app's
telemetry at the shared `ml-telemetry` volume. It listed three paths and needed
four: `CAREROUTE_INFERENCE_LOG`, `CAREROUTE_MONITOR_DIR`, `CAREROUTE_REPORT_DIR`
— and **`CAREROUTE_GROUND_TRUTH_LOG`**, which was missing.

`app/ml/monitor.py` computes **concept** drift by joining the inference log
against `ground_truth.jsonl` by case id — those rows are the clinician HITL
decisions, the only labels the system ever gets. Unset, the app fell back to its
in-image default (`backend/monitoring/ground_truth.jsonl`), which is *not* on the
volume: the service wrote labels into its own container filesystem, they died
with the task, and the scheduled monitor read a path that was always empty.

The app's own `docker-compose.yml` sets this variable on all three services, so
Compose computed concept drift correctly and only the deployed stack could not —
which is the worst shape for a gap like this, because local runs look fine.

**Failure signature:** drift reports that come back green forever, with no
labelled rows and no error. A whole MLOps pillar reporting success because its
input never arrived. Fixed 2026-09-19; see [[Changelog]].

## 8. `secret_recovery_window_days = 0` — imposed by AWS, not by the app

A deleted Secrets Manager secret **keeps its name reserved** for its recovery
window, and AWS defaults that window to 30 days. Every secret in this stack has
a fixed, derived name (`careroute-demo/openai-api-key`, `…/onemap`,
`…/onyx-api-key`, `…/grafana-admin-password`, `…/db-credentials`), so the
sequence this whole repo is built around — apply, demo, **destroy**, apply again
tomorrow — fails on the *second* apply:

```
InvalidRequestException: You can't create this secret because a secret
with this name is already scheduled for deletion.
```

Two things make this worse than a normal footgun. It fails at **apply**, which
is the one step you run live. And the nightly `scheduled_destroy` safety net —
the thing that exists to protect you — is what arms it, so the more carefully
you follow the repo's own cost advice the more certain the next morning's
failure becomes.

`0` deletes immediately, which is correct *here* and only here: these secrets
are re-created from CI variables on every apply, so there is nothing to recover.
Raise it to 7–30 for any environment where the secret is the only copy. AWS
rejects everything between 1 and 6, which the variable's validation mirrors.

**Failure signature:** an apply that worked yesterday fails today on a secret,
and the error reads like an AWS outage rather than a timer you started.
