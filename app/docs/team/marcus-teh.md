# Marcus Teh — Care-Routing

Back to [who did what](./README.md) · [project README](../../README.md)

## The job

Turn an urgency level into somewhere a person can actually go. Not "see a GP" — *this* clinic, open
now, this far away, and here is how to get there. This is the agent most exposed to the real world:
it depends on clinic data, opening hours and a live travel API, any of which can be wrong or down.

## What it does

![Marcus Teh](../diagrams/generated/member-marcus-teh.png)

<sub>Source: [`member-marcus-teh.mmd`](../diagrams/src/member-marcus-teh.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

## What was delivered

**Tier and clinic selection.** Final acuity maps to a care tier, then to a specific clinic with a
wait and travel estimate, drawing on the CHAS clinic directory and a GoWhere opening-hours snapshot.

**Constrained LLM selection.** The model does not invent a clinic. It chooses from a list of verified,
nearby, currently-open candidates, and the returned clinic ID, confidence and explanation are
independently checked before being accepted. An invalid or refused response falls back to the nearest
verified clinic.

**Live routing, with a failsafe that tells the truth.** OneMap supplies travel time and route
geometry. When OneMap is unavailable or unconfigured, the agent degrades to a **clearly labelled**
local estimate (`travelEstimateSource`) rather than presenting a guess as a real route. A fabricated
route is worse than an admitted estimate when someone is deciding where to go while unwell.

**Shared client, shared cache.** One OneMap client and token plus a route cache are shared across
requests — these are expensive and case-independent — while per-case state stays private to the
request.

**The patient-facing result panel.** The selected clinic, the alternatives with travel time and CHAS
tag, a public-transport replan banner, and structured `routingReason` / `routingClarification`
explanations rendered without the UI hard-coding sentences.

**Evaluation E3** — orchestrator / workflow-routing accuracy.

## Files owned

| Path | What it is |
|---|---|
| [`app/agents/routing.py`](../../backend/app/agents/routing.py) | The Care-Routing agent + clinic/tier tables |
| [`app/services/onemap.py`](../../backend/app/services/onemap.py) | OneMap client and failsafe |
| [`frontend/components/PatientTriage.jsx`](../../frontend/components/PatientTriage.jsx) | The routing result panel |

## Declared interface

| Property | Value |
|---|---|
| Autonomy | L2 |
| Tools | `clinic.lookup`, `facility.hours.lookup`, `travel.estimate`, `llm.complete` |
| Publishes | `care.routed` |
| Subscribes | `acuity.classified`, `safety.override` |
| Capability | `AGENT` |

## Prove it

```bash
cd backend
pytest -m routing                      # the agent in isolation
pytest tests/agents/test_routing_a2a.py
pytest tests/services/test_onemap.py   # client + failsafe

RUN_LIVE_ROUTING_TESTS=1 pytest -m integration   # opt-in, hits the real OneMap API

cd ../frontend && npx playwright test tests/e2e/routing_ui.spec.js
```
