// Which LLM served each step of a case, client side. Pure helpers, no React, so they can be
// unit-tested with `node --test` (see tests/models.test.mjs).
//
// The backend's LLM router (backend/app/llm.py ROUTES) sends every call to a TIER by how heavy
// the task is — fast (normalise, route, summarise, word a question), deep (classify, red-flag
// check, critique), max (a call escalated as hard) — and each tier maps to a model. The final
// event's `llm` list records every call made for this turn: task, tier, reason, model, ms, cached.

const TASKS = {
  'intake.normalise': ['Symptom-Intake', 'normalise the complaint'],
  'classifier.classify': ['Severity-Classifier', 'assess acuity'],
  'classifier.question': ['Severity-Classifier', 'word the interview question'],
  'safety.semantic': ['Safety-Override', 'semantic red-flag check'],
  'routing.select': ['Care-Routing', 'choose the clinic'],
  'handoff.summary': ['Clinician-Handoff', 'summarise for the clinician'],
  'reflection.critic': ['Reflection', 'critique the decision'],
}

export const TIERS = {
  fast: 'light task — cheaper, faster model',
  deep: 'heavy task — stronger model',
  max: 'escalated as hard — strongest model',
  pinned: 'model pinned by the agent',
  default: 'not routed — default model',
}

const REASONS = {
  low_confidence: 'escalated: low confidence',
  thin_evidence: 'escalated: thin evidence',
  rerun: 'escalated: re-run',
  override: 'model pinned',
  unrouted: 'not routed',
}

/** One call -> what the UI shows. */
export function describeCall(call) {
  const [agent, what] = TASKS[call.task] ?? ['Agent', call.task]
  const notes = []
  if (REASONS[call.reason]) notes.push(REASONS[call.reason])
  if (call.cached) notes.push('served from cache')
  return {
    agent,
    what,
    tier: TIERS[call.tier] ? call.tier : 'default',
    model: call.model || 'provider default',
    ms: call.cached ? null : call.ms,
    note: notes.join(' · '),
  }
}

/** The panel header: how many calls, which models, how many escalated. */
export function summarise(calls) {
  const list = Array.isArray(calls) ? calls : []
  return {
    count: list.length,
    models: [...new Set(list.map((c) => c.model).filter(Boolean))],
    escalated: list.filter((c) => c.tier === 'max').length,
  }
}

/** The call that worded this turn's interview question, if an LLM did. */
export function questionCall(calls) {
  const list = Array.isArray(calls) ? calls : []
  return list.filter((c) => c.task === 'classifier.question').at(-1) ?? null
}
