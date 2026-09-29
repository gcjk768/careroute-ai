'use client'

// Shared brand bits: the ECG wordmark glyph and the live backend-status dot.
// Used by both the patient layout and the staff portal layout.
import { useEffect, useState } from 'react'
import { getHealth } from '@/lib/api'
import styles from './Brand.module.css'

export function EcgMark() {
  return (
    <svg width="46" height="22" viewBox="0 0 46 22" fill="none" aria-hidden>
      <path
        className="ecg-path"
        d="M1 11h9l3-8 4 16 4-12 3 4h4l3-3 3 3h8"
        stroke="var(--coral)"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function StatusDot() {
  const [health, setHealth] = useState(null)
  useEffect(() => {
    let alive = true
    getHealth()
      .then((h) => alive && setHealth(h))
      .catch(() => alive && setHealth({ status: 'offline' }))
    return () => {
      alive = false
    }
  }, [])
  const online = health && health.status === 'ok'
  const live = health?.llm
  const label = !health ? 'connecting' : !online ? 'demo mode' : live ? `live · ${health.model}` : 'live · rules'
  const color = !health ? '#8AA893' : !online ? '#E8A13C' : '#3E7A5E'
  return (
    <div className={styles.status}>
      <span className={styles.dotWrap}>
        {online && <span className={styles.ping} style={{ background: color }} />}
        <span className={styles.dot} style={{ background: color }} />
      </span>
      {label}
    </div>
  )
}
