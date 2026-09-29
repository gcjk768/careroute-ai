'use client'

// PipelineVisualizer — [Agentic] the orchestrator→workers graph. Reusable across
// the Triage and Pipeline pages. Nodes change status (idle → active → done, or
// flagged for a red-flag override) as SSE events arrive, making the otherwise
// invisible agent orchestration legible to a non-technical audience.
import { motion, AnimatePresence } from 'framer-motion'
import { AGENTS, ORCHESTRATOR } from '@/lib/acuity'
import styles from './PipelineVisualizer.module.css'

// Renders the orchestrator → 6-worker graph. `states` is a map keyed by agent:
//   { status: 'idle' | 'active' | 'done' | 'flagged', summary?: string }
// `override` is the safety-override result (or null).
const WORKERS = AGENTS  // AGENTS is already just the workers, not the orchestrator

const STYLES = {
  idle: { ring: 'rgba(20,35,28,0.14)', fg: '#8AA893', bg: '#FBF8F1', glow: 'none' },
  active: { ring: '#1F4C3A', fg: '#1F4C3A', bg: '#FFFFFF', glow: '0 0 0 6px rgba(31,76,58,0.10)' },
  done: { ring: '#3E7A5E', fg: '#1F4C3A', bg: '#F0F5EE', glow: 'none' },
  flagged: { ring: '#E5533B', fg: '#C0392B', bg: 'rgba(229,83,59,0.08)', glow: '0 0 0 6px rgba(229,83,59,0.12)' },
}

function Node({ agent, state }) {
  const s = STYLES[state?.status || 'idle']
  const active = state?.status === 'active'
  return (
    <div className={styles.node}>
      <motion.div
        animate={{ boxShadow: s.glow, scale: active ? 1.06 : 1 }}
        transition={{ duration: 0.4 }}
        className={styles.nodeBox}
        style={{ borderColor: s.ring, background: s.bg, color: s.fg }}
      >
        <span className={styles.glyph}>{agent.glyph}</span>
        {active && (
          <motion.span
            className={styles.nodePulse}
            style={{ borderColor: s.ring }}
            animate={{ opacity: [0.6, 0, 0.6], scale: [1, 1.35, 1] }}
            transition={{ duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
          />
        )}
        {state?.status === 'done' && (
          <motion.span initial={{ scale: 0 }} animate={{ scale: 1 }} className={`${styles.nodeBadge} ${styles.nodeBadgeDone}`}>
            ✓
          </motion.span>
        )}
        {state?.status === 'flagged' && (
          <motion.span initial={{ scale: 0 }} animate={{ scale: 1 }} className={`${styles.nodeBadge} ${styles.nodeBadgeFlagged}`}>
            !
          </motion.span>
        )}
      </motion.div>
      <div className={styles.nodeMeta}>
        <div className={styles.nodeName}>{agent.name}</div>
        <div className={styles.nodeRole}>{agent.role}</div>
      </div>
    </div>
  )
}

export default function PipelineVisualizer({ states = {}, override }) {
  const sup = states.orchestrator || { status: 'idle' }
  const anyActive = Object.values(states).some((s) => s?.status === 'active')

  return (
    <div className={`card ${styles.wrap}`}>
      <div className={styles.head}>
        <div>
          <div className="eyebrow">Orchestration</div>
          <h3 className={styles.title}>Symptom-Intake → worker agents</h3>
        </div>
        <div className={styles.headStatus}>
          {anyActive ? 'running' : sup.status === 'done' ? 'complete' : 'idle'}
        </div>
      </div>

      {/* The orchestrator. Symptom-Intake wears both hats, so it appears here as
          the conductor AND below as worker step 1 — two indicators, one agent. */}
      <div className={styles.orchestratorRow}>
        <Node agent={ORCHESTRATOR} state={sup} />
      </div>

      {/* Connector bus — a fan-out that "energises" when the orchestrator is working.
          X-positions are derived from the worker count so the bus stays correct as
          agents are added/removed. */}
      <div className={styles.bus}>
        <svg viewBox="0 0 600 40" preserveAspectRatio="none" className={styles.busSvg} aria-hidden>
          {WORKERS.map((worker, i) => {
            const x = ((i + 0.5) / WORKERS.length) * 600
            const lit = states[worker.key] && states[worker.key].status !== 'idle'
            return (
              <motion.path
                key={worker.key}
                d={`M300 2 C300 22 ${x} 10 ${x} 38`}
                fill="none"
                stroke={lit ? '#1F4C3A' : 'rgba(20,35,28,0.16)'}
                strokeWidth="1.5"
                initial={false}
                animate={{ opacity: lit ? 1 : 0.5 }}
              />
            )
          })}
        </svg>
      </div>

      {/* Workers */}
      <div className={styles.workers} style={{ '--worker-count': WORKERS.length }}>
        {WORKERS.map((agent) => (
          <Node key={agent.key} agent={agent} state={states[agent.key]} />
        ))}
      </div>

      {/* Live override banner */}
      <AnimatePresence>
        {override?.triggered && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className={styles.override}
          >
            <span className={styles.overrideGlyph}>⬣</span>
            {/* A red-flag rule can RAISE acuity or simply agree with the
                classifier. When it agrees, prior === forced, and the old copy
                read "acuity forced from P1 to P1" — which says nothing and
                looks broken. Same event, two different things to say. */}
            {String(override.priorAcuity) === String(override.forcedAcuity) ? (
              <p className={styles.overrideText}>
                <span className={styles.overrideRule}>Red-flag override · {override.rule}</span> — independently
                confirmed{' '}
                <span className={`${styles.mono} ${styles.monoStrong}`}>{String(override.forcedAcuity).split('_')[0]}</span>. The rule
                reached the same acuity as the statistical classifier, and holds it there un-overridably.
              </p>
            ) : (
              <p className={styles.overrideText}>
                <span className={styles.overrideRule}>Red-flag override · {override.rule}</span> — acuity forced from{' '}
                <span className={styles.mono}>{String(override.priorAcuity).split('_')[0]}</span> to{' '}
                <span className={`${styles.mono} ${styles.monoStrong}`}>{String(override.forcedAcuity).split('_')[0]}</span>, independent of and
                un-overridable by the statistical classifier.
              </p>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
