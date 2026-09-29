// Acuity metadata — the P1..P5 emergency severity scale used across every surface.
// Colours run red (resuscitation) → pine (self-care), matching the class-diagram enum.
export const ACUITY = {
  P1_RESUSCITATION: { code: 'P1', label: 'Resuscitation', score: 1, color: '#C0392B', tint: 'rgba(192,57,43,0.12)' },
  P2_EMERGENT: { code: 'P2', label: 'Emergent', score: 2, color: '#E5533B', tint: 'rgba(229,83,59,0.12)' },
  P3_URGENT: { code: 'P3', label: 'Urgent', score: 3, color: '#E8A13C', tint: 'rgba(232,161,60,0.14)' },
  P4_NON_URGENT: { code: 'P4', label: 'Non-urgent', score: 4, color: '#6C9A6E', tint: 'rgba(108,154,110,0.14)' },
  P5_SELF_CARE: { code: 'P5', label: 'Self-care', score: 5, color: '#1F4C3A', tint: 'rgba(31,76,58,0.12)' },
}

export function acuityOf(codeOrObj) {
  if (!codeOrObj) return ACUITY.P4_NON_URGENT
  const code = typeof codeOrObj === 'string' ? codeOrObj : codeOrObj.code || codeOrObj.acuity
  // accept either the enum key (P1_RESUSCITATION) or short code (P1)
  if (ACUITY[code]) return ACUITY[code]
  const byShort = Object.values(ACUITY).find((a) => a.code === code)
  return byShort || ACUITY.P4_NON_URGENT
}

// The conductor shown above the fan-out. There is no separate Supervisor agent
// any more — Symptom-Intake holds the orchestrator role AND runs as step 1. It
// keeps its own UI key so the conductor's lifecycle (lit from case_open through
// final) stays independent of intake's row entry, which lights only while intake
// is actually normalising. Same agent, two jobs, two indicators.
export const ORCHESTRATOR = {
  key: 'orchestrator',
  name: 'Symptom-Intake',
  role: 'Orchestrator',
  glyph: '◆',
}

// The worker agents — display metadata for the pipeline visualizer. Keep in
// step with AGENT_LABELS in backend/app/agents/base.py; an agent missing here
// still runs, it just never appears on screen.
export const AGENTS = [
  { key: 'intake', name: 'Symptom-Intake', role: 'Normalisation', glyph: '❯' },
  { key: 'classifier', name: 'Severity-Classifier', role: 'Acuity + confidence', glyph: '▲' },
  { key: 'safety', name: 'Safety-Override', role: 'Red-flag rules', glyph: '⬣' },
  { key: 'routing', name: 'Care-Routing', role: 'Care tier + clinic', glyph: '◧' },
  { key: 'hitl', name: 'Human-in-the-Loop', role: 'Escalation gate', glyph: '✚' },
  { key: 'reflection', name: 'Reflection', role: 'Critic / self-check', glyph: '↺' },
  // Runs only on escalated cases (the backend gates it on `state.escalated`),
  // so on a routine run this node correctly stays idle — that idle node IS the
  // signal that no clinician packet was needed.
  { key: 'handoff', name: 'Clinician-Handoff', role: 'Clinician packet', glyph: '⇥' },
]

// Plain-English gloss per acuity code, for prose aimed at a patient. Mirrors
// _ACUITY_PHRASE in backend/app/agents/orchestration.py so the offline
// simulation and the real backend describe a tier the same way.
export const ACUITY_PHRASE = {
  P1_RESUSCITATION: 'a life-threatening emergency (P1)',
  P2_EMERGENT: 'an emergency (P2)',
  P3_URGENT: 'urgent, but not an emergency (P3)',
  P4_NON_URGENT: 'not urgent (P4)',
  P5_SELF_CARE: 'something you can look after at home (P5)',
}

// Plain-language "what to do now" guidance per acuity — so a member of the public
// gets an actionable next step, not just a care-tier label. (SG emergency = 995.)
export const ACUITY_ADVICE = {
  P1_RESUSCITATION: {
    tone: 'critical',
    headline: 'Seek emergency help immediately',
    steps: ['Call 995 now, or go to the nearest A&E.', 'Do not drive yourself if you feel unwell.', 'Stay with someone until help arrives.'],
  },
  P2_EMERGENT: {
    tone: 'critical',
    headline: 'Get emergency care now',
    steps: ['Go to the nearest A&E, or call 995 if you cannot get there safely.', 'Bring a list of your symptoms and medications.'],
  },
  P3_URGENT: {
    tone: 'urgent',
    headline: 'See a doctor today',
    steps: ['Visit an urgent-care clinic or polyclinic today.', 'If symptoms worsen quickly, treat it as an emergency and call 995.'],
  },
  P4_NON_URGENT: {
    tone: 'routine',
    headline: 'Book a routine appointment',
    steps: ['See a GP or polyclinic within the next few days.', 'Rest, stay hydrated, and monitor your symptoms.'],
  },
  P5_SELF_CARE: {
    tone: 'routine',
    headline: 'Self-care and monitor',
    steps: ['Manage at home with rest and over-the-counter remedies.', 'Seek care if symptoms persist beyond a few days or get worse.'],
  },
}

export function adviceOf(codeOrObj) {
  const a = acuityOf(codeOrObj)
  const key = Object.keys(ACUITY).find((k) => ACUITY[k].code === a.code)
  return ACUITY_ADVICE[key] || ACUITY_ADVICE.P4_NON_URGENT
}
