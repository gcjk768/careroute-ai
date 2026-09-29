'use client'
// Clinician dashboard — the [Responsible-AI] human-in-the-loop surface. Escalated
// cases (red-flag or low-confidence) queue here for a human decision that
// SUPERSEDES the AI recommendation and is written to the audit trail. This is the
// accountability / final-authority control the module brief asks for.
import { useEffect, useMemo, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { getEscalations, getEscalation, postDecision } from '@/lib/api'
import { getStaff } from '@/lib/auth'
import { ACUITY } from '@/lib/acuity'
import AcuityBadge from '@/components/AcuityBadge'
import PortalExplainer from '@/components/PortalExplainer'
import styles from './ClinicianDashboard.module.css'

// Offline fallback so the dashboard is always populated for a demo.
const MOCK = [
  {
    id: 'ESC-4471', caseId: 'CR-88213', reason: 'Red-flag: CARDIAC_CHEST_PAIN', confidence: 0.98,
    acuity: { code: 'P1', label: 'Resuscitation', score: 1 }, createdAt: minsAgo(4), status: 'pending',
    patientSummary: '58M — crushing chest pain radiating to left arm, diaphoretic, breathless.',
    normalisedSymptoms: 'chest pain (crushing, radiating L arm); diaphoresis; dyspnoea; onset ~30 min',
    language: 'English', rationale: 'Deterministic red-flag CARDIAC_CHEST_PAIN forced acuity to P1 independent of the classifier (est. P2). Non-overridable; routed for immediate clinician review.',
    evidence: ['Onset < 1h', 'Radiation to left arm', 'Associated diaphoresis + dyspnoea'],
    citations: [{ title: 'ESI v4 — cardiac presentations', snippet: 'Chest pain with cardiac features → level 1-2.', source: 'AHRQ' }],
    // [Clinician-Handoff] Safety-triggered escalation → grounded (safety rule fired).
    handoffSummary: '58-year-old male with crushing chest pain radiating to the left arm, diaphoretic and breathless, onset ~30 minutes ago. Assessed P1 at 98% confidence; the CARDIAC_CHEST_PAIN safety rule fired, forcing this acuity independently of the classifier’s own estimate. Escalated for immediate clinician review — time-critical presentation.',
    handoffCitations: [{ title: 'ESI v4 — cardiac presentations', snippet: 'Chest pain with cardiac features → level 1-2.', source: 'AHRQ' }],
    handoffQuestions: ['Any history of cardiac disease or prior MI?', 'Has aspirin or GTN been given pre-arrival?'],
  },
  {
    id: 'ESC-4468', caseId: 'CR-88207', reason: 'Low confidence (0.52)', confidence: 0.52,
    acuity: { code: 'P3', label: 'Urgent', score: 3 }, createdAt: minsAgo(11), status: 'pending',
    patientSummary: '34F — vague fatigue and intermittent abdominal ache, difficult to localise.',
    normalisedSymptoms: 'fatigue; intermittent abdominal ache (poorly localised); 3 days',
    language: '中文', rationale: 'Classifier confidence 0.52 fell below the 0.60 escalation band. No red-flag matched; ambiguous presentation warrants human judgement.',
    evidence: ['Confidence below band', 'Non-specific symptoms', 'No red-flag match'],
    citations: [{ title: 'Abdominal pain triage', snippet: 'Non-specific abdominal pain benefits from clinician assessment.', source: 'NICE CKS' }],
    // [Clinician-Handoff] Confidence-driven escalation → NOT grounded (no safety rule fired,
    // so handoff.py's `_should_ground` skips retrieval — no handoffCitations here on purpose).
    handoffSummary: '34-year-old female with three days of vague fatigue and intermittent, poorly localised abdominal ache. Classifier confidence (52%) fell below the escalation threshold with no red-flag rule matched — escalated for human judgement rather than a specific clinical concern.',
    handoffQuestions: ['Any associated fever, nausea, or change in bowel habit?', 'Any relevant menstrual or gynaecological history?'],
  },
  {
    id: 'ESC-4462', caseId: 'CR-88191', reason: 'Red-flag: STROKE_FAST', confidence: 0.95,
    acuity: { code: 'P1', label: 'Resuscitation', score: 1 }, createdAt: minsAgo(23), status: 'pending',
    patientSummary: '71M — unilateral face droop, slurred speech, right-arm weakness.',
    normalisedSymptoms: 'facial droop (unilateral); dysarthria; right arm weakness; onset ~45 min',
    language: 'Bahasa Melayu', rationale: 'FAST-positive stroke signs triggered STROKE_FAST red-flag → P1. Time-critical; escalated for immediate pathway activation.',
    evidence: ['FAST positive', 'Onset within window', 'Focal deficit'],
    citations: [{ title: 'Stroke — FAST pathway', snippet: 'FAST-positive → emergency stroke pathway.', source: 'CareRoute policy' }],
  },
  {
    id: 'ESC-4455', caseId: 'CR-88180', reason: 'Self-harm risk flag', confidence: 0.9,
    acuity: { code: 'P2', label: 'Emergent', score: 2 }, createdAt: minsAgo(38), status: 'decided',
    patientSummary: '22F — expressing thoughts of self-harm, low mood for two weeks.',
    normalisedSymptoms: 'low mood (2 weeks); passive self-harm ideation; social withdrawal',
    language: 'English', rationale: 'SELF_HARM_RISK flag escalated to emergent with a safe holding message pending clinician contact.',
    evidence: ['Explicit ideation phrase', 'Duration of low mood', 'Withdrawal'],
    citations: [{ title: 'Mental-health triage', snippet: 'Any self-harm ideation is escalated for human assessment.', source: 'CareRoute policy' }],
  },
]
function minsAgo(m) { return new Date(Date.now() - m * 60000).toISOString() }
function ago(iso) {
  const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000))
  return m < 1 ? 'just now' : m < 60 ? `${m}m ago` : `${Math.round(m / 60)}h ago`
}

