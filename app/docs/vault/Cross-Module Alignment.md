---
tags: [courseware, careroute, active]
updated: 2026-09-12
---
# Cross-Module Alignment — AIC, AAS, XRAI

Back to [[Home]]. Related: [[Lecture Alignment]] · [[Courseware Alignment]] · [[Evaluation Plan]] ·
[`ASPECTS.md`](../../ASPECTS.md) · [`backend/SECURITY.md`](../../backend/SECURITY.md)

[[Lecture Alignment]] and [[Courseware Alignment]] both audit **one** module — *Deploying and
Operating AI Solutions*. This note covers the other three: **AI and Cybersecurity (AIC)**,
**Architecting Agentic AI Solutions (AAS)** and **Explainable and Responsible AI (XRAI)**.

## Method

The four module folders hold **54 non-admin decks**. Rather than read them by eye, they were
extracted to text with `pypdf` and cross-referenced against the 325 tracked files in this repo:
for every named technique, framework or taxonomy a deck teaches, does the repo mention it at all?
That produces candidates, not conclusions — each candidate was then confirmed by reading the deck
section and the code. Two candidates turned out to be **false alarms** (evasion attacks and
Fairlearn are both implemented; the search strings were wrong), which is the expected failure mode
and the reason the matrix is a starting point rather than a verdict.

## What was wrong, and is now fixed

### 1. The OWASP LLM Top 10 numbering was two editions mixed together

`backend/SECURITY.md` is the graded AI-security risk register. It numbered risks using a blend of
the **2023** and **2025** OWASP lists, so:

* *Excessive Agency* was labelled **LLM08** — that is its 2023 number; in the 2025 list taught in
  AIC Day 2 it is **LLM06**, and LLM08 is *Vector and Embedding Weaknesses*.
* **LLM02** was claimed by two different rows (*Insecure Output Handling* and *Sensitive
  Information Disclosure*) — internally contradictory whichever edition you pick.
* *Overreliance* (2023) is *Misinformation* (2025).

The AIC workshop handouts are named `LLM-01-Prompt_Injection` … `LLM-10-Unbounded_Consumption`, so
the edition the module marks against is unambiguous. **11 cells renumbered**, and the table now
states its edition up front. [`ASPECTS.md`](../../ASPECTS.md) repeated the LLM08 error and is fixed
to match.

This is worth flagging beyond the fix: a marker checking the register against the deck would have
hit the contradiction on the first row they looked at.

### 2. Two LLM risks had controls but no register row

**LLM03 Supply Chain** and **LLM07 System Prompt Leakage** were absent from the register even
though both are already defended — SBOM + Trivy + `modelscan`/`fickling` for the first,
`guardrail.screen_output()` for the second. The work existed; the credit was missing. Both now have
rows, as do **LLM04** (poisoning, see below) and **LLM08** (the read-only corpus).

### 3. The agentic taxonomies were unmapped except ASI10

AIC Day 3 teaches the **OWASP Agentic AI Top 10 (ASI01–ASI10)**; the AAS *Agentic AI Threats and
Mitigations* guide teaches the overlapping **T1–T17** model. The register named exactly one of
them, ASI10. `SECURITY.md` now carries a full ASI01–ASI10 table.

**Most of it is honest N/A, and that is the point.** ASI05 (code execution) does not apply because
no agent generates or runs code; ASI07 (inter-agent protocol) does not apply because the workers
are in-process and there is no network hop to MITM. Writing "N/A because the architecture removes
the path" is stronger evidence of understanding than inventing a control.

Two entries came out **Partial**, and both are real:

* **ASI06 Memory & Context Poisoning** — `CaseState` is per-request and the RAG corpus is read-only,
  but `store.recall_session` *is* cross-visit memory and has **no integrity check**. This is the
  genuine gap in the agentic column.
* **ASI08 Cascading Failures** — the ≤ 9-turn step budget and the deterministic fallback cap
  runaway loops, but there is no circuit breaker or per-agent quota.

### 4. Two thirds of the classical ML attack taxonomy was untested

AIC Day 1 splits attacks on *traditional* (non-LLM) models three ways by the stage the attacker
reaches. CareRoute had one of the three:

