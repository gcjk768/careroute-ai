import { useCallback, useEffect, useRef, useState } from 'react'
import { streamTriage } from './api'

// Drives a single triage run and reduces the SSE event stream into UI state:
//   states   — per-agent { status } map for the visualizer
//   events   — chronological log of every SSE payload
//   override — the safety-override result (or null)
//   result   — the final recommendation (or null)
//   plan     — the orchestrator's validated per-case plan (or null)
//   error    — { status, message } when the run could not complete: an HTTP
//              refusal, an interrupted stream, or the backend's own `error`
//              event (which the guardrail emits with no `final` to follow).
//              The UI must render this instead of silently going idle.
//   running / simulated flags
const INITIAL_STATES = {
  orchestrator: { status: 'idle' },
  intake: { status: 'idle' },
  classifier: { status: 'idle' },
  safety: { status: 'idle' },
  routing: { status: 'idle' },
  hitl: { status: 'idle' },
  reflection: { status: 'idle' },
  handoff: { status: 'idle' },
}

export function useTriage() {
  const [states, setStates] = useState(INITIAL_STATES)
  const [events, setEvents] = useState([])
  const [override, setOverride] = useState(null)
  const [result, setResult] = useState(null)
  const [plan, setPlan] = useState(null)
  const [running, setRunning] = useState(false)
  const [simulated, setSimulated] = useState(false)
  const [error, setError] = useState(null)
  const abortRef = useRef(null)

  const reset = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setStates(INITIAL_STATES)
    setEvents([])
    setOverride(null)
    setResult(null)
    setPlan(null)
    setRunning(false)
    setSimulated(false)
    setError(null)
  }, [])

  // Navigating away mid-run must stop the stream; without this the SSE
  // connection (and the backend work behind it) outlives the component.
  useEffect(() => () => abortRef.current?.abort(), [])

  const onEvent = useCallback((evt) => {
    setEvents((e) => [...e, { ...evt, _t: Date.now() }])
    switch (evt.event) {
      case 'case_open':
        setStates((s) => ({ ...s, orchestrator: { status: 'active' } }))
        break
      case 'agent_active':
        setStates((s) => ({ ...s, [evt.agent]: { status: 'active', label: evt.label } }))
        break
      case 'agent_result':
        setStates((s) => ({ ...s, [evt.agent]: { status: 'done', summary: evt.summary } }))
        break
      case 'safety_override':
        setOverride(evt)
        if (evt.triggered) setStates((s) => ({ ...s, safety: { status: 'flagged' } }))
        break
      case 'plan':
        setPlan(evt)
        break
      case 'final':
        setResult(evt)
        setStates((s) => ({ ...s, orchestrator: { status: 'done' } }))
        break
      // The backend emits this when the guardrail blocks the input; no `final`
      // ever follows, so without handling it the UI would just fall back to
      // "Awaiting intake" and the patient would never learn why.
      case 'error':
        setError({ status: null, message: evt.message || 'The triage run was stopped by a safety check.' })
        break
      default:
        break
    }
  }, [])

  const start = useCallback(
    async (input) => {
      reset()
      // reset() is async via state; seed the orchestrator immediately for snappy UX
      setRunning(true)
      setStates({ ...INITIAL_STATES, orchestrator: { status: 'active' } })
      const controller = new AbortController()
      abortRef.current = controller
      // Everything below is guarded on `abortRef.current === controller`: a run
      // that was superseded (reset() aborted it, and a NEWER run is already
      // in flight) resolves late and must not clobber the new run's state.
      try {
        const outcome = await streamTriage(input, onEvent, controller.signal)
        if (abortRef.current !== controller) return
        setSimulated(!!outcome?.simulated)
        if (outcome?.error) setError(outcome.error)
      } catch {
        // streamTriage is not expected to reject, but if it ever does the run
        // must still end — otherwise `running` stays true forever and the form
        // is stuck on "Triaging…".
        if (abortRef.current !== controller) return
        setError({ status: null, message: 'The triage request failed unexpectedly. Please try again.' })
      } finally {
        if (abortRef.current === controller) setRunning(false)
      }
    },
    [onEvent, reset],
  )

  return { states, events, override, result, plan, running, simulated, error, start, reset }
}
