'use client'
// Governance dashboard — the [Responsible-AI][MLOps] oversight surface.
// Renders the stratified fairness audit (per-subgroup accuracy, before/after
// fairness gap), safety-critical red-flag recall, and drift monitors — the
// evidence a governance officer signs off before a model is promoted.
import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { BarChart, Bar, XAxis, YAxis, ResponsiveContainer, Cell, Tooltip } from 'recharts'
import { getFairness } from '@/lib/api'
import PortalExplainer from '@/components/PortalExplainer'
import styles from './GovernanceDashboard.module.css'

const MOCK = {
  overallAccuracy: 0.912, redFlagRecall: 0.994, fairnessGapBefore: 0.118, fairnessGapAfter: 0.041,
  modelVersion: 'triage-clf@2.3.1', updatedAt: new Date().toISOString(),
  subgroups: [
    { name: '18–34 · F', accuracy: 0.93, n: 1840 }, { name: '18–34 · M', accuracy: 0.925, n: 1710 },
    { name: '35–64 · F', accuracy: 0.915, n: 2210 }, { name: '35–64 · M', accuracy: 0.905, n: 2090 },
    { name: '65+ · F', accuracy: 0.892, n: 1360 }, { name: '65+ · M', accuracy: 0.884, n: 1290 },
  ],
  drift: { data: 0.06, target: 0.03, concept: 0.11 },
  // Seeded stand-ins for the named XRAI metrics, so the offline page has the
  // same shape as the live /api/fairness payload (see models.FairnessResponse).
  demographicParity: { statisticalParityDifference: 0.21 },
  equalOpportunity: { equalOpportunityGap: 0.08 },
  equalizedOdds: { tprGap: 0.08, fprGap: 0.04, equalizedOddsGap: 0.08 },
  disparateImpact: { disparateImpactRatio: 0.38, fourFifthsFloor: 0.8, meetsFourFifthsRule: false },
  counterfactual: { attribute: 'sex', n: 10, sexFlipRate: 0.0, meanAcuityDelta: 0.0 },
  calibration: { method: 'isotonic', ece: 0.022, brier: 0.14 },
  globalExplanation: {
    method: 'seeded stand-in', sampleN: 300,
    features: [
      { feature: 'chest_pain', label: 'chest pain', meanAbsShap: 0.061, impurityImportance: 0.09 },
      { feature: 'breathing', label: 'breathing difficulty', meanAbsShap: 0.048, impurityImportance: 0.07 },
      { feature: 'age_65p', label: 'age 65+', meanAbsShap: 0.041, impurityImportance: 0.05 },
      { feature: 'bleeding', label: 'bleeding', meanAbsShap: 0.033, impurityImportance: 0.04 },
      { feature: 'fever', label: 'fever', meanAbsShap: 0.021, impurityImportance: 0.03 },
    ],
    partialDependence: [
      { feature: 'chest_pain', label: 'chest pain', grid: [0, 1], pUrgent: [0.12, 0.71] },
      { feature: 'breathing', label: 'breathing difficulty', grid: [0, 1], pUrgent: [0.14, 0.62] },
      { feature: 'age_65p', label: 'age 65+', grid: [0, 1], pUrgent: [0.15, 0.34] },
    ],
  },
}

function pct(x) { return `${(x * 100).toFixed(1)}%` }
function num(x, d = 3) { return typeof x === 'number' ? x.toFixed(d) : '—' }

// [MLOps] Release gates — mirrors backend/app/ml/model.py (MIN_ACCURACY,
// MIN_RED_FLAG_RECALL, MAX_FAIRNESS_GAP). /api/fairness does not serve the
// thresholds, only the measurements, so they are restated here; keep them in
// step with model.py, which the CI gate imports directly.
//
// This page used to advertise "red-flag recall target >= 99%" — a number that
// exists nowhere in the pipeline (the real gate is 0.95). It also painted the
// gated KPIs pine unconditionally, so a metric could sit below its gate and
// still render in the success colour. Both are now derived from GATE, which is
// the point of a sign-off surface: the colour has to be able to say "no".
const GATE = { accuracy: 0.75, redFlagRecall: 0.95, fairnessGap: 0.35 }
const PASS = '#1F4C3A'
const FAIL = '#E5533B'

