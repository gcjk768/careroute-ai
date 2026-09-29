import { acuityOf } from '@/lib/acuity'
import styles from './AcuityBadge.module.css'

// A compact acuity chip (P1..P5). Colour encodes clinical severity consistently
// everywhere it appears — a small but important [Responsible-AI] legibility cue.
// `size` = 'sm' | 'md' | 'lg'.
export default function AcuityBadge({ code, size = 'md', showLabel = true }) {
  const a = acuityOf(code)
  const sizeClass = size === 'lg' ? styles.lg : size === 'sm' ? styles.sm : styles.md
  return (
    <span
      className={`${styles.badge} ${sizeClass}`}
      style={{ color: a.color, background: a.tint, boxShadow: `inset 0 0 0 1px ${a.color}33` }}
    >
      <span className={styles.dot} style={{ background: a.color }} />
      {a.code}
      {showLabel && <span className={styles.label}>· {a.label}</span>}
    </span>
  )
}
