---
tags: [roadmap, frontend, handoff, careroute]
updated: 2026-08-03
---
# Clinician Handoff UI — working notes

Back to [[Home]]. Related: [[Roadmap]] · [[App Overview]]

Scope: the **frontend** side of wiring `backend/app/agents/handoff.py`'s output into the clinician
portal. Backend agent is built + unit-tested but not merged into the pipeline — these notes are only
about what changes once the data exists. See [[CareRoute file isolation rule]] before touching
anything outside `handoff.py` / its own tests.

## Where it shows
Single surface: `frontend/components/ClinicianDashboard.jsx`, right-hand detail panel (`styles.detailCol`).
Not the left queue list — the summary is a detail concern, not a scan-the-queue concern.

Placement in the existing render order:
1. Header (case id, patient summary, acuity badge, confidence)
2. `Section "Normalised symptoms"`
3. `Section "Rationale"`
4. Evidence chips (`styles.evidenceChip`)
5. Feature contributions (`styles.featureChip`)
6. **→ new: Handoff summary block goes here** — after evidence/explanation, before the decision form
7. Decision form (`styles.recordBox`) or decided box (`styles.decidedBox`)

Rationale for the position: evidence/rationale is the AI's raw reasoning; the handoff summary is the
synthesized note meant to *save the clinician's reading time* — so it reads as the last thing before
they act, not mixed in with the raw evidence.

## New section — draft markup
```jsx
{detail.handoffSummary && (
  <div className={styles.block}>
    <div className="eyebrow">Handoff summary</div>
    <p className={styles.sectionText}>{detail.handoffSummary}</p>

    {detail.handoffCitations?.length > 0 && (
      <div className={styles.chipRow}>
        {detail.handoffCitations.map((c, i) => (
          <span key={i} className={styles.evidenceChip} title={c.snippet}>
            {c.title} · {c.source}
          </span>
        ))}
      </div>
    )}

    {detail.handoffQuestions?.length > 0 && (
      <div className={styles.block}>
        <div className="eyebrow">Suggested follow-up questions</div>
        <ul className={styles.sectionText}>
          {detail.handoffQuestions.map((q, i) => <li key={i}>{q}</li>)}
        </ul>
      </div>
    )}
  </div>
)}
```
Reuses existing `.block` / `.sectionText` / `.chipRow` / `.evidenceChip` classes from
`ClinicianDashboard.module.css` — no new CSS needed for a first pass.

## Data contract this depends on
Backend not-yet-done pieces (blocking, owned by whoever merges `models.py` / `store.py` / `main.py`):
- `EscalationDetail` needs `handoffSummary: str`, `handoffCitations: list[Citation]`,
  `handoffQuestions: list[str]` (mirroring `state.handoff_summary/_citations/_questions`).
- `store.create_escalation_from_case` needs to carry those three fields through from `CaseRecord`/`state`.
- `main.py` needs to actually call the handoff agent (after `orchestrate()` returns, i.e. after
  Reflection, gated on `state.escalated`) before building `case_record`.

Until that lands, `getEscalation(id)` in `lib/api.js` simply won't return these fields — the `&&` guards
above mean the new block silently doesn't render, so it's safe to ship the frontend piece ahead of the
backend wiring.

## Offline/demo mock (`MOCK` array in ClinicianDashboard.jsx)
`MOCK` and the `enrich()` fallback currently only synthesize `explanation` when missing. Same pattern
should extend to handoff fields so the offline demo still shows the section — e.g. add
`handoffSummary` / `handoffCitations` / `handoffQuestions` directly to one or two `MOCK` entries (the
ESC-4471 chest-pain and ESC-4468 low-confidence cases are good candidates since they already have
citations to reuse). Don't fabricate a synthesize-on-the-fly fallback the way `enrich()` does for
`explanation` — the whole point of this agent is faithfulness to real case data, a client-side
placeholder text would defeat that for a real (non-demo) case that hasn't been summarised yet.

## Open questions / decisions still needed
- [ ] Follow-up questions: display-only, or should the clinician be able to action one (e.g. send back
  to patient)? Nothing in `handoff.py` or the API assumes an action — treat as read-only for v1.
- [ ] Empty state: when `state.escalated` is true but the agent returned the `not_escalated` no-op path
  (shouldn't happen given the gating, but defensively) — the `&&` guard already handles empty string /
  empty arrays gracefully, no extra empty-state copy needed.
- [ ] Queue list (left column): confirmed no handoff indicator needed there for v1 — revisit only if
  clinicians ask to triage the queue itself by "has a summary ready" vs not.

## Changelog
- 2026-08-03: notes drafted alongside handoff.py review; nothing implemented yet.
