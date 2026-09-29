# How CareRoute works — the ideas behind the design

Back to the [project README](../README.md)

Three design decisions explain most of this system. Each gets a diagram.

---

## 1. It is a hybrid, not an "all-LLM" system

A common assumption about multi-agent AI is that every agent is an LLM call. CareRoute deliberately
is not. Exactly **one trained model**, **several bounded LLM steps**, and **deterministic logic where
being predictable matters more than being clever**.

![Hybrid architecture](diagrams/generated/hybrid-architecture.png)

<sub>Source: [`hybrid-architecture.mmd`](diagrams/src/hybrid-architecture.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

Each worker **declares** which it is, and the build checks the claim: you cannot call yourself an
agent without an inference step *and* more than one possible outcome, and a policy node must carry an
`upgrade_path` describing what real agency would take. Run `pytest -m capability`.

---

## 2. Every AI step has a deterministic floor

No AI call is load-bearing. Each one is tried first and falls back — and the fallbacks chain, so the
system keeps working with no AI available at all. That is not a degraded mode kept for emergencies:
**it is how the entire test suite runs**, which is what stops the fallback path quietly rotting.

![LLM provider fallback chain](diagrams/generated/provider-fallback.png)

<sub>Source: [`provider-fallback.mmd`](diagrams/src/provider-fallback.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

The kill switch (`CAREROUTE_KILL_SWITCH=1`) forces the deterministic path on demand — an answer to
"what do we do if the model starts behaving badly in production?" that does not require a deploy.

---

## 3. Personal data is removed before the AI sees it

Redaction happens **before** any agent runs, not before storage. So the masked text is what the
agents reason about, what gets logged, and what any external LLM provider would ever receive.

![Data and privacy flow](diagrams/generated/data-privacy.png)

<sub>Source: [`data-privacy.mmd`](diagrams/src/data-privacy.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

Prompt and response **content is never logged** — only provider names and outcomes. The audit trail
is a SHA-256 hash chain, so an altered or removed entry is detectable rather than merely discouraged.

---

## Where to go next

| You want | Read |
|---|---|
| What the app does, end to end | [README](../README.md) |
| How the agents coordinate | [README §3](../README.md#4-how-the-agents-talk-to-each-other) |
| Who built which part | [Who did what](./team/README.md) |
| The CI/CD and model gates | [MLOps](./MLOPS.md) |
| Supporting diagrams | [Diagrams](./DIAGRAMS.md) |
| Every technical detail | [Technical Reference](./TECHNICAL_REFERENCE.md) |
