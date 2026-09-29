'use client'

// A plain-language "what is this page?" panel for the staff portal. Each staff
// surface is fairly technical; this explains, in non-jargon terms, what the page
// is, what the reader is looking at, and how to use it. Collapsible (open by
// default) so returning staff can tuck it away.
import { useState } from 'react'
import styles from './PortalExplainer.module.css'

export default function PortalExplainer({ title, lede, looking = [], using = [] }) {
  const [open, setOpen] = useState(true)
  return (
    <section className={styles.card}>
      <button
        type="button"
        className={`focusable ${styles.head}`}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className={styles.badge}>ℹ︎ What is this page?</span>
        <span className={styles.headTitle}>{title}</span>
        <span className={styles.toggle}>{open ? 'Hide' : 'Show'}</span>
      </button>

      {open && (
        <div className={styles.body}>
          <p className={styles.lede}>{lede}</p>
          <div className={styles.cols}>
            {looking.length > 0 && (
              <div className={styles.col}>
                <div className={styles.colHead}>What you’re looking at</div>
                <ul className={styles.list}>
                  {looking.map((t, i) => (
                    <li key={i} className={styles.item}>
                      <span className={styles.bullet} aria-hidden />
                      <span>{t}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {using.length > 0 && (
              <div className={styles.col}>
                <div className={styles.colHead}>How to use it</div>
                <ol className={styles.list}>
                  {using.map((t, i) => (
                    <li key={i} className={styles.item}>
                      <span className={styles.num}>{i + 1}</span>
                      <span>{t}</span>
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
