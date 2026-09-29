'use client'

// Patient (end-user) app shell — the whole main experience.
// Deliberately clean and focused: a slim brand bar, the triage content, and a
// footer with the safety disclaimer plus a discreet Staff-portal link. No
// technical/agent navigation is shown to patients.
import Link from 'next/link'
import { motion } from 'framer-motion'
import { EcgMark } from './Brand'
import styles from './PatientLayout.module.css'

export default function PatientLayout({ children }) {
  return (
    <div className={styles.shell}>
      {/* Always-visible emergency banner — the single most important safety
          affordance for a public triage tool. */}
      <div className={styles.emergency} role="alert">
        <div className={styles.emergencyInner}>
          <span aria-hidden>⚠</span>
          <span>
            Life-threatening emergency? Don’t wait — call{' '}
            <a href="tel:995" className={styles.tel}>995</a> (Singapore) now.
          </span>
        </div>
      </div>

      <header className={styles.header}>
        <div className={styles.headerInner}>
          <Link href="/" className={styles.brand}>
            <EcgMark />
            <div className={styles.brandText}>
              <div className={styles.brandName}>CareRoute</div>
              <div className={styles.brandTag}>AI · Symptom Triage</div>
            </div>
          </Link>
          <span className={styles.headerNote}>AI assists · a clinician decides</span>
        </div>
      </header>

      <motion.main
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
        className={styles.main}
      >
        {children}
      </motion.main>

      <footer className={styles.footer}>
        <div className={styles.footerInner}>
          <p className={styles.disclaimer}>
            Not for emergencies — if this is life-threatening, call your local emergency number now.
            <br />
            Safety-gated · bias-audited · your final care decision rests with a clinician.
          </p>
          <Link href="/staff/login" className={`focusable ${styles.staffLink}`}>
            Staff login →
          </Link>
        </div>
      </footer>
    </div>
  )
}