export default function GovernanceDashboard() {
  const [data, setData] = useState(null)
  const [offline, setOffline] = useState(false)
  useEffect(() => {
    getFairness().then(setData).catch(() => { setOffline(true); setData(MOCK) })
  }, [])
  if (!data) return <div className={styles.loading}>Loading fairness audit…</div>

  const gapReduction = Math.round(((data.fairnessGapBefore - data.fairnessGapAfter) / data.fairnessGapBefore) * 100)
  const worst = Math.min(...data.subgroups.map((s) => s.accuracy))

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <div className="eyebrow">Responsible AI · oversight</div>
          <h1 className={styles.title}>Fairness audit</h1>
          <p className={styles.intro}>
            Stratified performance across demographic subgroups, red-flag recall, and drift telemetry — the evidence the
            governance officer signs off before promotion.
          </p>
        </div>
        <div className={styles.meta}>
          <div>{data.modelVersion}</div>
          <div className={styles.metaMoss}>updated {new Date(data.updatedAt).toLocaleString()}</div>
        </div>
      </header>

      <PortalExplainer
        title="Proof the AI model is accurate, fair and safe to deploy"
        lede="Before a triage model is allowed into production, a governance officer must sign off that it isn’t just accurate overall, but accurate fairly — across different ages and sexes — that it reliably catches safety-critical cases, and that it isn’t “drifting” as real-world data changes over time. This dashboard is that evidence, gathered in one place."
        looking={[
          'KPIs (top row): overall accuracy, red-flag recall (how reliably it catches safety-critical cases), the fairness gap after mitigation, and the worst-performing patient subgroup.',
          'The bar chart: accuracy broken down by demographic subgroup (age × sex), with the lowest-performing group highlighted in red.',
          'Fairness gap (before → after) and drift monitors: how much bias was removed by mitigation, and whether today’s live data still matches what the model was trained on.',
          'Named fairness metrics (bottom): demographic parity, equal opportunity, equalized odds, disparate impact (four-fifths rule), counterfactual stability and probability calibration — the standard definitions, each computed on the held-out set.',
        ]}
        using={[
          'Scan the KPIs for anything outside tolerance — a value below its gate turns red. Red-flag recall must stay ≥ 95%.',
          'Check the chart to confirm no subgroup lags badly behind the others.',
          'Confirm the fairness gap shrank after mitigation and drift is within bounds before promoting the model.',
        ]}
      />

      {offline && <p className={styles.offline}>⚠ backend offline — showing seeded audit data.</p>}

      {/* KPI row */}
      <div className={styles.kpiRow}>
        <Kpi
          i={0} label="Overall accuracy" value={pct(data.overallAccuracy)}
          foot={`held-out synthetic set · gate ≥ ${pct(GATE.accuracy)}`}
          accent={data.overallAccuracy >= GATE.accuracy ? PASS : FAIL}
        />
        <Kpi
          i={1} label="Red-flag recall" value={pct(data.redFlagRecall)}
          foot={`safety-critical · gate ≥ ${pct(GATE.redFlagRecall)}`}
          accent={data.redFlagRecall >= GATE.redFlagRecall ? PASS : FAIL}
        />
        <Kpi
          i={2} label="Fairness gap" value={pct(data.fairnessGapAfter)}
          foot={`↓ ${gapReduction}% after mitigation · gate ≤ ${pct(GATE.fairnessGap)}`}
          accent={data.fairnessGapAfter <= GATE.fairnessGap ? PASS : FAIL}
        />
        <Kpi i={3} label="Worst subgroup" value={pct(worst)} foot="lowest per-subgroup accuracy" accent={worst < 0.88 ? '#E5533B' : '#1F4C3A'} />
      </div>

      <div className={styles.mainGrid}>
        {/* Subgroup accuracy */}
        <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className={`card ${styles.chartCard}`}>
          <div className="eyebrow">Per-subgroup accuracy</div>
          <h3 className={styles.chartTitle}>Stratified performance</h3>
          <div className={styles.chartArea}>
            <ResponsiveContainer width="100%" height="100%">
              {/* Bar length encodes accuracy, so the scale MUST start at zero.
                  The old domain was [0.8, 0.95], which both exaggerated the
                  spread and — because the worst subgroup sits at 0.791, under
                  the floor — dropped that bar off the chart entirely. The red
                  "lowest-performing" Cell below was therefore never drawn: the
                  one bar this audit exists to show was the one it hid. The
                  side panel carries the spread as an explicit gap figure. */}
              <BarChart data={data.subgroups} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
                <XAxis
                  dataKey="name"
                  tick={{ fontSize: 11, fill: '#3E7A5E', fontFamily: 'IBM Plex Mono' }}
                  axisLine={{ stroke: 'rgba(20,35,28,0.12)' }}
                  tickLine={false}
                  interval={0}
                  /* age x sex labels ("18-34 · F") collide when laid flat at
                     8 subgroups; angling them keeps every tick readable. */
                  angle={-35}
                  textAnchor="end"
                  height={64}
                />
                <YAxis domain={[0, 1]} tickFormatter={(v) => `${(v * 100) | 0}%`} tick={{ fontSize: 11, fill: '#8AA893', fontFamily: 'IBM Plex Mono' }} axisLine={false} tickLine={false} />
                <Tooltip
                  cursor={{ fill: 'rgba(31,76,58,0.05)' }}
                  contentStyle={{ borderRadius: 12, border: '1px solid rgba(20,35,28,0.12)', fontFamily: 'IBM Plex Mono', fontSize: 12 }}
                  formatter={(v, _n, p) => [`${(v * 100).toFixed(1)}%  ·  n=${p.payload.n}`, 'accuracy']}
                />
                <Bar dataKey="accuracy" radius={[6, 6, 0, 0]} maxBarSize={54}>
                  {data.subgroups.map((s, i) => (
                    <Cell key={i} fill={s.accuracy === worst ? '#E5533B' : '#1F4C3A'} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className={styles.chartNote}>
            The lowest-performing subgroup is highlighted. Promotion is gated on the fairness gap staying within tolerance.
          </p>
        </motion.div>

        {/* Fairness gap before/after + drift */}
        <div className={styles.sideCol}>
          <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.05 }} className={`card ${styles.panel}`}>
            <div className="eyebrow">Fairness gap · before → after mitigation</div>
            <div className={styles.gapList}>
              <GapBar label="Before" value={data.fairnessGapBefore} max={Math.max(data.fairnessGapBefore, 0.15)} color="#E5533B" />
              <GapBar label="After" value={data.fairnessGapAfter} max={Math.max(data.fairnessGapBefore, 0.15)} color="#3E7A5E" />
            </div>
            <p className={styles.gapNote}>
              max subgroup accuracy spread · lower is fairer
            </p>
          </motion.div>

          <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }} className={`card ${styles.panel}`}>
            <div className="eyebrow">Drift monitors</div>
            <div className={styles.driftList}>
              <Drift label="Data drift" value={data.drift.data} />
              <Drift label="Target drift" value={data.drift.target} />
              <Drift label="Concept drift" value={data.drift.concept} />
            </div>
          </motion.div>
        </div>
      </div>

      {/* [Responsible-AI] Named fairness + calibration metrics. These were computed
          by the training audit all along but (until 2026-09-15) never left the
          backend — the response schema dropped them. Rendering them here is what
          makes "we measure equalized odds" a checkable claim rather than a docstring. */}
      <NamedMetrics data={data} />

      {/* [XRAI] GLOBAL explanation — what the model relies on overall, beside the
          per-case (local) SHAP bars the patient and clinician pages show. */}
      <GlobalExplanation ge={data.globalExplanation} />
    </div>
  )
}

