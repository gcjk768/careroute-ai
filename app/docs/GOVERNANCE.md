# AI Governance Self-Assessment — PDPC Model AI Governance Framework

This maps CareRoute AI to the **four pillars** of Singapore's *Model AI Governance Framework* (PDPC /
IMDA), as taught in the Explainable & Responsible AI module (2.0L / 2.2P). It complements
[`../ASPECTS.md`](../ASPECTS.md) (which maps to the *course modules*) by mapping to the *governance
framework*. It is a prototype self-assessment, not an accredited audit.

---

## Pillar 1 — Internal Governance Structures & Measures
*Clear roles, risk management, and controls for responsible AI.*

- **Human authority:** the Human-in-the-Loop and Reflection agents route low-confidence, high-acuity,
  and safety-flagged cases to a clinician; the clinician records the final decision.
- **Risk management:** an OWASP-mapped register ([`../backend/SECURITY.md`](../backend/SECURITY.md)),
  a deterministic safety-override that the LLM cannot lower, and a **kill switch** to revoke autonomy.
- **Traceability & accountability:** a per-case, **tamper-evident (SHA-256 hash-chained)** audit trail
  of every agent's reasoning, tools, and decision (`GET /api/cases/{id}/audit`).
- **Continuous assurance:** MLSecOps CI gates every change with tests, a fairness gate, and security scans.

## Pillar 2 — Determining the Level of Human Involvement
*Match human oversight to the severity and reversibility of the decision.*

- CareRoute operates in a **tiered oversight** mode by design — human-IN-the-loop for red-flag, P1/P2 and low-confidence cases (a clinician must decide before the patient sees a final answer), human-OVER-the-loop for confident P3–P5 cases (the patient receives the recommendation; clinicians review via the audit trail, ground-truth labels and the feedback path): the AI never
  finalises care autonomously. The escalation policy is explicit — **safety trigger OR confidence
  below threshold OR high acuity (P1/P2) ⇒ mandatory clinician review.**
- The commercial/harm trade-off is deliberately conservative: the system escalates rather than risk
  under-triage.

## Pillar 3 — Operations Management
*Data quality, model robustness, explainability, and monitoring across the lifecycle.*

- **Data & model transparency:** documented in the [Model Card](../backend/MODEL_CARD.md).
- **Explainability:** real **SHAP** contributions shown to patient and clinician; a plain-language
  rationale; **cited guidance** via lexical TF-IDF retrieval over a committed corpus (not vector search — see `ASPECTS.md`); a single-edit **counterfactual** ("would move to P2 if breathlessness were also reported") for the patient.
- **Fairness:** accuracy gap (before/after mitigation), **Demographic Parity**, **Equal Opportunity**,
  and **counterfactual** (sex-flip) audits at `GET /api/fairness`.
- **Robustness & security:** input/output guardrails, PII redaction, least-privilege tool access,
  rate limiting.
- **Monitoring:** PSI **drift** (data/target/concept), **Prometheus** metrics (`/metrics`), model
  versioning + artifact persistence, and a **Fairlearn** CI fairness gate.

## Pillar 4 — Stakeholder Interaction & Communication
*Make AI use known; provide plain-language explanations and feedback/redress channels.*

- **Disclosure:** the patient UI states that triage is AI-assisted and a clinician makes the decision.
- **Understandability:** results are explained in plain language with the top contributing symptoms.
- **Feedback / redress:** patients can rate a decision (helpful / not helpful) via
  `POST /api/cases/{id}/feedback`, which is recorded into the case audit trail.
- **Continuity:** episodic recall (`GET /api/sessions/{id}/history`) lets a returning session see prior visits.

---

### Summary matrix
| Pillar | Primary evidence |
|--------|------------------|
| Internal Governance | HITL + Reflection, kill switch, hash-chained audit, SECURITY.md, CI gates |
| Human Involvement | Mandatory-escalation policy (safety / low-confidence / high-acuity) |
| Operations Management | Model Card, Datasheet, SHAP, fairness suite, drift + Prometheus monitoring, fairness gate |
| Stakeholder Communication | AI-use disclosure, plain-language explanations, feedback endpoint |

---

## Part 2 — XRAI Day 3 self-assessment instruments

The PDPC framework above asks *what controls exist*. The four instruments below ask
*how mature the practice around them is*, and they are scored here against the same
evidence.