| Attack class | Stage | Before | Now |
|---|---|---|---|
| **Evasion** | deployment | ✅ ART HopSkipJump, `test_robustness.py` | unchanged |
| **Poisoning** | training | ❌ nothing | `test_ml_attacks.py` — label-flip gated |
| **Privacy: membership inference** | query access | ❌ nothing | `test_ml_attacks.py` — gated at ≤ 0.60 |
| **Privacy: model extraction** | query access | ❌ nothing | measured, **accepted risk**, documented |

New: `backend/tests/test_ml_attacks.py` + the blocking `ai-security:ml-attacks` job (69 jobs now).

**The tests gate a defence, not a demonstration.** Showing an attack works proves nothing about
this system, so each test asserts that a control already in the pipeline catches or bounds it:

* **Poisoning** — flipping 10% of red-flag training labels to P5 (the deck's *label flipping*,
  aimed the one direction that is clinically dangerous) drops red-flag recall from **0.957 to
  0.915**, and the existing release gate rejects it at the 0.95 threshold. A control test asserts
  the *clean* run still passes, because a gate that rejects everything is the likelier bug. A third
  test walks 10 → 20 → 30% and asserts recall falls monotonically — without it, the 10% result
  could be one lucky seed rather than the attack actually working.
* **Membership inference** — ART black-box attack reaches **0.548** accuracy against 0.5 chance.
  Gated at 0.60, which fails if a future change (deeper trees, less regularisation, a smaller
  training set) starts memorising patients. The data is synthetic today; the same forest retrained
  on real records leaks identically, and by then it is not testable in public CI.

### 5. Model extraction: measured, and deliberately not "fixed"

Worth recording separately because the honest answer is uncomfortable. A surrogate trained on the
deployed forest's own answers reaches:

| Queries | Agreement with the deployed model |
|---|---|
| 100 | 84.7% |
| 500 | 96.0% |
| 2000 | **100%** |

A 200-tree forest over 22 features is cheap to clone and no control in this codebase changes that.
The rate limiter (30 req/min/client) buys **time and noise** — 2000 queries is over an hour from one
key — not prevention. Accepted as a risk rather than papered over with a test that would have to
assert something false: the model is trained on synthetic data, its logic is published in
`MODEL_CARD.md`, and a clone gives an attacker no access to a patient.

### 6. The evasion gate that was already there had never run

Found while verifying the new tests, and the most serious item in this note.
`adversarial-robustness-toolbox` was **not in `requirements-dev.txt`** — even though
`test_robustness.py`'s docstring and the `test:robustness` CI comment both stated it was. The
consequence chain:

1. The job pip-installs `requirements.txt -r requirements-dev.txt`; ART is not among them.
2. `pytest.importorskip("art")` at module level skips the whole module.
3. pytest exits **5** ("no tests collected").
4. The job's exit-5 handler treats that as a clean skip and `exit 0`.

Job `16462517271` logs `1 skipped in 0.07s`, then `Job succeeded`. The evasion gate has been green
since it was written **without ever executing the attack**. ART is now pinned at `1.20.1`.

This is the repo's own **absent-is-not-clean** rule (see [[Courseware Alignment]]) failing where it
is hardest to see: a skipped security test is visually indistinguishable from a passing one, and
the skip was *designed in* as a convenience. The same trap is why the ART import in
`test_ml_attacks.py` sits inside the single test that needs it rather than at module level — the
poisoning gates need only numpy and must never be skippable.

## Checked and genuinely fine

* **XRAI explainability** — SHAP (real `TreeExplainer`), an in-repo LIME-style local surrogate used for a gated SHAP-agreement audit (`explanationAgreement`; the `lime` package was never installed, so the old "LIME explanation" was always empty), a single-edit patient-facing counterfactual, the sex-flip counterfactual fairness audit, local *and*
  global feature importance, model card. Ahead of what the deck asks.
* **XRAI fairness** — demographic parity, equal opportunity, subgroup accuracy, before/after
  mitigation, Fairlearn CI gate. The deck's named metrics are implemented, not cited.
* **AAS agentic patterns** — supervisor orchestration, reflection/critic, spectrum of agency,
  RAG grounding, working + episodic memory, A2A. Covered in [[Agent Capability Audit]].
