---
tags: [moc, careroute, infra]
updated: 2026-09-17
---
# Home — CareRoute AI Infrastructure

Navigation hub (MOC) for the **IaC repo**. The application has its own vault at
`../../../careroute_ai_app/docs/vault/`; this one covers only what provisions
AWS.

## Start here

- [[App Overview]] — what this repo deploys, module by module, and which
  application behaviour each piece exists for
- [[App Contract Settings]] — the settings that look like tuning knobs and are
  actually correctness, because of how the app behaves
- [[Monitoring Stack Design]] — why Prometheus/Grafana run in AWS, and the three
  non-obvious decisions inside that module
- [[Lecture Alignment]] — this repo measured against the DOAIS courseware decks
- [[Changelog]] — what changed, when, and why
- [[Roadmap]] — what is deliberately not built yet
- [[_Conventions]] — how this vault is kept

## Quick facts

- Two environments: `live/demo` (**create and destroy**, never left running) and
  `live/staging` (the promotion target — written, never applied). Cost detail
  lives in `../../COST.md`, not here.
- Region `ap-southeast-1`. State in S3 + DynamoDB, keyed `demo/careroute/`.
- The pipeline (`.gitlab-ci.yml`) provisions nothing except the **manual**
  `apply`; a scheduled `destroy` is the safety net.

## Open items

- [[Clinical Endpoint Auth Gap]] — ⛔ the blocker before any non-demo exposure
- **Nothing below has been applied yet.** The third pass on 2026-09-17 built out
  every gap the courseware review found; the stack goes up in about a week. See
  [[Lecture Alignment]] for what is built vs proven.
- **No WAF on the ALB** — a real gap for a healthcare app, kept deliberately red
  in `.checkov.yaml` (CKV2_AWS_28) rather than skipped.
