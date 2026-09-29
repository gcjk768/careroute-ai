---
tags: [mlops, courseware, gates, careroute]
updated: 2026-09-17
---
# Courseware Alignment — *Deploying and Operating AI Solutions*

Back to [[Home]]. Related: [[MLOps Pipeline]] · [[Loop Engineering]] · [[Evaluation Plan]] · [[Changelog]] · [[Roadmap]]

Gate-by-gate mapping from the DOAIS courseware onto CareRoute's pipeline, and — as important — an
honest record of which reference gates **do not apply here and why**. Sources: `03.
CICD_for_Agentic_AI_Solutions.pdf` (the CI quality-gate table, p14), the AgenticAIOps Workshop 1
reference solution (`travel-concierge-gates.png`, `travel-concierge-pipeline.png`), and
`09. LLMSecOps_v0.6.pdf`.

## The idea worth stealing: lanes, gated by blast radius

The reference pipeline splits one repo into **five artifact lanes with different cadences** — app
(daily), agent (daily), config (hourly), knowledge (weekly), platform (monthly) — on the argument
that *"prompts change hourly, Terraform monthly; one pipeline for both is either too slow for the
prompt or too reckless for the infrastructure."*

CareRoute runs a single 10-stage pipeline for all of it. That is **not yet a problem**: prompts here
are Python constants inside the agent modules, not separately deployable config, so there is no
hourly-changing artifact to starve. It becomes a problem the moment prompts move to a config store
(the reference publishes them to flagd so they roll back in ~30 s without a deploy). Recorded here as
the trigger to watch, not as work to do now.

## Gate-by-gate

Legend: **✓** in place · **~** partial · **NEW** added 2026-09-12 · **N/A** does not apply, with reason.