function GlobalExplanation({ ge }) {
  if (!ge?.features?.length) return null
  const rows = ge.features.map((f) => ({ ...f, name: f.label || f.feature }))
  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2 }} className={`card ${styles.panel} ${styles.namedPanel}`}>
      <div className="eyebrow">Global explanation · what the model relies on overall</div>
      <div className={styles.globalGrid}>
        <div>
          <h3 className={styles.chartTitle}>Top features by mean |SHAP|</h3>
          <div className={styles.globalChart}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 16, left: 8, bottom: 0 }}>
                <XAxis type="number" tick={{ fontSize: 11, fill: '#8AA893', fontFamily: 'IBM Plex Mono' }} axisLine={false} tickLine={false} />
                <YAxis type="category" dataKey="name" width={150} tick={{ fontSize: 11, fill: '#3E7A5E', fontFamily: 'IBM Plex Mono' }} axisLine={false} tickLine={false} />
                <Tooltip
                  cursor={{ fill: 'rgba(31,76,58,0.05)' }}
                  contentStyle={{ borderRadius: 12, border: '1px solid rgba(20,35,28,0.12)', fontFamily: 'IBM Plex Mono', fontSize: 12 }}
                  formatter={(v, _n, p) => [`${v.toFixed(4)}  ·  impurity ${p.payload.impurityImportance.toFixed(3)}`, 'mean |SHAP|']}
                />
                <Bar dataKey="meanAbsShap" fill="#1F4C3A" radius={[0, 6, 6, 0]} maxBarSize={18} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className={styles.chartNote}>
            Averaged over {ge.sampleN} held-out cases and all five acuity classes. Hover for the forest&apos;s own impurity importance.
          </p>
        </div>
        <div>
          <h3 className={styles.chartTitle}>Partial dependence · P(urgent)</h3>
          <div className={styles.pdpList}>
            {ge.partialDependence?.map((curve) => (
              <div key={curve.feature} className={styles.pdpRow}>
                <div className={styles.pdpLabel}>{curve.label || curve.feature}</div>
                <div className={styles.pdpValues}>
                  {curve.grid.map((g, i) => (
                    <span key={i} className={styles.pdpCell}>
                      <span className={styles.pdpGrid}>{g}</span>
                      <span className={styles.pdpP}>{pct(curve.pUrgent[i])}</span>
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
          <p className={styles.chartNote}>
            Average calibrated probability of P1/P2 when the feature is forced to each value, everything else held as observed.
          </p>
        </div>
      </div>
    </motion.div>
  )
}

function NamedMetrics({ data }) {
  const dp = data.demographicParity, eo = data.equalOpportunity, eq = data.equalizedOdds
  const di = data.disparateImpact, cf = data.counterfactual, cal = data.calibration
  if (!dp && !eo && !eq && !di && !cf && !cal) return null
  const diOk = di?.meetsFourFifthsRule
  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.15 }} className={`card ${styles.panel} ${styles.namedPanel}`}>
      <div className="eyebrow">Named fairness &amp; calibration metrics · held-out set, after mitigation</div>
      <div className={styles.metricGrid}>
        <Metric
          label="Demographic parity"
          value={dp ? num(dp.statisticalParityDifference) : '—'}
          foot="statistical parity difference · max − min P(urgent | group)"
        />
        <Metric
          label="Equal opportunity"
          value={eo ? num(eo.equalOpportunityGap) : '—'}
          foot="max − min true-positive rate on red-flag (P1/P2) cases"
        />
        <Metric
          label="Equalized odds"
          value={eq ? num(eq.equalizedOddsGap) : '—'}
          foot={eq ? `max(TPR gap ${num(eq.tprGap)}, FPR gap ${num(eq.fprGap)})` : 'TPR and FPR gap'}
        />
        <Metric
          label="Disparate impact"
          value={di ? num(di.disparateImpactRatio, 2) : '—'}
          accent={diOk ? '#1F4C3A' : '#E8A13C'}
          foot={di
            ? `four-fifths floor ${di.fourFifthsFloor} · ${diOk ? 'met' : 'below floor — reported, not gated: the 65+ band is correctly flagged urgent more often'}`
            : 'ratio of min/max selection rate'}
        />
        <Metric
          label="Counterfactual (sex flip)"
          value={cf ? pct(cf.sexFlipRate ?? 0) : '—'}
          foot={cf ? `acuity changed on ${cf.n} probes · mean |Δ acuity| ${num(cf.meanAcuityDelta, 2)}` : 'flip rate'}
        />
        <Metric
          label="Calibration (ECE)"
          value={cal ? num(cal.ece) : '—'}
          accent={cal && cal.ece <= 0.05 ? '#1F4C3A' : '#E5533B'}
          foot={cal ? `${cal.method} · Brier ${num(cal.brier)} · gate ECE ≤ 0.05` : 'expected calibration error'}
        />
      </div>
    </motion.div>
  )
}

function Metric({ label, value, foot, accent = '#14231C' }) {
  return (
    <div className={styles.metric}>
      <div className={styles.kpiLabel}>{label}</div>
      <div className={styles.metricValue} style={{ color: accent }}>{value}</div>
      <div className={styles.metricFoot}>{foot}</div>
    </div>
  )
}

function Kpi({ label, value, foot, accent = '#14231C', i = 0 }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: i * 0.06, duration: 0.4 }}
      className={`card ${styles.kpi}`}
    >
      <div className={styles.kpiLabel}>{label}</div>
      <div className={styles.kpiValue} style={{ color: accent }}>{value}</div>
      <div className={styles.kpiFoot}>{foot}</div>
    </motion.div>
  )
}

function GapBar({ label, value, max, color }) {
  return (
    <div>
      <div className={styles.gapHead}>
        <span>{label}</span>
        <span style={{ color }}>{pct(value)}</span>
      </div>
      <div className={styles.gapTrack}>
        <motion.div initial={{ width: 0 }} animate={{ width: `${(value / max) * 100}%` }} transition={{ duration: 0.9, ease: 'easeOut' }} className={styles.gapFill} style={{ background: color }} />
      </div>
    </div>
  )
}

function Drift({ label, value }) {
  const level = value > 0.1 ? { c: '#E5533B', t: 'watch' } : value > 0.05 ? { c: '#E8A13C', t: 'nominal' } : { c: '#3E7A5E', t: 'stable' }
  return (
    <div className={styles.driftRow}>
      <div className={styles.driftLabel}>{label}</div>
      <div className={styles.driftTrack}>
        <motion.div initial={{ width: 0 }} animate={{ width: `${Math.min(100, value * 500)}%` }} transition={{ duration: 0.8 }} className={styles.driftFill} style={{ background: level.c }} />
      </div>
      <div className={styles.driftTag} style={{ color: level.c }}>{level.t}</div>
    </div>
  )
}
