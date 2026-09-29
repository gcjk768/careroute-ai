'use client'
// Pipeline page — the [Agentic] orchestration surface. Streams a triage run over
// Server-Sent Events and shows the orchestrator activating each worker in real time,
// alongside a terminal-style event log. This is the clearest demo of agent
// autonomy + orchestration (Architecting Agentic AI course).
import { useEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import { useTriage } from '@/lib/useTriage'
import PipelineVisualizer from '@/components/PipelineVisualizer'
import PlanList from '@/components/PlanList'
import AcuityBadge from '@/components/AcuityBadge'
import PortalExplainer from '@/components/PortalExplainer'
import styles from './PipelinePage.module.css'

const CASES = [
  { label: 'Cardiac chest pain', text: 'Sudden crushing chest pain radiating to the left arm, sweating and breathless.' },
  { label: 'Stroke (FAST)', text: 'Face drooping on one side, slurred speech, sudden weakness in one arm.' },
  { label: 'Low-confidence', text: 'Feeling a bit off, some tiredness and a vague ache, hard to describe exactly.' },
  { label: 'Self-care', text: 'Minor runny nose and a mild cough since yesterday, otherwise feeling fine.' },
]

const EVENT_META = {
  case_open: { c: '#3E7A5E', t: 'CASE' },
  guardrail: { c: '#1F4C3A', t: 'GUARD' },
  agent_active: { c: '#1F4C3A', t: 'ACTIVATE' },
  agent_result: { c: '#3E7A5E', t: 'RESULT' },
  safety_override: { c: '#E5533B', t: 'OVERRIDE' },
  plan: { c: '#1F4C3A', t: 'PLAN' },
  final: { c: '#14231C', t: 'FINAL' },
  error: { c: '#C0392B', t: 'ERROR' },
}

export default function PipelinePage() {
  const { states, events, override, result, plan, running, simulated, start, reset } = useTriage()
  const logRef = useRef(null)

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' })
  }, [events])

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <div className="eyebrow">Orchestration telemetry</div>
          <h1 className={styles.title}>Agent pipeline</h1>
          <p className={styles.lead}>
            Watch <span className={styles.leadStrong}>Symptom-Intake</span> activate each worker agent over a live
            Server-Sent Events stream, with the deterministic guardrail and red-flag gates enforced at every hop.
          </p>
        </div>
        <div className={styles.headerActions}>
          {running && <span className={styles.streaming}>streaming…</span>}
          <button onClick={reset} className="btn-ghost">Clear</button>
        </div>
      </header>

      <PortalExplainer
        title="A live view of the AI agents working together"
        lede="CareRoute doesn’t use one big AI. It uses a team of six small AI “agents”, each with a single job. The first one — Symptom-Intake — also acts as the coordinator, much like a triage nurse who takes the patient’s history and then delegates to specialists. This page lets you launch a test symptom case and watch that team work through it, step by step, in real time. It’s a window into how the AI actually reaches its decision."
        looking={[
          'The graph: the coordinator at the top (Symptom-Intake) hands work to the worker agents (Intake → Severity classifier → Safety rules → Care routing → Human hand-off). Intake appears twice because it does both jobs. Each node lights up while it runs and turns green when done — or red if a red-flag safety rule fires.',
          'The event stream (right): a timestamped log of every step the agents take — the same trace an engineer would use to audit or debug a run.',
          'The outcome bar (below the graph): the final acuity (P1–P5) and recommended care tier once the run finishes.',
        ]}
        using={[
          'Click a sample case (e.g. “Cardiac chest pain”) to launch a run.',
          'Watch the agents activate in sequence and the event log fill in.',
          'Try “Cardiac chest pain” to see a red-flag override force the highest priority — the key safety mechanism the AI cannot bypass.',
          'Press “Clear” to reset and try another case.',
        ]}
      />

      {/* Case launcher */}
      <div className={styles.launcher}>
        {CASES.map((c) => (
          <button
            key={c.label}
            disabled={running}
            onClick={() => start({ text: c.text, language: 'English', isVoice: false })}
            className={`focusable ${styles.caseBtn}`}
          >
            ▶ {c.label}
          </button>
        ))}
      </div>

      <div className={styles.grid}>
        <div className={styles.colMain}>
          <PipelineVisualizer states={states} override={override} />
          <PlanList plan={plan} />
          {result && (
            <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className={`card ${styles.outcome}`}>
              <div className={styles.outcomeLeft}>
                <span className={styles.outcomeLabel}>outcome</span>
                <AcuityBadge code={result.acuity?.code} />
                {result.escalated && (
                  <span className={styles.escalated}>
                    escalated
                  </span>
                )}
              </div>
              <span className={styles.careTier}>{result.careTier}</span>
            </motion.div>
          )}
        </div>

        {/* Terminal-style event log */}
        <div className={styles.colSide}>
          <div className={`card ${styles.logCard}`}>
            <div className={styles.logHeader}>
              <span className={styles.logHeaderLabel}>event stream</span>
              <span className={styles.logHeaderCount}>{events.length} events{simulated ? ' · sim' : ''}</span>
            </div>
            <div ref={logRef} className={styles.logBody}>
              {events.length === 0 && <p className={styles.logEmpty}>Launch a case to stream events…</p>}
              {events.map((e, i) => {
                const m = EVENT_META[e.event] || { c: '#8AA893', t: e.event?.toUpperCase() }
                return (
                  <motion.div key={i} initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} className={styles.logRow}>
                    <span className={styles.logTag} style={{ color: m.c, background: `${m.c}14` }}>
                      {m.t}
                    </span>
                    <span className={styles.logText}>
                      {e.event === 'agent_active' && `orchestrator → ${e.agent}: ${e.label}`}
                      {e.event === 'agent_result' && `${e.agent}: ${e.summary}`}
                      {e.event === 'guardrail' && `${e.status} — ${e.detail}`}
                      {e.event === 'case_open' && `opened ${e.caseId}`}
                      {e.event === 'safety_override' && (e.triggered ? `RULE ${e.rule} fired → ${String(e.forcedAcuity).split('_')[0]}` : 'no red-flag matched')}
                      {e.event === 'plan' && `${e.shape}: ${e.steps.map((s) => s.step).join(' → ')}`}
                      {e.event === 'final' && `→ ${e.careTier}${e.escalated ? ' (escalated)' : ''}`}
                      {e.event === 'error' && e.message}
                    </span>
                  </motion.div>
                )
              })}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