| Reference gate | Threshold | CareRoute | |
|---|---|---|---|
| Dependency CVE | 0 HIGH/CRITICAL | Trivy blocks CRITICAL; OSV/Grype/Dependency-Check/Retire.js advisory | ~ |
| Injection bypass rate | ≤ 2% | `ai-security:guardrail-score` — **0.00%** over 30 injection cases | **NEW** |
| Guardrail recall / false positives | ≥ 0.95 / ≤ 0.05 | same job — **100% / 0%** over 55 labelled cases | **NEW** |
| Data protection (PII egress) | 0 events, Presidio | `scan:pii-egress`, blocking, over every published artifact | **NEW** |
| Agency (unbounded irreversible tools) | 0 | `TOOL_ALLOWLIST` + `enforce_tool_access()` + the enforcement matrix (E6) | ✓ |
| Safety violations | 0 | `ai-security:guardrail-regression` + E7 safety-context benchmark | ✓ |
| Efficiency — p95 latency | ≤ 8000 ms | `test:load-locust`, advisory until a CI baseline exists | ~ |
| Efficiency — cost per task | ≤ $0.12 | `app/llm_cost.py` + `careroute_llm_{tokens,cost_usd}_total` | **NEW** |
| Loop safety | ≤ 9 turns/task | `test:agent-graph` — step budget 9, measured 6–7 | **NEW** |
| Model lifecycle stage | registered version is staged | `app/ml/lifecycle.py` — `@challenger` on every gated build, `@champion` by explicit promotion only. Aliases, because MLflow 3.x removed the stage API both decks teach | **NEW** |
| LLM latency + cost alerting | quota/cost guard | `careroute_llm_latency_seconds` + the four `careroute-llm` rules in `monitoring/alert.rules.yml` | **NEW** |
| Graph reachability | every node reachable | `test:agent-graph`, from a real `orchestrate()` run | **NEW** |
| Regression vs live baseline | ≤ 3%, 95% CI | champion–challenger on point metrics, no confidence interval | ~ |
| Auto-rollback | error <1%, tools <2% | ECS alarm rollback in `careroute_ai_infra` (5xx rate > 1% = `canary.MAX_ERROR_RATE`) + circuit breaker; the infra `apply` fails when ECS reverted a release. Tool-failure gate not yet a CloudWatch alarm; rolling, not a traffic-split canary (needs AWS provider 6.x) | **configured, not yet applied** |
| Drift | intent PSI ≤ 0.2 | worst-feature PSI, gate 0.25, closed retrain loop | ✓ |
| Train/serve skew | features served == features trained | `app/ml/feature_contract.py` — the extractor's configuration AND behaviour fingerprinted into the artifact at training time, verified on load, checked in `test:model-gate` before the quality gate. The `featureNames` check it replaces saw layout only, never meaning | **NEW** |
| Availability — yield / harvest / uptime | served ÷ received; answer completeness | the `careroute-availability` recording rules + `app/availability.py`. Yield needed a new `outcome="error"` first: the counter it was to be derived from counted only successes | **NEW** |
| Nothing below prod spends money | assert no live creds | `scan:no-live-credentials` | **NEW** |
| Experiment tracks code version | git commit + source ref | `run_context.mlflow_tags()` on every run | **NEW** |
| Experiment tracks environment | Python, OS, libraries | same | **NEW** |
| Snapshot records statistics | schema + stats + splits | `triage_dataset.meta.json` | **NEW** |
| Retrain on target drift | label mix shifted | `_drift_gate`, PSI ≥ 0.25 | **NEW** |
| Retrain on concept drift | accuracy fell | `_drift_gate`, drop ≥ 0.10 | **NEW** |
| Build/test outcome notified | email, chat, dashboard | `notify:pipeline-outcome` | **NEW** |
| Task quality, 5 repeats | ≥ 0.90 | single deterministic run (acc + red-flag recall) | ~ |
| Tool-call accuracy | ≥ 0.95 | **Scored since 2026-09-17.** E13 (`app/evals/tool_calls.py`) rates the ReAct loop's validator over 34 labelled replies: accuracy **1.000**, refusal recall **1.000**, false-refusal **0.000**, per failure family. Scores the harness, not a live model's judgement — the E11 split. Other workers use static allowlists (no selection to score) | **NEW** |
| Retrieval context recall / precision | ≥ 0.88 / ≥ 0.80 | **Both measured since 2026-09-17.** E10 (`app/evals/retrieval.py`) gives hybrid **Recall@2 0.969**, clearing 0.88. E12 (`app/evals/context.py`) measures precision over the context actually handed to the model: **0.484 → 0.750** once `top_k` became a ceiling instead of a quota (`rag.assemble_context`), at **unchanged** recall. 0.80 is reachable at a tighter margin and **deliberately not taken** — it costs recall 0.969 → 0.906, and a dropped relevant citation is the worse error in triage | ~ |
| Grounding / faithfulness | ≥ 0.85 | **Instrumented 2026-09-17, not yet gated.** E15 (`app/evals/grounding.py`) measures both candidates on a 16-row corpus: deterministic term overlap scores **0.5625** — 1.000 on invented facts, **0.000 on faithful paraphrases**, and a perfect 1.0 on a flipped negation it cannot see. No tolerance fixes it (best 0.625, sweep published). The gate is therefore an LLM-as-judge (`eval.grounding`, deep + cacheable), which CI cannot run: it reports `available: false` rather than passing. E1/E8 remain sound for the narrower curated-term questions they ask | instrument built, gate blocked on a provider |
| Continuity, multi-turn | ≥ 0.90 | **Scored 2026-09-17 and NOT met: 0.750.** E14 (`app/evals/continuity.py`) runs each case three times — turn 1, turn 2 with the answer, and an empty-answer control. Never re-asks / retains context / terminates all measure 1.000; **the answer changing the outcome measures 0.000**, because `state.clarifications` is only ever tested for emptiness. Fix belongs with the unbuilt patient-facing resume path ([[Clarification Resume API Handoff]]); pinned by a strict `xfail` so closing it forces a re-score | open, quantified |

## What scoring found that spot-checking had not

`ai-security:guardrail-regression` blocks the pipeline and has always passed. Replacing "are these
payloads blocked?" with "what fraction gets through?" put the guardrail's **false-positive rate at
11%** on first run — against a 5% budget — while recall was already perfect:

