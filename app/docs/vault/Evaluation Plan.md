---
tags: [mlops, careroute, active]
updated: 2026-09-17
---
# Evaluation Plan — agent + system

Back to [[Home]]. Related: [[Proposal Review Feedback]] · [[Agent Capability Audit]] · [[MLOps Pipeline]] · [[App Overview]]

Answers Point 2 of [[Proposal Review Feedback]]: six evaluations, each defining
**test dataset · expected outputs · evaluation metrics · acceptance criteria**.

The plan is **code, not prose** —
[`backend/app/evals/plan.py`](../../backend/app/evals/plan.py) holds one
`EvalSpec` per evaluation and `tests/test_eval_plan.py` validates every one on
every CI run. A spec missing any of the four mandatory fields fails the build, so
this note and the plan cannot drift apart. Run: `pytest -m eval`.

## The six

| # | Evaluation | Owner | Agent | Status |
|---|---|---|---|---|
| E1 | Symptom-intake field-extraction accuracy | Sham | intake | **implemented** |
| E2 | Appropriateness of clarifying questions | Heriz | hitl | planned — **feature does not exist** |
| E3 | Orchestrator / workflow-routing accuracy | Marcus | routing | planned |
| E4 | Severity-classification performance | James | classifier | **implemented** |
| E5 | Human-in-the-Loop trigger accuracy | Heriz | hitl | **implemented (template)** |
| E6 | Tool-selection accuracy | platform | all | **implemented (template)** |

E5 and E6 are **reference implementations** — the other four owners copy their
shape: a JSON dataset under `tests/fixtures/`, the real pipeline driven end-to-end
with the LLM disabled, and acceptance criteria imported from the spec.

## Beyond the six (additions, not replacements)

Each covers a property the reviewer's six did not scope. `plan.EVALUATION_PLAN` is
the source of truth; this table is the map.

| # | Evaluation | Agent | Added | Status |
|---|---|---|---|---|
| E7 | Safety-context benchmark | safety | 2026-08 | **implemented** |
| E8 | Handoff-summary faithfulness | handoff | 2026-08 | **implemented** |
| E9 | Guardrail effectiveness, scored | guardrail | 2026-09-12 | **implemented** |
| E10 | Retrieval quality (lexical vs dense vs hybrid) | rag | 2026-09-16 | **implemented** |
| E11 | Critic decision validity | reflection | 2026-09-16 | **implemented** |
| E12 | Retrieval **context precision** | rag | 2026-09-17 | **implemented** |
| E13 | **Tool-call accuracy**, scored | routing | 2026-09-17 | **implemented** |
| E14 | **Multi-turn continuity** of the clarification handshake | hitl | 2026-09-17 | **implemented — scores 0.750 against the 0.90 gate** |
| E15 | **Groundedness**: term overlap vs LLM-as-judge | handoff | 2026-09-17 | **implemented — overlap 0.5625; the judge is unrun in CI** |

Five of them (E9, E12, E13, E14, E15) exist because a pass/fail check was replaced by a
**rate over a labelled corpus**, and each measures BOTH directions — recall and
false-positives, precision and recall, refusals and false refusals. E9 found two
real bugs that way; E12 found that a fixed `top_k` capped precision at 0.5 however
good the ranking; E13 found nothing, which is a result rather than a formality;
E14 found the largest of them — the clarification handshake carries the fact that
a question was answered and never reads the answer; E15 found that the cheap
faithfulness check everyone reaches for scores 0.5625 and rejects every faithful
paraphrase in the corpus.

## Acceptance criteria are reused, not invented

Where a threshold already gates a release, the plan cites the *same* number, so it
describes what actually blocks a merge:

| Criterion | Source |
|---|---|
| accuracy ≥ 0.75 | `ml/model.MIN_ACCURACY` — enforced at **train** time |
| red-flag recall ≥ 0.95 | `ml/model.MIN_RED_FLAG_RECALL` |
| pipeline red-flag recall == 1.0 | `test_triage_eval.MIN_RED_FLAG_RECALL` |
| confidence gate 0.5 | `agents/base.CONFIDENCE_THRESHOLD` |
| subgroup accuracy ≥ 0.75 | `test_fairness_gate.py` |
| drift PSI < 0.5 | `ml/monitor.py` |

New numbers appear only where nothing existed to reuse, and are marked `(NEW)`.

## Four findings worth reporting to the reviewer