**Read the unit of assessment first.** All four were written to score an
**organisation** — C-suite sponsorship, hiring pipelines, cross-portfolio policy. The
unit here is a **four-person student project with one model**, so several dimensions
have no honest answer and are marked N/A rather than given a flattering one. Scoring
a practice-module team "Leading" on talent strategy would say nothing true about the
work. Where a dimension *does* translate, it is scored strictly and the evidence is
named.

### AI Governance Maturity (Levels 0–5)

| Level | Definition | CareRoute |
|---|---|---|
| 0 | No AI lifecycle governance | passed |
| 1 | AI policies guide the lifecycle | ✅ `SECURITY.md`, `GOVERNANCE.md`, `MODEL_CARD.md`, `DATASHEET.md`, `_Conventions.md` |
| 2 | Common metric set governs the lifecycle | ✅ one shared threshold set in `app/ml/model.py` (accuracy, red-flag recall, fairness gap, calibration ECE) used by the release gate, the CI gate and the report alike |
| 3 | Enterprise data and AI catalog | 🟡 **partial** — MLflow registry, DVC-tracked data, content-addressed `triage_dataset.meta.json`. It is a catalogue of *one* project, not an enterprise |
| 4 | Automated validation and monitoring | ✅ 69 CI jobs; model/data/lineage/fairness/drift/security all gate automatically; drift monitor (Evidently when it imports, built-in PSI otherwise — the report JSON records which ran) with a closed retrain loop; live LABELLED metrics from clinician ground truth (`performance.labelled`) |
| 5 | Fully automated AI lifecycle | ❌ **not met, and not claimable** — there is no deployment target. Canary and shadow deploy exist as code and print their intent rather than shifting traffic |

**Assessed: Level 4 on the validation-and-monitoring axis, Level 3 partial on
cataloguing.** Level 5 is blocked by infrastructure the project deliberately does not
have, not by missing pipeline work — see *Infra-Dependent Work* in the vault.

### AI Maturity (Accenture: Strategy, Data & AI Core, Talent & Culture, Responsible AI)

| Dimension | Score | Basis |
|---|---|---|
| Strategy & Sponsorship | **N/A** | No C-suite, no portfolio, no investment decision to assess |
| Data & AI Core | **Builder** | Reproducible seeded data, content-addressed artefacts, feature code shared by train and serve, experiment tracking + registry. No feature store and no cloud platform |
| Talent & Culture | **N/A** | A student team is not a talent model |
| Responsible AI | **Achiever** | The one dimension that fully translates: responsible-AI practice is *industrialised* rather than documented — fairness, explainability, safety and security are **blocking CI gates**, not review steps. This is the dimension the instrument cares most about and the one this project is strongest on |

### AI Ethics Readiness (IEEE EAD: Lagging / Basic / Advanced / Leading)

| Dimension | Score | Basis |
|---|---|---|
| Ethics embedded in decisions vs bolted on | **Advanced** | Fairness mitigation is inside `build_artifact()` and gated before persistence; the safety override can only raise acuity and cannot be overruled by the model. Ethics is in the control flow, not in a review checklist |
| Training, support, resources | **Basic** | Course material only; no review board, no external ethics advisory |
| Leadership buy-in | **N/A** | See above |
| Transparency to affected people | **Advanced** | AI-use disclosure, plain-language explanation with contributing symptoms, feedback/redress endpoint, and an audit trail a patient's clinician can inspect |

**Not Leading, and the gap is honest:** "Leading" requires ethics infused across roles
and onboarding in an organisation. There is no organisation.

### AI Readiness (AI Singapore AIRI)

AIRI scores an industry organisation's readiness to *adopt* AI — business drivers,
data infrastructure, workforce, governance. Three of its four axes assess an adopting
enterprise and have no counterpart here. The governance axis is answered by Part 1 and
by the maturity table above, so scoring AIRI separately would restate the same evidence
under a different heading. **Recorded as assessed and not applicable**, rather than
silently skipped.

### What these instruments actually surfaced

Two things worth carrying into the report:

1. **The project is strongest exactly where the instruments weight hardest** —
   Responsible AI as an industrialised, automated practice rather than a document.
2. **Every unmet level is blocked by the absence of an organisation or a deployment
   target, not by missing engineering.** That is a scope decision and is stated as one.
