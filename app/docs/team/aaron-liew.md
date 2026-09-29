# Aaron Liew — Safety-Override & Safety-NLP

Back to [who did what](./README.md) · [project README](../../README.md)

## The job

The clinical safety interlock. Safety-Override is the one step in the pipeline that is allowed to
overrule the model, and it can only ever move in one direction: **it raises urgency, it never lowers
it.** Chest pain becomes P1 no matter what the classifier thought.

Aaron then built a semantic NLP layer on top that understands language the regex floor cannot —
without ever being able to weaken the floor underneath it.

## What it does

![Aaron Liew](../diagrams/generated/member-aaron-liew.png)

<sub>Source: [`member-aaron-liew.mmd`](../diagrams/src/member-aaron-liew.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

## What was delivered

**The deterministic floor.** Red-flag rules in `redflags.py` — chest pain, stroke signs, suicidal
ideation and more. Un-overridable by design, and provably one-directional: the override can raise,
never lower, and is a no-op when nothing fires.

**The semantic layer — 16 modules, the largest subsystem added to the project.** Named-entity
recognition, assertion/negation detection (so *"no chest pain"* is not read as *"chest pain"*),
similarity scoring, context handling, translation, and a classifier, with benchmarking for both
overall and per-category performance.

**Governed model loading.** `models/safety/manifest.json` is a checked-in **governance record**, not
a build artifact: it states which optional models may load and under which approvals. Optional
dependencies live in `requirements-safety-nlp.txt`; without them the layer degrades cleanly to the
regex floor, which is a supported configuration rather than a failure.

**An upgrade that cannot regress safety.** This is the part worth reading closely. The agent moved
from L1 to L2 — it now reasons — but the reasoning layer is strictly **additive**. The deterministic
result is the floor; the semantic layer may only add signals on top. So a hallucinating layer can
cost precision but **cannot cost recall**, and with the LLM and the optional models switched off the
behaviour is byte-identical to the pure-rules version. `safety.py` is the worked example the other
agents copy for layering reasoning over determinism.

**Evaluation E7** — the safety red-flag context benchmark.

## Files owned

| Path | What it is |
|---|---|
| [`app/agents/safety.py`](../../backend/app/agents/safety.py) | The Safety-Override agent |
| [`app/redflags.py`](../../backend/app/redflags.py) | The deterministic L1 rules |
| [`app/safety_nlp/`](../../backend/app/safety_nlp/) | The 16-module semantic layer |
| `models/safety/manifest.json` | Which models may load, under which approvals |

## Declared interface

| Property | Value |
|---|---|
| Autonomy | L2 over an L1 floor |
| Tools | `redflags.evaluate`, `llm.complete`, `safety_nlp.classify` |
| Publishes | `safety.override` |
| Subscribes | `acuity.classified`, `safety.assessment.requested` |
| Capability | `AGENT` |

## Prove it

```bash
cd backend
pytest -m safety                        # the agent in isolation
pytest tests/test_redflags.py           # raise-only, never lowers
pytest tests/test_safety_nlp.py         # the semantic layer
pytest tests/test_safety_nlp_manifest.py  # governed model loading

RUN_SAFETY_NLP_ARTIFACT_TESTS=1 pytest -m safety_nlp_artifact  # opt-in, loads real models
```
