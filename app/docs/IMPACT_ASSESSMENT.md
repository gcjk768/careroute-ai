# CareRoute AI — Impact Assessment and Human-Oversight Design

*Explainable & Responsible AI, Day 1 (adoption considerations, harm) and Day 3 (PDPC Model AI
Governance Framework, section 3.1 "Did your organization conduct an impact assessment (e.g.
probability and/or severity of harm) on individuals and organizations who are affected by the AI
solution?").*

This document answers 3.1 for CareRoute and uses the answer to DERIVE the level of human
involvement per case, instead of asserting one level for the whole system. Every number is taken
from a measured artifact named beside it. CareRoute is a Practice-Module prototype trained on
synthetic data; this assessment describes the design and its evidence, not a clinical validation.

---

## 1. Scope

**Decision supported:** an emergency-department acuity level (P1 resuscitation to P5 self-care),
the matching care tier, and a nearby facility for a patient describing symptoms in free text.

**Decision NOT made by the system:** diagnosis, treatment, or dispatch. The system recommends; for
the cases defined in section 4, a clinician decides before the recommendation stands.

| Acuity | Care tier (`models.ACUITY_TABLE`) |
|---|---|
| P1 Resuscitation, P2 Emergent | Emergency Department |
| P3 Urgent | Urgent Care |
| P4 Non-urgent | GP |
| P5 Self-care | Telehealth |

---

## 2. Stakeholders

| Stakeholder | How the system affects them | What they need from it |
|---|---|---|
| Patients | Told where to seek care and how urgently | A safe recommendation, a reason they can understand, a way to question it |
| Vulnerable patients: 65+, 0–17, patients who do not give an age, non-native English speakers | Carry the highest cost of under-triage; the synthetic data deliberately under-represents 65+ | Performance at parity with other groups; not being silently treated as a default adult |
| Clinicians reviewing escalations | Their queue is the safety net; their attention is finite | Only the cases that need them; evidence, not a bare verdict; their decision recorded as theirs |
| Emergency departments and GP / urgent-care providers | Receive the routed demand | Fewer avoidable ED visits without missed emergencies |
| Operator of the service (the healthcare organisation) | Accountable for outcomes, data protection and oversight | Audit trail, monitoring, the ability to switch AI off |
| Regulators (PDPC, MOH) | Oversight of personal data and clinical safety | Traceability and a documented governance basis |

---

## 3. Harms: probability and severity

**Severity scale:** 5 catastrophic (death or permanent harm), 4 serious, 3 moderate, 2 minor,
1 negligible. **Probability** uses measured rates where one exists.

| # | Harm | Who | Severity | Probability evidence | Control |
|---|---|---|---|---|---|
| H1 | **Under-triage of an emergency** (P1/P2 routed lower) | Patient | **5** | Model-only red-flag recall 0.9572 held-out; calibrated pipeline after post-processing 0.9817 (`MODEL_CARD.md`). End-to-end red-flag recall on the gold set is gated at **1.0** (`tests/test_triage_eval.py`) because the deterministic red-flag table forces acuity regardless of the model | Deterministic raise-only red flags; Reflection forces every P1/P2 to a clinician; LLM critic can escalate further; release gate recall ≥ 0.95 |
| H2 | **Under-triage within a subgroup** | Vulnerable patients | **5** | Equal Opportunity gap (severe-class TPR) 0.0789 before mitigation, **0.0377** after (`postProcessing`) | Age-band oversampling + raise-only group thresholds; gap gated ≤ 0.10 before release; live labelled subgroup metrics |
| H3 | Under-triage P3 → P4/P5 | Patient | 4 | Overall held-out accuracy 0.9173; 5-fold CV 0.9103 ± 0.0063 | Low confidence escalates; counterfactual shows what would change the result; critic review |
| H4 | **Over-triage** (non-urgent sent to ED) | ED, other patients, the patient | 2 | FPR gap across groups 0.013 before, 0.0551 after post-processing — the deliberate trade for H2 | Reported beside the TPR gap, not averaged away |
| H5 | Wrong facility (closed, unreachable, beyond travel limit) | Patient | 3 | Care-Routing final deterministic check rejects a closed or out-of-limit clinic before publishing | Verified candidates only; circuit breaker + labelled local estimate when OneMap fails |
| H6 | Clinician automation bias (rubber-stamping) | Patient, clinician | 4 | Not measured | Evidence shown with its provenance (SHAP vs LLM vs keyword), final acuity is an explicit clinician choice, agreement logged as ground truth |
| H7 | Clinician overload (too many escalations) | Clinicians, all escalated patients | 3 | HITL trigger gated on specificity as well as recall (E5). **Gap:** the LLM critic's added escalations are observable but not yet gated | `careroute_escalations_total`; critic switchable by `CAREROUTE_LLM_CRITIC` |
| H8 | Patient over-reliance on a self-care result | Patient | 3 | Not measured | Safety-net advice, AI-use disclosure, counterfactual stated as "describes the model, not medical advice", review request (see §6) |
| H9 | Personal-data exposure | Patient | 4 | PII egress gate in CI | Redaction before any model, storage, feedback or clinician note; no raw text on the agent bus; local embeddings by default |
| H10 | Service unavailable or AI disabled | All | 3 | — | Every agent has a deterministic fallback; the pipeline runs with no LLM at all |

