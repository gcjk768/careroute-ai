// API client for the CareRoute backend (FastAPI on :8000, proxied at /api in dev).
// The triage endpoint streams Server-Sent Events; because it is a POST we read the
// body stream manually rather than using EventSource.
//
// If the backend is UNREACHABLE (no HTTP response at all, and nothing of the run
// delivered yet) we fall back to a faithful in-browser SIMULATION so the console
// is always demonstrable. A refusal (4xx/5xx) or a stream that breaks part-way is
// NOT that case: the service answered, so its answer is surfaced as an error
// rather than papered over with a fabricated clinical recommendation.

import { ACUITY_PHRASE } from './acuity'

const BASE = '/api'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// P1/P2 — the tiers where the patient must act now and must NOT be told to wait
// on a clinician. Matches `_is_emergency` in the backend orchestrator.
const CRITICAL_ACUITY = new Set(['P1_RESUSCITATION', 'P2_EMERGENT'])

async function j(path, opts) {
  // Browser-side, same-origin fetch: BASE is the constant '/api' and `path` is
  // built by this module's own helpers, so there is no server-side request to forge.
  // nosemgrep: nodejs_scan.javascript-ssrf-rule-node_ssrf
  const res = await fetch(BASE + path, opts)
  if (!res.ok) {
    // Carry the status so callers can tell "already decided" (404) from
    // "service broken" (5xx) instead of swallowing every failure alike.
    const err = new Error(`${path} → ${res.status}`)
    err.status = res.status
    throw err
  }
  return res.json()
}

export const getHealth = () => j('/health')
export const getEscalations = () => j('/escalations')
export const getEscalation = (id) => j(`/escalations/${encodeURIComponent(id)}`)
export const postDecision = (id, body) =>
  j(`/escalations/${encodeURIComponent(id)}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
export const getFairness = () => j('/fairness')

// [Responsible-AI] PDPC Stakeholder-Interaction pillar: let a patient rate or
// challenge a decision. Best-effort — never blocks the UI on failure.
export const submitFeedback = (caseId, body) =>
  j(`/cases/${encodeURIComponent(caseId)}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

/**
 * Turn a non-2xx triage response into a message a patient can act on.
 * The backend's own `detail` is preferred for 4xx (it is written for humans:
 * see main.py's HTTPException details); 5xx bodies are not shown, since they
 * are internal and may leak implementation detail.
 */
async function httpErrorMessage(res) {
  if (res.status === 429) return 'Too many requests — please wait a moment and try again'
  if (res.status >= 500) return 'The triage service returned an error'
  try {
    const data = await res.json()
    const detail = data?.detail
    if (typeof detail === 'string' && detail.trim()) return detail.trim()
    // FastAPI validation errors (422) arrive as a list of {loc, msg, type}.
    if (Array.isArray(detail)) {
      const messages = detail.map((d) => (typeof d === 'string' ? d : d?.msg)).filter(Boolean)
      if (messages.length) return messages.join('; ')
    }
  } catch {
    /* not JSON — fall through to the generic message below */
  }
  return `The triage service could not accept this request (HTTP ${res.status}).`
}

/**
 * Stream a triage run. Calls onEvent(evt) for every SSE payload.
 *
 * Returns one of:
 *   { simulated: false }                    — the stream completed
 *   { simulated: false, aborted: true }     — the caller aborted it
 *   { simulated: true }                     — the backend was UNREACHABLE and a
 *                                             client-side simulation was run
 *   { simulated: false, error: {status, message} }
 *                                           — the request was refused or the
 *                                             stream broke; the caller must show
 *                                             the error, not a result
 *
 * The simulation is reachable ONLY from a genuine network failure (fetch itself
 * rejecting, i.e. no HTTP response) with zero frames already delivered. An HTTP
 * status of any kind means the service answered and its answer must be surfaced;
 * a stream that breaks after real events must keep those events, because
 * replaying a whole fabricated run on top of a partial real one produces
 * contradictory clinical output under a "backend offline" caption.
 */
export async function streamTriage(input, onEvent, signal) {
  let delivered = 0
  const deliver = (evt) => {
    delivered += 1
    onEvent(evt)
  }

  let res
  try {
    res = await fetch(BASE + '/triage/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(input),
      signal,
    })
  } catch {
    // fetch only rejects for an abort or a genuine transport failure (TypeError).
    if (signal?.aborted) return { simulated: false, aborted: true }
    await simulateTriage(input, onEvent, signal)
    return { simulated: true }
  }

  if (!res.ok) {
    return { simulated: false, error: { status: res.status, message: await httpErrorMessage(res) } }
  }
  if (!res.body) {
    return {
      simulated: false,
      error: { status: res.status, message: 'The triage service returned an empty response.' },
    }
  }

  try {
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? ''
      for (const frame of frames) {
        const line = frame.split('\n').find((l) => l.startsWith('data:'))
        if (!line) continue
        try {
          deliver(JSON.parse(line.slice(5).trim()))
        } catch {
          /* ignore malformed keepalive frames */
        }
      }
    }
  } catch {
    if (signal?.aborted) return { simulated: false, aborted: true }
    if (delivered > 0) {
      return {
        simulated: false,
        error: {
          status: null,
          interrupted: true,
          message:
            'The connection to the triage service was interrupted before the assessment finished. What you see below is the partial result — please run the triage again.',
        },
      }
    }
    // Nothing was ever delivered, so there is no real output to contradict.
    await simulateTriage(input, onEvent, signal)
    return { simulated: true }
  }
  return { simulated: false }
}

