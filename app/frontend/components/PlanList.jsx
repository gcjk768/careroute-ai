// [Agentic] Plan-and-Execute: the steps the orchestrator chose for THIS case
// (backend agents/planner.py), each with its one-line rationale. `fallback`
// means the proposed plan failed validation and the fixed sequence ran.
export default function PlanList({ plan }) {
  if (!plan) return null
  return (
    <div className="card" style={{ padding: '0.75rem 1rem' }} aria-label="Orchestration plan">
      <div className="eyebrow">Plan · {plan.shape}</div>
      <ol style={{ margin: '0.5rem 0 0', paddingLeft: '1.25rem', fontSize: '0.85rem' }}>
        {plan.steps.map((s) => (
          <li key={s.step}>
            <strong>{s.step}</strong> — {s.rationale}
          </li>
        ))}
      </ol>
      {plan.fallback_reason && <small>Fallback: {plan.fallback_reason}</small>}
    </div>
  )
}