---

## 4. Derived level of human involvement

The PDPC framework describes three levels — human-in-the-loop, human-over-the-loop (humans
intervene when the situation calls for it, e.g. below a confidence threshold) and
human-out-of-the-loop (not practical to review every recommendation). It also names the factors
that set the level: risk appetite, user experience and operational cost. Applying section 3:

| Case condition | Worst harm if wrong | Level | Mechanism |
|---|---|---|---|
| Deterministic red flag fired | H1, severity 5 | **Human-in-the-loop** | HITL escalates on `safety_triggered` |
| Final acuity P1 or P2, any confidence | H1, severity 5 | **Human-in-the-loop** | Reflection forces escalation of every unescalated P1/P2 |
| Calibrated confidence < 0.5 | H1–H3 | **Human-in-the-loop**, or one clarifying question first when confidence is the only reason | HITL on `confidence < CONFIDENCE_THRESHOLD`; ask/proceed handshake |
| Recurrence across visits | H1–H3 | **Human-in-the-loop** | `cross_visit_escalation` |
| LLM critic judges the decision unsafe or inconsistent | Any | **Human-in-the-loop** | Critic `escalate` (monotone) |
| Confident P3 | H3, severity 4 | **Human-over-the-loop** | Patient receives the recommendation; clinicians oversee through the audit trail, live labelled metrics and review requests |
| Confident P4 / P5 | H3/H8, severity 3 | **Human-over-the-loop** | As above, plus explanation, counterfactual and safety-net advice |

**Why not human-out-of-the-loop anywhere.** The framework reserves it for high volumes of
low-stakes micro-decisions. A triage recommendation is neither: the lowest-acuity band still
carries severity-3 harm, and case volume in a clinic setting is reviewable.

**Why the threshold is 0.5.** `agents/base.CONFIDENCE_THRESHOLD = 0.5` applies to CALIBRATED
probabilities (isotonic, expected calibration error 0.0216, gated ≤ 0.05). With calibration that
good, confidence below 0.5 means the model is more likely wrong than right about the acuity it
chose — the point at which its recommendation should not stand without a person.

**Risk appetite, user experience, cost.** Appetite for H1/H2 is zero, which is why every
safety control is raise-only and red-flag recall is gated at 1.0 end to end. Asking the patient
one question before escalating a low-confidence case is the user-experience concession, and it is
never offered when a red flag, P1/P2 or recurrence is present. Clinician cost is the reason
confident P3–P5 cases are over-the-loop rather than in it.

---

## 5. Monitoring that keeps this assessment true

| Signal | Where | Triggers review when |
|---|---|---|
| Red-flag recall, accuracy, subgroup gap, Equal Opportunity gap, ECE, explanation agreement | Release gates in `app/ml/train.py`, CI model and fairness gates | Any gate fails |
| Live accuracy, recall and subgroup metrics on clinician-labelled cases | `app/ml/monitor.py` → `performance.labelled` | Labelled recall or subgroup gap departs from the held-out values |
| Share of predictions made with a defaulted age band | `performance.labelled.ageDefaultedShare` | It grows, because the age-aware mitigation cannot act on those cases |
| Data drift (PSI) | `monitor.py` drift gate | PSI ≥ 0.25 |
| Escalation volume, critic actions, tool-call refusals | Prometheus `/metrics` | Queue growth without a matching rise in acuity |
| Patient review requests and feedback | Audit trail, clinician queue | Any review request |

## 6. Residual risks and open items

- The model is trained and evaluated on **synthetic** data. None of the probabilities in section 3
  are clinical evidence.
- H6 (automation bias) and H8 (over-reliance) are unmeasured.
- H7: the critic's escalation rate is not gated.
- The audit log is in-memory and staff identity is self-asserted by a mock login (see
  `backend/SECURITY.md` T8/T9).

## 7. Ownership and review

Owner: platform lead (James Koh). Review this assessment when the model is retrained, a gate
threshold changes, a new agent or tool is added, or a monitoring trigger in section 5 fires. There
is no clinical governance board for this prototype; in a deployment, sign-off would sit with the
operator's clinical safety officer.