// ── Client-side simulation (used when the backend is offline) ────────────────
const RULES = [
  { kw: ['chest pain', 'crushing', 'cardiac', 'heart attack'], acuity: 'P1_RESUSCITATION', rule: 'CARDIAC_CHEST_PAIN' },
  { kw: ['can\'t breathe', 'cannot breathe', 'breathless', 'shortness of breath', 'choking'], acuity: 'P1_RESUSCITATION', rule: 'RESPIRATORY_DISTRESS' },
  { kw: ['stroke', 'face droop', 'slurred', 'weakness one side', 'numb arm'], acuity: 'P1_RESUSCITATION', rule: 'STROKE_FAST' },
  { kw: ['suicidal', 'end my life', 'self harm'], acuity: 'P2_EMERGENT', rule: 'SELF_HARM_RISK' },
  { kw: ['severe bleeding', 'won\'t stop bleeding', 'anaphylaxis', 'swelling throat'], acuity: 'P1_RESUSCITATION', rule: 'HAEMORRHAGE_ANAPHYLAXIS' },
  { kw: ['high fever', 'persistent', 'vomiting', 'dehydrated', 'severe pain'], acuity: 'P3_URGENT', rule: null },
  { kw: ['rash', 'mild', 'sore throat', 'cough', 'cold'], acuity: 'P4_NON_URGENT', rule: null },
]

const CARE_TIER = {
  P1_RESUSCITATION: 'Emergency Department (call 995)',
  P2_EMERGENT: 'Emergency Department',
  P3_URGENT: 'Urgent Care Clinic',
  P4_NON_URGENT: 'GP / Polyclinic',
  P5_SELF_CARE: 'Self-care & monitoring',
}

// Aborting a simulated run is a normal outcome, not a failure. It is signalled
// with a private sentinel so it can never escape as an exception into
// streamTriage (where it would have been mistaken for a transport failure).
const SIM_ABORTED = Symbol('simulation aborted')

async function simulateTriage(input, onEvent, signal) {
  try {
    await runSimulation(input, onEvent, signal)
  } catch (err) {
    if (err !== SIM_ABORTED) throw err
  }
}

