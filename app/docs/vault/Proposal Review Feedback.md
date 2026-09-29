---
tags: [review, careroute, active]
updated: 2026-07-27
---
# Proposal Review Feedback — Junhua, 2026-07-24

Back to [[Home]]. Related: [[Agent Capability Audit]] · [[Evaluation Plan]] · [[App Overview]] · [[Changelog]]

Reviewer **Chen Junhua** returned three points on the CareRoute AI proposal
(email thread, 2026-07-24, to Sham / James / Aaron / Marcus / Heriz). Point 3 was
answered on the thread on 2026-07-27; Points 1 and 2 were deferred with
*"you may proceed with implementation while taking the remaining feedback into
consideration"*, and are now implemented in code.

## The three points

**Point 1 — agent or workflow node?**
> "For the following two components, please clarify whether they are genuinely
> agents or whether they should be implemented as deterministic tools or workflow
> nodes... Please think through their required reasoning, autonomy, memory, and
> tool use." — *Safety-Override Agent, Human-in-the-Loop Agent.*

**Point 2 — expand the evaluation plan.** Six named evaluations, each needing a
test dataset, expected outputs, evaluation metrics, and acceptance criteria.

**Point 3 — is a separate ML model trained, and which agent uses it?**
> "MLOps are mentioned in the proposal, but it is unclear which agent uses the
> trained model, as the agent descriptions appear to indicate that all agents are
> LLM-based."

## What the review actually exposed

The proposal described CareRoute as a multi-agent system in which all agents are
LLM-based. It is not — it is a **hybrid**, and every one of the three points
follows from that one inaccuracy:

| | Reality |
|---|---|
| Trained models | **One** `RandomForestClassifier` (`backend/app/ml/`) |
| Consumed by | **Severity-Classifier only** (`classifier.py` `_try_model`) — the answer to Point 3 |
| Genuine LLM agents | Symptom-Intake (extraction), Severity-Classifier (LLM as fallback) |
| Deterministic nodes | Supervisor, Safety-Override, Care-Routing, HITL, Reflection |

**Junhua named two, but three of the five member-owned agents were deterministic**
— Care-Routing was not flagged and was in exactly the same position. It is
declared honestly now rather than waiting to be asked.

## How each point is answered — in code, not prose

Prose in a proposal cannot be verified and drifts. So all three answers are
declarations the test suite enforces. See [[Agent Capability Audit]] for the
mechanism and [[Evaluation Plan]] for Point 2.

- **Point 1 →** every worker declares an `AgentCapability`
  ([`agents/capability.py`](../../backend/app/agents/capability.py)) naming the
  reviewer's own four criteria — reasoning, autonomy (action space), memory, tool
  use — plus a classification of `agent` / `policy_node` / `orchestrator`.
  `tests/agents/test_capability.py` fails the build if a declaration contradicts
  the implementation. **A policy node must carry an `upgrade_path`**, so the route
  to genuine agency lives beside the code.
- **Point 2 →** [`app/evals/plan.py`](../../backend/app/evals/plan.py) holds one
  `EvalSpec` per evaluation with the four mandatory fields;
  `tests/test_eval_plan.py` validates them every CI run. Two are implemented as
  worked templates, four are honestly `planned` with a stated blocker.
- **Point 3 →** `CAPABILITY.uses_trained_model` is `True` on exactly one worker,
  asserted by `test_exactly_one_agent_uses_the_trained_model`.

## Status

- [x] **Point 3** — answered on the email thread 2026-07-27; now also machine-checked.
- [x] **Point 1** — capability framework + Safety-Override upgraded to a genuine agent.
- [x] **Point 2** — plan complete for all six; two implemented, four specified.
- [ ] **Remaining (the 5 owners):** implement the four planned evaluations and the
      two outstanding `upgrade_path`s — see [[Agent Capability Audit]] for who owns what.
- [ ] Rewrite the proposal's agent descriptions to state the hybrid split. The code
      is now the source of truth; the document is what is still wrong.

> [!warning] Do not "fix" this by making everything an LLM agent
> `redflags.py` says it in its own docstring: the rules are *intentionally* not
> LLM-based, because they are the last line of defence. The upgrade pattern is
> **additive** — see [[Agent Capability Audit]].