* **AIC LLM Top 10 controls** — every one of the ten now has a row, and nine have a real control.

## Closed in the second pass (2026-09-12)

Everything on the original open list was built except the two items at the bottom, which are
recorded as decisions rather than debt.

### ASI06 — episodic memory is now integrity-checked

`store.memory_digest()` SHA-256 fingerprints each case as it enters episodic memory; the digest is
held **beside** the record in `Store._memory_digests`, not on it, so a writer who mutates a record
does not also get to rewrite its own checksum. `recall_session()` re-verifies before replaying and
**fails closed**: a record whose digest does not match, *or that has no digest at all*, is excluded
rather than returned with a warning. A missing digest and a broken one are the same evidence.

Only decision-bearing fields are covered (symptoms, acuity, confidence, care tier, escalation,
timestamp). Hashing the whole record would fire on edits that cannot steer a later triage — a route
instruction, a wait time — and a check that cries wolf is one that gets switched off.

`verify_episodic_memory()` is separate on purpose: recall must exclude a suspect record *silently*
so reasoning is never influenced by it, while an operator needs to know something was excluded. A
control that only fails quietly is indistinguishable from one that never fires.

### ASI08 — circuit breaker on the provider chain

Five workers call `llm.complete()` per triage, so a dead provider is waited on **five times per
case**, its full timeout each, and every concurrent case pays the same tax. That amplification —
not the original fault — is what ASI08 is about. After 3 consecutive failures a provider is skipped
outright until a 30 s cooldown elapses, then one half-open trial call decides whether it reopens.

It counts *consecutive* failures and any success resets the count, so a flaky-but-usable provider is
never locked out. Breaker state is exposed on `/api/health`, because a provider can be reachable and
still be skipped, and an operator who cannot see that reads the skip as a whole-chain outage. Five
tests pin it, including that the ASI10 kill switch still wins (no provider is called at all).

### Equalized Odds and Disparate Impact

Both named in XRAI Day 2, both now in `fairness.py` and in the `/api/fairness` payload.

**Equalized Odds** exists because Equal Opportunity constrains only the TPR, which a model can
satisfy while over-triaging: catching every elderly emergency by sending half the *well* elderly to
resuscitation scores a perfect EO gap. Adding the FPR closes that. The headline is the **max** of
the two gaps, never the mean — averaging would let a total FPR disparity read as "half fair".
Measured: TPR gap **0.079**, FPR gap **0.044**.

**Disparate Impact** is the ratio form of Demographic Parity (the four-fifths rule). It is
**reported, not gated**, and the measured value is the reason why: **0.377**, well under the 0.8
floor — because the 65+ band is correctly flagged urgent about twice as often as the others. Age-
adjusted acuity is correct medicine and is the entire point of the age-aware model, so gating on
0.8 would gate against this project's own fairness mitigation. The number is surfaced so a reviewer
can judge whether a disparity is clinical or unfair, which is a judgement no threshold can make.

### Datasheet for Datasets

`backend/DATASHEET.md`, following Gebru et al. — motivation, composition, collection, preprocessing,
uses, distribution, maintenance, limitations. It states the uncomfortable facts rather than burying
them: the data is entirely synthetic so every fairness result is conditional on assumptions *we*
wrote, and the 65+ band is **~12% of rows against ~29% for each other band** — generated that way on
purpose, because it reproduces the real failure mode the project exists to demonstrate.

`tests/test_datasheet.py` pins every falsifiable number against `triage_dataset.meta.json`, so a
dataset change that is not reflected in the datasheet **fails the build**. The prose stays
hand-written — generating it would lose the judgement that makes it worth reading — but it cannot
go quietly stale.

### XRAI Day 3 instruments — scored after all

Previously skipped as "they score an organisation, not a codebase". That was true and was still the
wrong call: the instruments are on the syllabus, and the right answer is to score what translates
and mark the rest N/A **with the reason**. `docs/GOVERNANCE.md` Part 2 now does that.

* **AI Governance Maturity: Level 4** on automated validation and monitoring (69 CI gates, closed
  drift→retrain loop), **Level 3 partial** on cataloguing (MLflow + DVC + content-addressed dataset,
  but one project rather than an enterprise). **Level 5 not claimable** — there is no deployment
  target, so the lifecycle cannot be fully automated.