* `"sprained my ankle playing football yesterday"` → blocked as an off-scope chat request. The
  off-scope layer only fires when a sentence carries **no** clinical vocabulary, and the lexicon had
  head/neck/back/arm/leg but **no joints or extremities at all** — no ankle, knee, wrist, shoulder.
  A whole triage category was one keyword away from rejection.
* `"i forget everything when the migraine starts"` → blocked as prompt injection, because
  `forget (everything|all|your)` is an imperative pattern that also matches a patient describing
  memory loss. Which is a neurological red flag.

Both fixed in `app/guardrail.py` (limb/joint vocabulary; the `forget` pattern now exempts a leading
first/third-person subject pronoun). The imperative forms still block, and a pronoun prefix buys an
attacker nothing because the high-specificity patterns are untouched — pinned by
`test_imperative_forget_is_still_blocked` and
`test_first_person_exemption_does_not_shield_a_real_injection`.

**The lesson is the method, not the two bugs.** A pass/fail gate over hand-picked payloads measures
the payloads someone thought of. Only a rate over a labelled corpus with deliberately
attack-shaped benign text can see a guardrail that is quietly turning patients away.

## Second pass — the core MLOps decks, not just the agentic one

The first pass mined `03. CICD_for_Agentic_AI_Solutions` and the workshop reference solution. Going back
through `02. CICD in MLOps`, `06. Data_Versioning_with_DVC` and `07. Experiment_Tracking_with_MLflow`
turned up four more gaps — all of them things the decks name explicitly and the pipeline simply did not do.

| Deck | What it asks for | Was | Now |
|---|---|---|---|
| 07, "What should an experiment track?" | **code version**, dataset version, **environment** | only dataset version (`data_sha256`) | `app/ml/run_context.py` — git commit/branch/dirty, Python, OS, and the five libraries that change a model, set as MLflow tags |
| 06, "What data versioning adds" | schema, **statistics**, **splits**, **provenance** | schema + hash only | snapshot metadata now carries per-feature min/max/mean/std, label and subgroup balance, the 0.25/stratified/seed-42 split, and generator provenance |
| 02 p33, "Model retraining trigger" | dips in accuracy · data drift · **target drift** · **concept drift** | only data drift gated | `_drift_gate` evaluates all three; any one breaching fires the retrain |
| 02 p7, "Notification of build/test outcome" | feedback via email, chat, dashboards | nothing | `notify:pipeline-outcome` + `app/ml/notify.py` |

Three judgement calls worth recording:

* **The commit goes on the MLflow run, not in the dataset metadata.** Deck 06 asks a snapshot for
  provenance and deck 07 asks a run for code version — and only the second is allowed to be volatile.
  `triage_dataset.meta.json` is git-committed and re-derived by `data:version --check`, so a timestamp
  or git SHA in it would dirty the file on every export and turn a real data diff into noise. Everything
  recorded in the snapshot is deterministic; `test_no_volatile_fields_leaked_into_the_metadata` pins it.
* **Secondary retrain signals fail OPEN; the headline still fails closed.** A missing `dataDriftPSI`
  breaches (a monitor that cannot measure is broken, not calm), but a backend that never computes target
  drift must not be reported as drifting on it — that would fire a retrain on every run.
* **An `allow_failure` job going red is not a broken pipeline.** The notifier reports advisory failures
  under the verdict rather than in it. Folding them into the headline would cry wolf until the headline
  stopped being read, which is the failure mode notifications actually have.

## Still open from the courseware

Named in the decks, deliberately not built yet:

* **Feature store (Feast)** — row 3 of deck 02's toolchain table, and Gap 6 in [[Lecture Alignment]].
  Still not built, and now for a better reason: the **train/serve skew gate** landed on 2026-09-17
  (`app/ml/feature_contract.py`), which is the property Feast is bought for. The artifact records a
  fingerprint of the extractor's configuration AND behaviour at training time; loading verifies it,
  and `test:model-gate` checks it against the artifact the pipeline just built. The old argument
  ("both paths import the same `features.py`") was true of the source and silent about the artifact:
  a keyword edit changes what the features MEAN while every name and the dimension stay put.
