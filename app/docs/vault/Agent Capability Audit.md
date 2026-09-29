---
tags: [architecture, careroute, active]
updated: 2026-07-27
---
# Agent Capability Audit — which of our "agents" are actually agents

Back to [[Home]]. Related: [[Proposal Review Feedback]] · [[Evaluation Plan]] · [[App Overview]] · [[Loop Engineering]]

Answers Point 1 of [[Proposal Review Feedback]]. The mechanism is
[`backend/app/agents/capability.py`](../../backend/app/agents/capability.py);
the upgrade template is
[`backend/app/agents/reasoning.py`](../../backend/app/agents/reasoning.py).

## The test we apply

An **agent** does both of these. A **policy node** does neither or only one.

1. **Reasoning** — performs an inference step whose output is not fixed by its input.
2. **Autonomy** — chooses between more than one genuine outcome.

`classify()` applies exactly this rule mechanically, and `enforce_capability()`
refuses a declaration the implementation does not support — you cannot call your
worker an agent without a model call and a real action space. Memory and tool use
are declared too (the reviewer asked for all four) but are not part of the
classification rule: plenty of real agents are stateless.

`orchestrator` is a third classification, because "is it an agent?" is the
wrong question for the workflow itself. Exactly one agent may hold it, named
by `capability.ORCHESTRATOR_SLUG`. That used to be the platform-owned
Supervisor; it is now **Symptom-Intake**, which is unusual in that it is both
a worker and the workflow. The declaration has to be explicit precisely
because `classify()` could never infer that combination.

## Where each worker stands

| Worker | Owner | Class | Reasons? | Action space | Trained model |
|---|---|---|---|---|---|
| Symptom-Intake | Sham | **orchestrator** *(+ agent)* | LLM extraction | open (sentence + keywords + language) **+ runs the fixed sequence** | — |
| Severity-Classifier | James | **agent** | RandomForest → LLM → keywords | 5 acuity codes | **yes — the only one** |
| Safety-Override | Aaron | **agent** *(upgraded)* | semantic red-flag layer | confirm / add / add-nothing | — |
| Care-Routing | Marcus | agent | constrained LLM clinic selection over verified nearby candidates | choose candidate / nearest fallback / safe offline route | — |
| Human-in-the-Loop | Heriz | policy node | no | escalate / don't | — |
| Reflection / Critic | platform | policy node | no | reroute / force-escalate / pass | — |

`test_exactly_one_agent_uses_the_trained_model` pins the "trained model" column —
that assertion **is** the answer to Point 3.

## The upgrade pattern — additive, never replacement

The wrong fix is replacing rules with an LLM. That trades a guarantee for a
probability, and `test_triage_eval.py` gates on red-flag recall of exactly 1.0.

```
deterministic result ──┐
                       ├──► monotone merge ──► final   (can only escalate)
reasoning layer      ──┘
```

- the deterministic path is untouched and becomes the **floor**
- the layer may only make the outcome **more cautious**
- any failure — LLM down, bad JSON, layer disabled — returns `None` and the floor stands
- therefore: **offline behaviour is byte-identical**, and a hallucinating layer can
  cost precision but *cannot* cost recall

## Worked example — Safety-Override (`safety.py`)

The real gap that justified it: `RED_FLAG_RULES` is **English-only literal regex**,
while intake claims multilingual support and its deterministic `_fallback()` does
no translation. So with the LLM down, *"no puedo respirar"* and *"an elephant is
sitting on my chest"* match **zero** rules.

`SemanticRedFlagLayer` closes that, bounded hard: it may only answer with a
category that already exists in `RED_FLAG_RULES` (so it cannot invent an
emergency), needs ≥0.6 confidence, and is never asked whether a case is *safe* —
that question has no output channel, so a prompt injection has nothing to express
itself through. Forced acuity always comes from the rule table, never the model.

## Still open — the remaining `upgrade_path`s

Each policy node carries its own route to agency in its `CAPABILITY.upgrade_path`
(a build failure if missing). Summary:

- **HITL (Heriz)** — widen the action space to *escalate / ask one clarifying
  question / proceed*, keep today's three rules as a hard floor, and wire
  `monitoring/ground_truth.jsonl` (already written on every clinician decision,
  currently unread) back in as memory. → [[Evaluation Plan]] E5 + E2.

Run the audit: `pytest -m capability`.
