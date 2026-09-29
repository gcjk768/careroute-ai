'use client'

// Staff portal shell — the internal surfaces for non-patient actors.
// Left-rail nav across the three staff surfaces, the signed-in role, a logout,
// and a link back to the patient app. Rendered only inside the gated (portal)
// route group, which passes the active page in as `children`.
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import { motion } from 'framer-motion'
import { EcgMark, StatusDot } from './Brand'
import { getStaff, logoutStaff } from '@/lib/auth'
import styles from './StaffLayout.module.css'

const NAV = [
  { to: '/staff/pipeline', label: 'Pipeline', hint: 'Watch the AI agents work', actor: 'Platform Engineer' },
  { to: '/staff/clinician', label: 'Clinician', hint: 'Human review of AI cases', actor: 'Clinician' },
  { to: '/staff/governance', label: 'Governance', hint: 'Fairness & safety checks', actor: 'Governance Officer' },
]

export default function StaffLayout({ children }) {
  const pathname = usePathname()
  const router = useRouter()
  const staff = getStaff()

  const logout = () => {
    logoutStaff()
    router.replace('/staff/login')
  }

  return (
    <div className={styles.shell}>
      {/* Left rail */}
      <aside className={styles.rail}>
        <div>
          <div className={styles.brand}>
            <EcgMark />
            <div className={styles.brandText}>
              <div className={styles.brandName}>CareRoute</div>
              <div className={styles.brandTag}>Staff Portal</div>
            </div>
          </div>

          <nav className={styles.nav}>
            {NAV.map((n) => {
              const isActive = pathname === n.to
              return (
                <Link key={n.to} href={n.to} className={`focusable ${styles.navItem}`}>
                  {isActive && (
                    <motion.span
                      layoutId="staff-nav-pill"
                      className={styles.navPill}
                      transition={{ type: 'spring', stiffness: 400, damping: 34 }}
                    />
                  )}
                  <span className={`${styles.navLabel} ${isActive ? styles.navLabelActive : ''}`}>{n.label}</span>
                  <span className={`${styles.navHint} ${isActive ? styles.navHintActive : ''}`}>{n.hint}</span>
                </Link>
              )
            })}
          </nav>
        </div>

        <div className={styles.railFoot}>
          <StatusDot />
          {staff && (
            <div className={styles.signedIn}>
              <div className={styles.signedInLabel}>signed in</div>
              <div className={styles.signedInName}>{staff.name}</div>
              <div className={styles.signedInRole}>Staff</div>
              <button onClick={logout} className={`focusable ${styles.logoutRail}`}>Log out</button>
            </div>
          )}
          <Link href="/" className={styles.patientLink}>← Patient app</Link>
        </div>
      </aside>

      {/* Main column */}
      <div className={styles.column}>
        {/* Mobile bar */}
        <div className={styles.mobileBar}>
          <div className={styles.mobileBrand}>
            <EcgMark />
            <span className={styles.mobileTitle}>Staff Portal</span>
          </div>
          <button onClick={logout} className={styles.logoutMobile}>Log out</button>
        </div>
        <nav className={styles.mobileNav}>
          {NAV.map((n) => {
            const isActive = pathname === n.to
            return (
              <Link
                key={n.to}
                href={n.to}
                className={`${styles.mobileNavItem} ${isActive ? styles.mobileNavItemActive : ''}`}
              >
                {n.label}
              </Link>
            )
          })}
        </nav>

        <motion.main
          key={pathname}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
          className={styles.main}
        >
          {children}
        </motion.main>
      </div>
    </div>
  )
}