async function runSimulation(input, onEvent, signal) {
  const stopped = () => signal?.aborted
  // CSPRNG, not Math.random: getRandomValues works in non-secure (plain http)
  // contexts too, unlike crypto.randomUUID.
  const caseId = 'SIM-' + crypto.getRandomValues(new Uint32Array(1))[0].toString(36).padStart(6, '0').slice(-6).toUpperCase()
  // Keyword regexes below are fixed literals with no nested quantifiers, so
  // they run in linear time on any input — not a ReDoS sink.
  // nosemgrep: nodejs_scan.javascript-dos-rule-regex_dos
  const text = (input.text || '').toLowerCase() // njsscan-ignore: regex_dos
  const match = RULES.find((r) => r.kw.some((k) => text.includes(k)))
  const baseAcuity = match ? match.acuity : text.length > 120 ? 'P3_URGENT' : 'P4_NON_URGENT'
  const redFlag = !!(match && match.rule)
  const confidence = redFlag ? 0.98 : 0.55 + Math.min(0.4, text.length / 400)

  const step = async (evt, ms = 750) => {
    if (stopped()) throw SIM_ABORTED
    onEvent(evt)
    await sleep(ms)
  }

  await step({ event: 'case_open', caseId, ts: new Date().toISOString() }, 350)
  await step({ event: 'guardrail', status: 'pass', detail: 'Input screened — no injection or policy violation.' }, 600)

  await step({ event: 'agent_active', agent: 'intake', label: 'Normalising symptoms' }, 700)
  await step({ event: 'agent_result', agent: 'intake', label: 'Symptom-Intake', summary: 'Structured intake record created.', data: { language: input.language || 'en', normalised: input.text?.slice(0, 90) } }, 500)

  await step({ event: 'agent_active', agent: 'classifier', label: 'Estimating acuity' }, 850)
  await step({ event: 'agent_result', agent: 'classifier', label: 'Severity-Classifier', summary: `Acuity ${baseAcuity.split('_')[0]} @ ${(confidence * 100) | 0}% confidence.`, data: { acuity: baseAcuity, confidence } }, 500)

  await step({ event: 'agent_active', agent: 'safety', label: 'Evaluating red-flag rules' }, 800)
  await step({
    event: 'safety_override',
    triggered: redFlag,
    rule: match?.rule ?? null,
    priorAcuity: baseAcuity,
    forcedAcuity: redFlag ? match.acuity : null,
  }, 500)
  await step({ event: 'agent_result', agent: 'safety', label: 'Safety-Override', summary: redFlag ? `Red-flag ${match.rule} — acuity forced, non-overridable.` : 'No red-flag rule matched.', data: {} }, 450)

  const finalAcuity = redFlag ? match.acuity : baseAcuity
  const escalated = redFlag || confidence < 0.6

  await step({ event: 'agent_active', agent: 'routing', label: 'Mapping care tier + clinic lookup' }, 800)
  await step({ event: 'agent_result', agent: 'routing', label: 'Care-Routing', summary: CARE_TIER[finalAcuity], data: { careTier: CARE_TIER[finalAcuity] } }, 450)

  if (escalated) {
    await step({ event: 'agent_active', agent: 'hitl', label: 'Routing to clinician' }, 700)
    await step({ event: 'agent_result', agent: 'hitl', label: 'Human-in-the-Loop', summary: 'Escalation created — awaiting clinician decision.', data: {} }, 450)
    // Mirrors the backend, which gates Clinician-Handoff on `state.escalated`:
    // without this the handoff node would sit idle on an escalated simulated
    // run and contradict what the real pipeline does.
    await step({ event: 'agent_active', agent: 'handoff', label: 'Preparing clinician packet' }, 700)
    await step({ event: 'agent_result', agent: 'handoff', label: 'Clinician-Handoff', summary: 'Handoff packet prepared for the reviewing clinician.', data: {} }, 450)
  }

  // SHAP-surrogate explanation — signed feature contributions (positive = pushes
  // toward a more urgent acuity). Mirrors the backend's classifier.explain().
  const explanation = redFlag
    ? [
        { feature: match.rule.replace(/_/g, ' '), weight: 0.95 },
        { feature: 'red-flag keyword match', weight: 0.8 },
        { feature: 'symptom acuity signals', weight: 0.4 },
      ]
    : [
        { feature: text.length > 120 ? 'detailed symptom description' : 'brief description', weight: text.length > 120 ? 0.35 : -0.2 },
        { feature: /pain|fever|vomit|persistent/.test(text) ? 'urgency keywords present' : 'no urgency keywords', weight: /pain|fever|vomit|persistent/.test(text) ? 0.5 : -0.4 },
        { feature: /mild|minor|slight/.test(text) ? 'mild-severity language' : 'neutral severity language', weight: /mild|minor|slight/.test(text) ? -0.55 : 0.1 },
      ]

  if (stopped()) throw SIM_ABORTED
  onEvent({
    event: 'final',
    caseId,
    acuity: { code: finalAcuity, label: finalAcuity, score: 0 },
    careTier: CARE_TIER[finalAcuity],
    confidence,
    escalated,
    explanation,
    // These bars are invented by the simulation, and the UI says so.
    explanationSource: 'simulation',
    // Plain English, matching PipelineOrchestrator.build_rationale — a patient
    // should not be able to tell which path produced the sentence.
    rationale: [
      `You told us: ${(input.text || '').trim().replace(/\.+$/, '')}.`,
      `We assessed this as ${ACUITY_PHRASE[finalAcuity] || finalAcuity}.`,
      redFlag
        ? 'A safety rule flagged it, and that rule cannot be overruled by the model.'
        : `The classifier was ${(confidence * 100) | 0}% confident.`,
      `Where to go: ${CARE_TIER[finalAcuity]}.`,
      escalated
        ? (CRITICAL_ACUITY.has(finalAcuity)
            ? 'This case also goes to a clinician. Do not wait to hear back — get help now.'
            : 'This case also goes to a clinician. They may update this advice. If anything gets worse in the meantime, treat it as an emergency.')
        : null,
    ].filter(Boolean).join(' '),
    citations: [
      { title: 'Emergency Severity Index (ESI) v4', snippet: 'Five-level triage acuity guidance for symptom-based prioritisation.', source: 'AHRQ' },
      { title: 'Local red-flag protocol', snippet: 'Chest pain, breathlessness and stroke signs escalate deterministically.', source: 'CareRoute clinical policy' },
    ],
    waitTimeMin: finalAcuity === 'P1_RESUSCITATION' ? 0 : 20 + ((text.length * 7) % 90),
    clinic: finalAcuity === 'P1_RESUSCITATION' ? 'Nearest ED' : 'Tampines Polyclinic',
  })
}