const DECISIONS = ['Agree with recommendation', 'Escalate to ED now', 'Adjust care tier', 'Discharge with advice']

// The backend's acuity enum (P1_RESUSCITATION …). The mock rows above use the
// short badge code ('P1'), so normalise either form to the enum the decision
// endpoint validates `finalAcuity` against.
const ACUITY_CODES = Object.keys(ACUITY)
function toFullCode(code) {
  if (!code) return ''
  if (ACUITY[code]) return code
  return ACUITY_CODES.find((k) => ACUITY[k].code === code) || ''
}

// [Responsible-AI] Provenance of the feature-contribution chips (mirrors
// PatientTriage.jsx). A clinician must be able to tell a SHAP value from a
// language model's self-report before sanity-checking it.
const EXPLANATION_SOURCE_LABEL = {
  shap: 'SHAP values from the trained model',
  llm: 'self-reported by the language model',
  keyword: 'keyword surrogate (fallback path)',
}

/** Why the decision was not written, in terms a reviewer can act on. */
function decisionFailureReason(err) {
  if (err?.status === 404) return 'This escalation no longer accepts a decision — it may already have been decided.'
  if (err?.status) return `The service rejected the decision (HTTP ${err.status}).`
  return 'The decision could not be sent — the service is unreachable.'
}