> [!important] E1 — the LLM was fine; our own keyword table was the bottleneck
> E1 failed on its first run, like E5 did. Four defects in `intake.py`, all fixed
> at root with **no bar lowered**:
> 1. The vocabulary matched *clinical shorthand*, not how patients write. An
>    inserted copula ("my throat **is** closing") or an expanded contraction ("it
>    **will not** stop bleeding" vs the listed "won't") defeated the match — so
>    **stroke, anaphylaxis and severe bleeding each extracted zero keywords.**
> 2. On the LLM path, **every translation was correct**. *"a strong pain in my
>    chest"*, *"my chest hurts a lot"*, *"difficulty breathing"* — and the table
>    matched none of them. Severe-keyword recall was **0.800**. The measurement was
>    tracking the model's *word choice*, not its accuracy. This is only visible
>    because the two paths are scored separately.
> 3. "my head hurts" / "my stomach hurts" were simply missing.
> 4. The LLM trigger fired only on *zero* keywords, so Spanish *"Estoy vomitando"* —
>    an accidental **cognate** hit on the English form `vomit` — short-circuited
>    translation and returned an untranslated sentence with one lucky keyword. A
>    cognate is not a reading of the sentence; the trigger is now *empty keywords
>    **or** non-English*.
>
> The cognate behaviour is now **measured, not assumed**: coverage is reported
> (0.045) and whatever leaks through must be *correct*, because a wrong keyword
> from an untranslated language is worse than none — downstream it is
> indistinguishable from a real extraction.

## Three earlier findings

> [!important] E5 — recall alone was not an evaluation
> The plan measured red-flag **recall** only. Recall is trivially maximised by
> escalating every case, which would flood the clinician queue and make *"AI
> assists, clinician decides"* meaningless. `tests/fixtures/hitl_cases.json` now
> carries a **must-NOT-escalate** half and the gate adds **specificity ≥ 0.80**.
> `test_dataset_covers_both_classes` stops the negative half being dropped.

> [!important] E6 — "tool-selection accuracy" is not measurable here
> Since 2026-09-15 ONE agent selects tools at runtime (Care-Routing's bounded ReAct loop, `tests/agents/test_routing_react.py`); every other worker declares a static least-privilege
> `TOOL_ALLOWLIST` enforced by `enforce_tool_access()`. Reporting a
> selection-accuracy number would be reporting a fiction. Reframed as
> **enforcement**: `tests/fixtures/tool_access_cases.json` is the full
> (agent, tool) permission matrix — every permitted pair must execute, every
> forbidden pair must raise. Block rate must be 1.0 (OWASP LLM06/LLM08, FR-12).

> [!important] E5 caught a real defect on its first run — a minor-trauma blind spot
> **This is the strongest evidence that the expanded plan was worth doing.** The
> new specificity gate failed immediately at **0.500 < 0.80**: `noesc-bruise`,
> `noesc-minor-cut` and `noesc-sprain` all escalated to a clinician.
>
> Root cause was not the fixture. `ml/features.FEATURE_KEYWORDS` held 19 symptom
> categories, **all medical, none traumatic** — no wound, bruise or sprain. Every
> minor-injury complaint therefore lit up *zero* symptom flags, collapsed to the
> same near-empty feature vector, and scored an identical **0.448** confidence.
> That sits just under `CONFIDENCE_THRESHOLD` (0.5), which the HITL gate converts
> into a clinician interruption. The identical confidence across three unrelated
> injuries was the tell. Minor trauma is a large share of real walk-in volume, so
> the model was blind to a whole clinical category and the blindness was being
> paid for by the clinician queue.
>
> Fixed at root: three categories (`minor_wound`, `bruise`, `sprain_strain`) added
> to the feature space with matching `_ACUITY_PROFILES` rows, and the model
> retrained (26 → 29 features). The three cases now classify correctly and
> confidently — P5 @ 0.69, P5 @ 0.76, P4 @ 0.92 — and specificity reaches 1.0.
> The threshold was **not** lowered to make the gate pass; that would have been
> exactly the rubber stamp this evaluation exists to prevent.
>
> Side finding, now **fixed**: `ml/features.is_suspected_evasion()` was defined
> but **never called anywhere**, and claimed a zero-feature vector was evidence of
> adversarial obfuscation. The trauma cases disprove that — the same three
> sentences flagged True before the retrain and False after it. *A verdict that
> flips when you retrain the model, with the input unchanged, is not measuring the
> input; it is measuring coverage.* Renamed to `has_no_feature_coverage()`, honest
> docstring, and demoted to telemetry: `zero_coverage_rate()` is now reported on
> every monitoring backend beside drift. Deliberately **never a gate** — it is
> redundant with the confidence rule (no features → low confidence → HITL already
> escalates), and gating on it would have *masked* this very defect by making the
> over-escalation look intentional. Real evasion screening stays in
> [`app/guardrail.py`](../../backend/app/guardrail.py), which separates trauma
> (relevance 0.083–0.10) from gibberish (0.0) independently of the model.

## What blocks the four planned ones

Each spec states its own blocker (a build failure if it doesn't). In short:

- **E1** — ~~no dataset~~ **DONE.** `tests/fixtures/intake_gold.json` (44 rows,
  en/es/zh/ms/fr/**ta**) + `tests/test_eval_intake.py`. Tamil was added because the
  original list covered two languages barely spoken here and omitted one of
  Singapore's four official ones. Like E5, it **failed on its first run and found
  four real defects** — see the finding below and [[Changelog]] (2026-07-28 later).
- **E2** — **the feature does not exist.** Nothing in `app/` asks the patient a
  follow-up question. Unblocked only once the clarifying-question action joins the
  HITL action space ([[Agent Capability Audit]]). Report as *scope*, not coverage.
- **E3** — no dataset, and clinic selection has no ground truth until the mock
  directory carries queue depth / distance / opening hours.
- **E5 extension** — Heriz to grow the fixture toward 40 balanced rows.

## Related

The MLOps release gates that several of these criteria come from are described in
[[MLOps Pipeline]]; the agent classifications the evaluations are scoped against
are in [[Agent Capability Audit]].