* **Accenture AI Maturity: Achiever on Responsible AI** — the dimension that fully translates, and
  the strongest claim in the project: responsible-AI practice is *industrialised* as blocking CI
  gates, not documented as review steps. **Builder** on Data & AI Core. Strategy and Talent are N/A.
* **IEEE AI Ethics Readiness: Advanced**, not Leading — "Leading" needs ethics infused across roles
  and onboarding, and there is no organisation.
* **AIRI**: assessed and recorded as not applicable rather than silently skipped.

What the instruments surfaced is worth carrying into the report: **every unmet level is blocked by
the absence of an organisation or a deployment target, not by missing engineering.**

### Adversarial re-training — built, measured, and the result is *negative*

AIC Day 1 names two model-side defences. **Ensemble learning** was already satisfied (the deployed
model is a 200-tree forest, and the deck's own argument for ensembles is that an attack tuned to one
boundary does not transfer to many). **Adversarial re-training** — "inject adversarial examples
during training with correct labels" — is now implemented in `app/ml/adversarial.py`.

It is a **measurable experiment, not part of the served training path**, and the measurement is why:

| | Clean accuracy | Robust accuracy (L-inf ≤ 0.25) |
|---|---|---|
| Baseline forest | 0.9093 | 0.7917 |
| Adversarially re-trained | 0.9093 | 0.4167 |

**Do not read that as "re-training harmed the model".** Two things make the table unusable as
evidence, and both of them *are* the finding:

1. **Only 6 of 40 attack attempts landed inside the budget.** "Inject adversarial examples during
   training" produced **six rows** against ~2,250. Clean accuracy is byte-identical before and
   after — exactly what six rows in 2,256 should do.
2. **The instrument is noisier than most effects it could detect.** The *same baseline model*,
   measured twice by `robust_accuracy`, scored **0.8333** and **0.7917**. HopSkipJump is stochastic
   and 24 attacked samples is a small denominator. A comparison whose instrument moves 0.04 on a
   fixed model cannot be read confidently.

The honest conclusion is therefore **"at a CI-affordable attack budget this experiment cannot
measure whether it helped or harmed"** — not a verdict on the technique. A defence adopted *or*
rejected on this would be a guess wearing a number.

Recording it this way is the point. The tempting move is to re-roll the seed until the defence
"works" and report that number; the deck's own red-teaming section (p37) is entirely about
methodology. Making this conclusive needs one to two orders of magnitude more attack budget than CI
can carry — the 48 attack runs above took **74 s**.

So the deployed model is unchanged, and the reason is a measurement rather than an omission.

**A bug this found, worth recording.** The first implementation filtered attack results to those
inside the perturbation budget, then re-derived the sample indices and truncated them to match — so
a survivor at position 5 was labelled with the clean row at position 2. That is *silently mislabelled
training data*: a poisoning step wearing a defence's clothes, which is precisely the failure the
"with correct labels" wording in the deck exists to prevent. `generate_adversarial_examples` now
returns the source indices alongside the rows, and
`test_adversarial_rows_are_labelled_from_the_row_they_were_perturbed_from` uses deliberately
**non-contiguous** survivors (rows 7, 2, 9) so the truncation bug cannot come back unnoticed. The
first set of numbers recorded for this experiment came from the buggy version and were discarded.

## Still open

* **Promoting `test:robustness` to blocking** — once a few pipelines establish a stable
  robust-accuracy margin on CI hardware. See the note in `.gitlab-ci.yml`.
* **Adversarial-example *detection*** — AIC Day 1 p31's anomaly-detection defence. Not built; the
  input guardrail covers text, not feature-space anomalies.

## Honest limits of this audit

The decks were read at **section depth, not slide depth**. The DOAIS module got slide-by-slide
treatment in [[Lecture Alignment]]; these three got a taxonomy-and-named-technique sweep plus close
reading of the security decks, which is where the findings above came from. The AAS Day 2 deck
(130 pages) and the two governance frameworks (70 and 122 pages) were sampled, not exhausted —
if anything here is going to be marked against a slide this note missed, that is where to look.