* **Batch serving** — deck 08 treats real-time and batch as the two serving modes. The job exists
  since 2026-09-17 (`app/ml/batch_score.py`: re-score the pending escalation queue, report the
  cases the current model now ranks MORE urgent, mutate nothing). What is still missing is a place
  to schedule it against: `store.Store` is in-memory, so an out-of-process nightly run sees an empty
  queue. Batch serving needs a durable store, which is [[Infra-Dependent Work 2026-09-02]], not a
  cron line.
* **Blue-green and rolling deploys** — deck 02 p37 names four strategies. Shadow and canary are now
  implemented; blue-green and rolling are not, and need a real deployment target first.
* **GitHub Actions** — deck 02's appendix and a separate `githubAction.pdf` teach it, while this project
  runs GitLab CI. Mirroring 69 jobs into a second CI system is a real maintenance cost for a portability
  demo; flagged as a decision for James rather than taken unilaterally.
* **Aggregated logging (ELK/Loki) and cloud deploy** — Gaps 3 and 5 in
  [[Lecture Alignment]]. Availability metrics (Gap 4) closed 2026-09-17: yield, harvest and
  uptime are recording rules in the `careroute-availability` group.

## Design rules carried over from the reference

* **Fail closed.** A canary step with missing metrics ABORTS. "Prometheus returned nothing" and "the
  service is healthy" are the same empty dict, and only one is safe to widen on. Same rule as
  absent-is-not-clean in the security report and the PII gate.
* **The rule lives with the thing it measures, not in the YAML.** SLO check in the locustfile, ramp
  logic in `app/ml/canary.py`, thresholds in `guardrail_score.py`. A rule embedded in
  `.gitlab-ci.yml` cannot be unit tested, and an untested rollback rule is found to be wrong during
  the incident it exists to handle.
* **A gate that is wrong every run is one everybody ignores.** Hence `scan:no-live-credentials`
  covers only *agent-reachable* credentials (OneMap, LLM provider, Onyx) and deliberately excludes CI
  plumbing (`SONAR_TOKEN`, the trigger token, the MLflow token) — those must be present for their
  jobs to work.
* **A security tool must not re-publish what it finds.** PII findings carry the **redacted** line as
  their excerpt, and the credential guard reports credential **names** only. Masking by construction,
  not by remembering to mask.

## What measuring context precision found

Two things, neither of which the ranking metrics could see:

* **A perfect ranker still scores 0.484 on precision** if it always returns two documents
  over a corpus with one relevant document per presentation. Precision was capped by the
  **context assembly policy**, not by retrieval quality — a fixed `top_k` is a quota, and
  half of every prompt's guidance block was padding. `rag.CONTEXT_MARGIN` makes `top_k` a
  ceiling instead; 17 of 32 gold queries now answer with one document.
* **The optional extra crashes the process, and CI cannot see it.** Scoring E12 required
  installing `requirements-agentic.txt` for the first time. The first onnxruntime import
  lands on the worker thread every retrieval runs on, and takes the interpreter down with
  an access violation — through every `except Exception` on the path, because a SIGSEGV is
  not an exception. Fixed by warming the backend on the main thread
  (`rag_embed.warm()`, called from the lifespan and from `pytest_configure`). CI installs
  no extras, so a green pipeline said nothing about it.

## Known limits

* `scan:no-live-credentials` reads the process environment. In CI that is right — credentials arrive
  as CI variables. It would **not** see a credential sitting only in a repo-root `.env` that the app
  loads at import; committed secrets are gitleaks' job, and `.env` is gitignored.
* Presidio is optional. Without it the PII gate runs deterministic rules only and says so in
  `backendStatus` — names and addresses need the NER model. The CI job pip-installs it best-effort.
* The canary job performs no traffic shifting: there is no deployment target or reachable Prometheus
  yet, and it prints that rather than a fake ramp. See [[Infra-Dependent Work 2026-09-02]].
* Every new scanner/gate job is **unrun until a pipeline executes**. The YAML parses, all 69 jobs
  resolve their `needs`, and the embedded Python compiles — but only GitLab can confirm the images
  pull and the flags are right.