export default function ClinicianDashboard() {
  const [list, setList] = useState(null)
  const [offline, setOffline] = useState(false)
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [decision, setDecision] = useState(DECISIONS[0])
  // [XRAI][MLOps] The clinician's FINAL acuity is the ground-truth label for
  // this case (store._record_ground_truth). Defaults to the model's own code, so
  // agreeing is one click and any change is an explicit override.
  const [finalAcuity, setFinalAcuity] = useState('')
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [submitError, setSubmitError] = useState('')

  useEffect(() => {
    getEscalations()
      .then((d) => { setList(d); setSelectedId(d[0]?.id ?? null) })
      .catch(() => { setOffline(true); setList(MOCK); setSelectedId(MOCK[0].id) })
  }, [])

  const summary = useMemo(() => (list || []).find((e) => e.id === selectedId), [list, selectedId])

  // NOTE for Heriz (HITL / clinician handoff owner) — changed 2026-09-15 by James
  // during the courseware audit. The `enrich()` helper that used to live here
  // FABRICATED feature contributions (a fixed 0.85, 0.67, 0.49, 0.31 ramp over
  // the evidence strings) whenever the backend attached none. On the clinician
  // surface that is invented explainability: a reviewer could sanity-check
  // weights no model ever produced. It is removed. A case with no explanation now
  // says so (see the "Feature contributions" block below), and every real
  // explanation is labelled with its provenance (SHAP / LLM / keyword) from the
  // new `explanationSource` field the backend now sends. Nothing else in your
  // decision flow changed. Shout if the empty state reads wrong to you.
  useEffect(() => {
    if (!selectedId) return setDetail(null)
    setNote('')
    setDecision(DECISIONS[0])
    setFinalAcuity('')
    setSubmitError('')
    if (offline) { setDetail(MOCK.find((e) => e.id === selectedId)); return }
    getEscalation(selectedId).then(setDetail).catch(() => setDetail(MOCK.find((e) => e.id === selectedId)))
  }, [selectedId, offline])

  const pending = (list || []).filter((e) => e.status === 'pending')

  // A decision is an accountability record. Showing it as "recorded" when the
  // POST failed would tell a clinician the audit trail holds something it does
  // not — so the optimistic update is applied ONLY on success, and a failure
  // leaves the case pending with a visible reason to retry.
  async function submit() {
    if (!summary) return
    setSaving(true)
    setSubmitError('')
    // [XRAI] Accountability: the decision is attributed to the SIGNED-IN staff
    // member (lib/auth), never a hard-coded name — the audit trail and the
    // ground-truth log both record who decided.
    const clinician = getStaff()?.name || 'Staff'
    const body = {
      decision, note, clinician,
      finalAcuity: finalAcuity || toFullCode(detail?.acuity?.code) || undefined,
    }
    if (!offline) {
      try {
        await postDecision(summary.id, body)
      } catch (err) {
        setSubmitError(`${decisionFailureReason(err)} The case is still pending — please try again.`)
        setSaving(false)
        return
      }
    }
    setList((l) => l.map((e) => (e.id === summary.id ? { ...e, status: 'decided' } : e)))
    setDetail((d) => (d ? { ...d, status: 'decided', decision, note } : d))
    setSaving(false)
  }

  return (
    <div className={styles.container}>
      <header className={styles.header}>
        <div>
          <div className="eyebrow">Human-in-the-loop</div>
          <h1 className={styles.title}>Clinician review</h1>
          <p className={styles.intro}>
            Escalated cases await your decision. The clinician holds final authority — your decision supersedes the
            AI recommendation and is written to the audit trail.
          </p>
        </div>
        <div className={styles.queueCard}>
          <div className={styles.queueCount}>{pending.length}</div>
          <div className={styles.queueCardLabel}>in queue</div>
        </div>
      </header>

      <PortalExplainer
        title="The human safety net for the AI’s decisions"
        lede="The AI never has the final word on an urgent case. Whenever it isn’t confident enough, or a hard clinical red-flag rule fires (e.g. chest pain), the case is held here for a real clinician to decide. Your decision overrides the AI and is written to the audit trail — this is the “AI assists, a clinician decides” principle in practice."
        looking={[
          'A queue (left) of cases the AI escalated, each tagged with why it was escalated (a red-flag rule or low confidence) and how long it has been waiting.',
          'A detail panel (right) showing the AI’s reasoning, the evidence it relied on, its confidence score, and the feature contributions behind its guess — so you can sanity-check it.',
        ]}
        using={[
          'Click a case in the queue to open its full detail.',
          'Review the rationale, evidence and confidence the AI provided.',
          'Choose a decision, add an optional clinical note, and Submit — this supersedes the AI and is logged to the audit trail.',
        ]}
      />

      {offline && (
        <p className={styles.offlineNote}>⚠ backend offline — showing seeded demo escalations.</p>
      )}

      <div className={styles.grid}>
        {/* Queue */}
        <div className={styles.queueCol}>
          {(list || []).map((e) => {
            const active = e.id === selectedId
            return (
              <button
                key={e.id}
                onClick={() => setSelectedId(e.id)}
                className={`focusable ${styles.queueItem} ${active ? styles.queueItemActive : styles.queueItemInactive}`}
              >
                <div className={styles.queueItemHeader}>
                  <AcuityBadge code={e.acuity?.code} size="sm" showLabel={false} />
                  <span className={styles.queueTime}>{ago(e.createdAt)}</span>
                </div>
                <p className={styles.queueSummary}>{e.patientSummary}</p>
                <div className={styles.queueMeta}>
                  <span className={styles.queueReason}>{e.reason}</span>
                  {e.slaBreached && e.status === 'pending' && (
                    <span className={`${styles.queueStatus} ${styles.statusOverdue}`} title={`Review SLA passed at ${e.slaDueAt}`}>
                      SLA breached
                    </span>
                  )}
                  <span className={`${styles.queueStatus} ${e.status === 'pending' ? styles.statusPending : styles.statusDecided}`}>
                    {e.status}
                  </span>
                </div>
              </button>
            )
          })}
          {!list && <p className={styles.loading}>Loading queue…</p>}
        </div>

        {/* Detail */}
        <div className={styles.detailCol}>
          <AnimatePresence mode="wait">
            {detail ? (
              <motion.div key={detail.id} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className={`card ${styles.detailCard}`}>
                <div className={styles.accentBar} style={{ background: 'var(--coral)' }} />
                <div className={styles.detailBody}>
                  <div className={styles.detailHeader}>
                    <div>
                      <div className="eyebrow">Case {detail.caseId} · {detail.id}</div>
                      <h3 className={styles.detailTitle}>{detail.patientSummary}</h3>
                    </div>
                    <div className={styles.detailAcuity}>
                      <AcuityBadge code={detail.acuity?.code} size="lg" />
                      <div className={styles.detailMeta}>conf {Math.round((detail.confidence || 0) * 100)}% · {detail.language}</div>
                    </div>
                  </div>

                  <Section label="Normalised symptoms">{detail.normalisedSymptoms}</Section>
                  <Section label="Rationale">{detail.rationale}</Section>

                  {detail.evidence?.length > 0 && (
                    <div className={styles.block}>
                      <div className="eyebrow">Evidence</div>
                      <div className={styles.chipRow}>
                        {detail.evidence.map((ev, i) => (
                          <span key={i} className={styles.evidenceChip}>{ev}</span>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Explainability — signed feature contributions the clinician can sanity-check.
                      Labelled with provenance; never synthesised (see the note above `useEffect`). */}
                  {detail.explanation?.length > 0 ? (
                    <div className={styles.block}>
                      <div className="eyebrow">
                        Feature contributions · {EXPLANATION_SOURCE_LABEL[detail.explanationSource] || 'source not reported'}
                      </div>
                      <div className={styles.chipRow}>
                        {detail.explanation.map((c, i) => {
                          const pos = (c.weight || 0) >= 0
                          return (
                            <span
                              key={i}
                              className={styles.featureChip}
                              style={{ color: pos ? '#C0392B' : '#3E7A5E', background: pos ? 'rgba(229,83,59,0.08)' : 'rgba(62,122,94,0.10)' }}
                            >
                              {c.feature} {pos ? '+' : ''}{(c.weight || 0).toFixed(2)}
                            </span>
                          )
                        })}
                      </div>
                    </div>
                  ) : (
                    <div className={styles.block}>
                      <div className="eyebrow">Feature contributions</div>
                      <p className={styles.muted}>
                        No feature contributions were attached to this case, so none are shown. The panel never invents them.
                      </p>
                    </div>
                  )}

                  {/* Handoff summary — the Clinician-Handoff worker's synthesized note for a
                      time-pressured reviewer, sitting after the raw evidence/explanation and
                      before the decision form. Guards mean this silently doesn't render until
                      the backend actually populates these fields (see docs/vault/Clinician
                      Handoff UI.md). */}
                  {detail.handoffSummary && (
                    <div className={styles.handoffBox}>
                      <div className="eyebrow">Handoff summary</div>
                      <p className={styles.sectionText}>{detail.handoffSummary}</p>

                      {detail.handoffCitations?.length > 0 && (
                        <div className={styles.chipRow}>
                          {detail.handoffCitations.map((c, i) => (
                            <span key={i} className={styles.evidenceChip} title={c.snippet}>
                              {c.title} · {c.source}
                            </span>
                          ))}
                        </div>
                      )}

                      {detail.handoffQuestions?.length > 0 && (
                        <div className={styles.block}>
                          <div className="eyebrow">Suggested follow-up questions</div>
                          <ul className={styles.handoffQuestions}>
                            {detail.handoffQuestions.map((q, i) => <li key={i}>{q}</li>)}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Decision */}
                  {detail.status === 'decided' ? (
                    <div className={styles.decidedBox}>
                      <div className={styles.decidedLabel}>recorded decision</div>
                      <p className={styles.decidedDecision}>{detail.decision || 'Decision recorded'}</p>
                      {detail.note && <p className={styles.decidedNote}>“{detail.note}”</p>}
                      {(detail.clinician || detail.finalAcuity) && (
                        <p className={styles.decidedBy}>
                          {detail.clinician ? `Decided by ${detail.clinician}` : 'Decided'}
                          {detail.finalAcuity ? ` · final acuity ${ACUITY[detail.finalAcuity]?.code || detail.finalAcuity}` : ''}
                        </p>
                      )}
                      {offline && (
                        <p className={styles.offlineDecisionNote}>
                          Demo mode — recorded in this browser only; the backend is offline, so nothing was written to the audit trail.
                        </p>
                      )}
                    </div>
                  ) : (
                    <div className={styles.recordBox}>
                      <div className={`eyebrow ${styles.mb3}`}>Record decision</div>
                      <div className={styles.decisionRow}>
                        {DECISIONS.map((d) => (
                          <button
                            key={d}
                            onClick={() => setDecision(d)}
                            className={`focusable ${styles.decisionBtn} ${decision === d ? styles.decisionBtnSelected : styles.decisionBtnUnselected}`}
                          >
                            {d}
                          </button>
                        ))}
                      </div>
                      <label className={styles.acuityLabel}>
                        <span className={styles.acuityLabelText}>Final acuity (your clinical judgement — this is the ground-truth label)</span>
                        <select
                          value={finalAcuity || toFullCode(detail.acuity?.code)}
                          onChange={(e) => setFinalAcuity(e.target.value)}
                          className={`focusable ${styles.acuitySelect}`}
                        >
                          {ACUITY_CODES.map((k) => (
                            <option key={k} value={k}>
                              {ACUITY[k].code} · {ACUITY[k].label}{toFullCode(detail.acuity?.code) === k ? ' (model)' : ''}
                            </option>
                          ))}
                        </select>
                      </label>
                      <textarea
                        value={note}
                        onChange={(e) => setNote(e.target.value)}
                        rows={3}
                        placeholder="Clinical note (optional) — this is written to the audit trail…"
                        className={`focusable ${styles.noteInput}`}
                      />
                      <button onClick={submit} disabled={saving} className={`btn-primary ${styles.mt4}`}>
                        {saving ? 'Recording…' : 'Submit final decision'}
                      </button>
                      {submitError && (
                        <p className={styles.submitError} role="alert">{submitError}</p>
                      )}
                      {offline && (
                        <p className={styles.offlineDecisionNote}>
                          Demo mode — the backend is offline, so this decision is kept in the browser only and is NOT written to the audit trail.
                        </p>
                      )}
                    </div>
                  )}
                </div>
              </motion.div>
            ) : (
              <div className={`card ${styles.emptyState}`}>Select a case to review.</div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  )
}

function Section({ label, children }) {
  return (
    <div className={styles.block}>
      <div className="eyebrow">{label}</div>
      <p className={styles.sectionText}>{children}</p>
    </div>
  )
}
