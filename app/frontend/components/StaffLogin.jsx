'use client'
// Staff login — the password is checked on the server (/api/staff/session),
// which sets the httpOnly session cookie the escalation proxy requires. With no
// staff key configured (local dev, CI) any password is accepted.
import { useEffect, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { motion } from 'framer-motion'
import { EcgMark } from '@/components/Brand'
import { loginStaff, getStaff } from '@/lib/auth'
import styles from './StaffLogin.module.css'

export default function StaffLogin() {
  const router = useRouter()
  const [name, setName] = useState('')
  // After mount, not during render: localStorage doesn't exist on the server.
  useEffect(() => {
    const existing = getStaff()
    if (existing?.name && existing.name !== 'Staff') setName(existing.name)
  }, [])
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await loginStaff(name, password)
      router.replace('/staff')
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <div className={styles.page}>
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
        className={`card ${styles.cardBox}`}
      >
        <div className={styles.topBand} />
        <form onSubmit={submit} className={styles.form}>
          <div className={styles.brandRow}>
            <EcgMark />
            <div className={styles.brandText}>
              <div className={styles.brandName}>CareRoute AI</div>
              <div className={styles.brandTag}>Staff Portal</div>
            </div>
          </div>

          <h1 className={styles.title}>Staff sign in</h1>
          <p className={styles.intro}>
            The internal surfaces — clinician review, governance and the agent pipeline — are staff-only.
          </p>

          <label className={`eyebrow ${styles.nameLabel}`} htmlFor="staff-name">Your name</label>
          <input
            id="staff-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Dr. A. Rahman"
            className={`focusable ${styles.input}`}
            autoFocus
          />

          <label className={`eyebrow ${styles.nameLabel}`} htmlFor="staff-password">Staff password</label>
          <input
            id="staff-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={`focusable ${styles.input}`}
          />

          {error && <p role="alert" className={styles.intro}>{error}</p>}

          <button type="submit" disabled={busy} className={`btn-primary ${styles.submit}`}>
            {busy ? 'Signing in…' : 'Enter staff portal'}
          </button>

          <div className={styles.footerRow}>
            <Link href="/" className={styles.backLink}>← Patient app</Link>
            <span className={styles.demoNote}>staff only</span>
          </div>
        </form>
      </motion.div>
    </div>
  )
}
