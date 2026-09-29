'use client'
// Patient triage — the primary [Responsible-AI] surface. A patient describes
// symptoms; the six-agent pipeline runs (visualised live) and returns a cited,
// explainable recommendation with an acuity, care tier, confidence, SHAP-surrogate
// feature contributions, and — for red-flag / low-confidence cases — an escalation
// notice. Reinforces the module's core principle: AI assists, the clinician decides.
import { useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useTriage } from '@/lib/useTriage'
import { submitFeedback } from '@/lib/api'
import { QUICK_REPLIES, MAX_ANSWER_CHARS, appendAnswer, interviewProgress, isInterviewPending } from '@/lib/interview'
import PipelineVisualizer from '@/components/PipelineVisualizer'
import PlanList from '@/components/PlanList'
import AcuityBadge from '@/components/AcuityBadge'
import { acuityOf, adviceOf, AGENTS } from '@/lib/acuity'
import { describeCall, questionCall, summarise, TIERS } from '@/lib/models'
import styles from './PatientTriage.module.css'

const SAMPLES = [
  { label: 'Crushing chest pain', text: 'I have sudden crushing chest pain spreading to my left arm and I feel sweaty and short of breath.' },
  { label: 'Mild sore throat', text: 'Mild sore throat and a runny nose for two days, no fever, still eating normally.' },
  { label: 'Persistent high fever', text: 'High fever for four days now, persistent headache and vomiting, feeling very dehydrated.' },
  { label: 'Possible stroke', text: 'My father suddenly has face droop on one side, slurred speech and weakness in his right arm.' },
]

const LANGS = ['English', '中文', 'Bahasa Melayu', 'தமிழ்']

export default function PatientTriage() {
  const [text, setText] = useState('')
  const [language, setLanguage] = useState('English')
  const [isVoice, setIsVoice] = useState(false)
  const [ageBand, setAgeBand] = useState('')
  const [sex, setSex] = useState('')
  const [location, setLocation] = useState(null)
  const [locationStatus, setLocationStatus] = useState('idle')
  const [locationMessage, setLocationMessage] = useState('')
  const [transportMode, setTransportMode] = useState('unknown')
  const [maxTravelTimeMin, setMaxTravelTimeMin] = useState('45')
  const [accessibilityNeed, setAccessibilityNeed] = useState('none')
  const [affordabilityPreference, setAffordabilityPreference] = useState('standard')
  // The clarifying interview: every answer so far, re-posted with the complaint
  // on each turn (the server keeps no conversation state). Lives here, not in
  // useTriage, because each turn is a fresh run and the thread must outlive it.
  const [transcript, setTranscript] = useState([])
  // The complaint the thread is about — frozen at submit so editing the textarea
  // mid-interview does not rewrite the first bubble.
  const [complaint, setComplaint] = useState('')
  const { states, override, result, plan, running, simulated, error, start, reset } = useTriage()
  // An error keeps the right column open: collapsing back to the idle card would
  // hide the reason the run stopped (and any partial result already rendered).
  const started = running || !!result || !!error
  const transportRequired = Boolean(location) && transportMode === 'unknown'
  const interviewing = isInterviewPending(result)
  const liveStatus = triageStatusMessage({ running, states, result, error, interviewing })

  const triageInput = ({
    emergencySelfTransportConfirmed = false,
    emergencyTransportMode = transportMode,
    clarifications = transcript,
  } = {}) => ({
    text: text.trim(), language, isVoice, ageBand: ageBand || null, sex: sex || null,
    latitude: location?.latitude ?? null,
    longitude: location?.longitude ?? null,
    transportMode: emergencyTransportMode,
    emergencySelfTransportConfirmed,
    maxTravelTimeMin: location ? Number(maxTravelTimeMin) : null,
    accessibilityNeed,
    affordabilityPreference,
    clarifications,
  })

  const submit = (e) => {
    e?.preventDefault()
    if (!text.trim() || transportRequired) return
    // A new complaint starts a new conversation.
    setTranscript([])
    setComplaint(text.trim())
    start(triageInput({ clarifications: [] }))
  }

  const answer = (reply) => {
    if (!interviewing || running) return
    // `translated` is display-only; the backend ignores the extra field.
    const next = appendAnswer(transcript, { ...result.clarification, translated: result.translations?.question }, reply)
    if (next === transcript) return
    setTranscript(next)
    start(triageInput({ clarifications: next }))
  }

  const resetAll = () => {
    reset()
    setText('')
    setTranscript([])
    setComplaint('')
  }

  const requestEmergencySelfTransportRoute = (mode) => {
    if (!location || !text.trim()) return
    start(triageInput({ emergencySelfTransportConfirmed: true, emergencyTransportMode: mode }))
  }

  const useMyLocation = () => {
    if (!navigator.geolocation) {
      setLocationStatus('error')
      setLocationMessage('This browser does not support location access.')
      return
    }
    setLocationStatus('loading')
    setLocationMessage('Requesting your location…')
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        setLocation({ latitude: Number(coords.latitude.toFixed(5)), longitude: Number(coords.longitude.toFixed(5)) })
        setLocationStatus('ready')
        setLocationMessage('Location added for nearby clinic routing.')
      },
      () => {
        setLocationStatus('error')
        setLocationMessage('Location was not shared. You can still run triage without a clinic route.')
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 },
    )
  }

  return (
    <div className={styles.container}>
      {/* Hero */}
      <header className={styles.hero}>
        <div className="eyebrow">Patient intake</div>
        <h1 className={styles.heroTitle}>
          Describe what you feel.
          <span className={styles.pineSoft}> We route you safely.</span>
        </h1>
        <p className={styles.heroLede}>
          A safety-gated pipeline of seven agents estimates urgency, applies hard clinical red-flag rules, and
          returns a cited recommendation. Borderline cases go to a human clinician —{' '}
          <span className={styles.mediumInk}>AI assists, the clinician decides.</span>
        </p>
      </header>

      <DisclosureNotice />

      <div className={styles.grid}>
        {/* Intake form */}
        <form onSubmit={submit} className={`card ${styles.form}`}>
          <label className="eyebrow" htmlFor="symptoms">
            Your symptoms
          </label>
          <textarea
            id="symptoms"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="e.g. chest tightness and breathlessness since this morning…"
            rows={5}
            className={`focusable ${styles.textarea}`}
          />

          <div className={styles.sampleRow}>
            {SAMPLES.map((s) => (
              <button
                key={s.label}
                type="button"
                onClick={() => setText(s.text)}
                className={`focusable ${styles.sampleBtn}`}
              >
                {s.label}
              </button>
            ))}
          </div>

          {/* Optional structured details — age/sex feed the age-aware acuity model */}
          <div className={styles.detailsGrid}>
            <label className={styles.srOnly} htmlFor="age">Age band</label>
            <select
              id="age"
              value={ageBand}
              onChange={(e) => setAgeBand(e.target.value)}
              className={`focusable ${styles.select}`}
            >
              <option value="">Age (optional)</option>
              {['0-17', '18-39', '40-64', '65+'].map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
            <label className={styles.srOnly} htmlFor="sex">Sex</label>
            <select
              id="sex"
              value={sex}
              onChange={(e) => setSex(e.target.value)}
              className={`focusable ${styles.select}`}
            >
              <option value="">Sex (optional)</option>
              <option>Female</option>
              <option>Male</option>
            </select>
          </div>

          <div className={styles.controlRow}>
            <label className={styles.srOnly} htmlFor="language">Language</label>
            <select
              id="language"
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              className={`focusable ${styles.langSelect}`}
            >
              {LANGS.map((l) => (
                <option key={l}>{l}</option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => setIsVoice((v) => !v)}
              aria-pressed={isVoice}
              className={`focusable ${styles.voiceBtn} ${
                isVoice ? styles.voiceOn : styles.voiceOff
              }`}
            >
              <span>🎙</span> Voice {isVoice ? 'on' : 'off'}
            </button>
          </div>

          <section className={styles.locationPanel} aria-labelledby="location-heading">
            <div>
              <div id="location-heading" className="eyebrow">Optional clinic routing</div>
              <p className={styles.locationText}>
                Share your approximate location to find nearby eligible clinics. It is used only for this triage request.
              </p>
            </div>
            <button
              type="button"
              onClick={useMyLocation}
              disabled={running || locationStatus === 'loading'}
              className={`focusable ${styles.locationButton}`}
            >
              {locationStatus === 'loading' ? 'Finding location…' : location ? 'Location added' : 'Use my location'}
            </button>
            {locationMessage && (
              <p className={locationStatus === 'error' ? styles.locationError : styles.locationStatus} role="status">
                {locationMessage}
              </p>
            )}
            {location && (
              <div className={styles.routingPrefs}>
                <label className={styles.srOnly} htmlFor="transport">Transport mode</label>
                <select id="transport" value={transportMode} onChange={(e) => setTransportMode(e.target.value)} className={`focusable ${styles.select}`}>
                  <option value="unknown">How will you travel?</option>
                  <option value="walk">Walking</option>
                  <option value="cycle">Cycling</option>
                  <option value="public">Public transport</option>
                  <option value="drive">Driving</option>
                  <option value="taxi">Taxi</option>
                </select>
                {transportRequired && (
                  <p className={styles.transportRequired} role="status">
                    Select how you will travel to receive an accurate route and travel estimate.
                  </p>
                )}
                <label className={styles.srOnly} htmlFor="accessibility">Accessibility need</label>
                <select id="accessibility" value={accessibilityNeed} onChange={(e) => setAccessibilityNeed(e.target.value)} className={`focusable ${styles.select}`}>
                  <option value="none">Accessibility needs (optional)</option>
                  <option value="step_free">Step-free access needed</option>
                  <option value="wheelchair">Wheelchair access needed</option>
                </select>
                <label className={styles.srOnly} htmlFor="affordability">Affordability preference</label>
                <select id="affordability" value={affordabilityPreference} onChange={(e) => setAffordabilityPreference(e.target.value)} className={`focusable ${styles.select}`}>
                  <option value="standard">Standard clinic search</option>
                  <option value="chas_subsidy">CHAS-eligible clinic preferred</option>
                </select>
                <label className={styles.srOnly} htmlFor="max-travel">Maximum travel time</label>
                <select id="max-travel" value={maxTravelTimeMin} onChange={(e) => setMaxTravelTimeMin(e.target.value)} className={`focusable ${styles.select}`}>
                  {[15, 30, 45, 60, 90].map((minutes) => <option key={minutes} value={minutes}>{minutes} min maximum</option>)}
                </select>
              </div>
            )}
          </section>

          <div className={styles.submitRow}>
            <button type="submit" disabled={running || !text.trim() || transportRequired} className={`btn-primary ${styles.grow}`}>
              {running ? 'Triaging…' : 'Run triage'}
            </button>
            {started && (
              <button type="button" onClick={resetAll} className="btn-ghost">
                Reset
              </button>
            )}
          </div>

          <p className={styles.disclaimer}>
            Not for emergencies. If this is life-threatening, call your local emergency number now.
          </p>
        </form>

        {/* Live pipeline + result.
            Announcements come from the dedicated status line at the end of this
            column, NOT from the column itself: making the whole column live
            re-read the entire pipeline, result card, citations and map on every
            SSE event, which is unusable with a screen reader. */}
        <div className={styles.rightCol}>
          <AnimatePresence mode="wait">
            {!started ? (
              <motion.div
                key="idle"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className={`card ${styles.idle}`}
              >
                <div>
                  <div className={styles.idleRule} />
                  <p className={styles.idleTitle}>Awaiting intake</p>
                  <p className={styles.idleText}>
                    Enter symptoms or pick a sample case to watch the agents activate one by one.
                  </p>
                </div>
              </motion.div>
            ) : (
              <motion.div key="run" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className={styles.runStack}>
                <PipelineVisualizer states={states} override={override} />
                <PlanList plan={plan} />
                {error && <TriageError error={error} />}
                {(transcript.length > 0 || interviewing) && (
                  <InterviewThread
                    complaint={complaint}
                    transcript={transcript}
                    pending={interviewing ? { ...result.clarification, translated: result.translations?.question } : null}
                    questionModel={interviewing ? questionCall(result.llm) : null}
                    progress={interviewProgress(result)}
                    running={running}
                    onAnswer={answer}
                  />
                )}
                <AnimatePresence>
                  {result && !interviewing && (
                    <Recommendation result={result} simulated={simulated} onEmergencySelfTransportRoute={requestEmergencySelfTransportRoute} />
                  )}
                </AnimatePresence>
              </motion.div>
            )}
          </AnimatePresence>
          <p className={styles.srOnly} role="status" aria-live="polite">
            {liveStatus}
          </p>
        </div>
      </div>
    </div>
  )
}

// One short sentence for the visually-hidden live region — a milestone, never a
// re-read of the whole column.
function triageStatusMessage({ running, states, result, error, interviewing }) {
  if (error) return `Triage stopped. ${error.message}`
  if (interviewing) return `Question: ${result.clarification.question}`
  if (result) return 'Recommendation ready'
  if (!running) return ''
  const active = AGENTS.find((a) => states[a.key]?.status === 'active')
  if (active) return `${active.name} is working`
  const done = AGENTS.filter((a) => ['done', 'flagged'].includes(states[a.key]?.status)).length
  // No "of N": Clinician-Handoff only runs on escalated cases, so a fixed total
  // would announce "6 of 7 complete" forever on a routine run.
  return done ? `${done} ${done === 1 ? 'agent' : 'agents'} complete` : 'Triage started'
}

// The run could not complete: a refusal (rate limit, invalid input), a dropped
// stream, or a guardrail block. Shown INSTEAD of a recommendation — and, for an
// interrupted stream, above whatever partial real output was already rendered.
function TriageError({ error }) {
  return (
    <div className={`card ${styles.errorCard}`} role="alert">
      <div className="eyebrow">Triage could not complete</div>
      <p className={styles.errorText}>{error.message}</p>
      <p className={styles.errorHint}>
        No recommendation has been made. If you feel this is an emergency, call 995 now.
      </p>
    </div>
  )
}

// The patient's language first, the English original underneath. English-only
// when the backend sent no translation (English input, or translation failed).
function Bilingual({ text, translated }) {
  if (!translated || translated === text) return text
  return (
    <>
      <span>{translated}</span>
      <span className={styles.englishLine} lang="en">{text}</span>
    </>
  )
}

// The clarifying interview as a chat thread: the complaint, each question the
// agents asked with the patient's answer, the pending question, and an answer
// box with quick replies. Every answer re-runs the pipeline with the whole
// transcript (docs/design/specs/2026-09-26-clarifying-chat-interview-design.md).
function InterviewThread({ complaint, transcript, pending, questionModel, progress, running, onAnswer }) {
  const [draft, setDraft] = useState('')
  const inputRef = useRef(null)
  const pendingQuestion = pending?.question ?? null
  // A fresh question gets the cursor; the draft never carries over between questions.
  useEffect(() => {
    setDraft('')
    if (pendingQuestion && !running) inputRef.current?.focus()
  }, [pendingQuestion, running])

  const send = (e) => {
    e?.preventDefault()
    if (!draft.trim()) return
    onAnswer(draft)
  }

  return (
    <motion.section
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={`card ${styles.chat}`}
      aria-label="Clarifying questions"
    >
      <div className={styles.chatHead}>
        <div className="eyebrow">A few questions first</div>
        {progress && (
          <span className={styles.chatProgress}>Question {progress.current} of {progress.budget}</span>
        )}
      </div>
      <ol className={styles.thread}>
        {complaint && <li className={`${styles.bubble} ${styles.bubblePatient}`}>{complaint}</li>}
        {transcript.map((turn, index) => (
          <li key={`${index}-${turn.question}`} className={styles.turn}>
            <div className={`${styles.bubble} ${styles.bubbleAgent}`}><Bilingual text={turn.question} translated={turn.translated} /></div>
            <div className={`${styles.bubble} ${styles.bubblePatient}`}>{turn.answer}</div>
          </li>
        ))}
        {pending && !running && (
          <li className={`${styles.bubble} ${styles.bubbleAgent}`} data-testid="pending-question"><Bilingual text={pending.question} translated={pending.translated} /></li>
        )}
        {/* Kept OUTSIDE the question bubble: tests read the bubble's exact text. */}
        {pending && !running && questionModel && (
          <li className={styles.modelTag}>
            worded by <span className={styles.modelName}>{questionModel.model}</span>
            <TierBadge tier={questionModel.tier} />
          </li>
        )}
        {running && <li className={`${styles.bubble} ${styles.bubbleAgent} ${styles.bubbleMuted}`}>Thinking…</li>}
      </ol>
      {pending && !running && (
        <form onSubmit={send} className={styles.answerForm}>
          <div className={styles.quickReplies} role="group" aria-label="Quick replies">
            {QUICK_REPLIES.map((reply) => (
              <button key={reply} type="button" onClick={() => onAnswer(reply)} className={`focusable ${styles.sampleBtn}`}>
                {reply}
              </button>
            ))}
          </div>
          <div className={styles.answerRow}>
            <label className={styles.srOnly} htmlFor="interview-answer">Your answer</label>
            <input
              id="interview-answer"
              ref={inputRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              maxLength={MAX_ANSWER_CHARS}
              placeholder="Type your answer…"
              className={`focusable ${styles.answerInput}`}
              autoComplete="off"
            />
            <button type="submit" disabled={!draft.trim()} className="btn-primary">
              Send
            </button>
          </div>
          <p className={styles.chatFoot}>
            Answer as best you can. “Not sure” is a fine answer. If this is an emergency, call 995 now.
          </p>
        </form>
      )}
    </motion.section>
  )
}

function Recommendation({ result, simulated, onEmergencySelfTransportRoute }) {
  // routing.py sets five different `routing_reason` strings and may attach a
  // structured `routing_clarification`. Key the "share your location"
  // call-to-action on the structured kind, falling back to the original
  // sentence so an older backend still behaves as before.
  const clarification = result.routingClarification
  const locationRequired =
    clarification?.kind === 'location' || result.routingReason === 'Location is required to find nearby clinics.'
  const hasTravelEstimate = ['onemap_route', 'geodesic_speed_estimate'].includes(result.travelEstimateSource)
  const a = acuityOf(result.acuity?.code)
  const conf = Math.round((result.confidence || 0) * 100)
  const isP2 = result.acuity?.code === 'P2_EMERGENT'
  const p2RouteRequested = Boolean(result.routingPlan?.self_transport?.route_requested)
  const p2LocationMissing = isP2 && !Number.isFinite(result.clinicLatitude) && !Number.isFinite(result.clinicLongitude)
  const [showP2Transport, setShowP2Transport] = useState(false)
  const [p2TransportMode, setP2TransportMode] = useState('taxi')
  return (
    <motion.div
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
      className={`card ${styles.recCard}`}
    >
      {/* top band coloured by acuity */}
      <div className={styles.acuityBand} style={{ background: a.color }} />
      <div className={styles.recBody}>
        <div className={styles.recHeader}>
          <div>
            <div className="eyebrow">Recommendation</div>
            <h3 className={styles.recTier}>{result.careTier}</h3>
            <div className={styles.caseId}>
              case {result.caseId}
            </div>
          </div>
          <AcuityBadge code={result.acuity?.code} size="lg" />
        </div>

        {/* confidence meter */}
        <div className={styles.confBlock}>
          <div className={styles.confLabel}>
            <span>Classifier confidence</span>
            <span>{conf}%</span>
          </div>
          <div className={styles.confTrack}>
            <motion.div
              initial={{ width: 0 }}
              animate={{ width: `${conf}%` }}
              transition={{ duration: 0.9, ease: 'easeOut' }}
              className={styles.confFill}
              style={{ background: a.color }}
            />
          </div>
        </div>

        {result.escalated && (
          <div className={styles.escalated}>
            <span className={styles.escalatedIcon}>✚</span>
            {/* WORDING IS SAFETY-CRITICAL. On a P1/P2 the old copy ("routed for
                human review; the final decision rests with the clinician and
                will supersede this recommendation") invites a patient to WAIT
                for that clinician. Review happens alongside them acting on the
                advice, never instead of it, so on an emergency we say so first
                and never imply a pending decision. */}
            {adviceOf(result.acuity?.code).tone === 'critical' ? (
              <p className={styles.escalatedText}>
                <span className={styles.semibold}>A clinician has been notified — do not wait for them.</span>{' '}
                Follow the emergency steps below now. A clinician is reviewing this case at the same
                time, and will be in touch if anything changes.
              </p>
            ) : (
              <p className={styles.escalatedText}>
                <span className={styles.semibold}>A clinician will check this case.</span> They may update the
                advice below, so follow it in the meantime. If your symptoms get worse before you hear
                back, treat it as an emergency and call 995.
              </p>
            )}
          </div>
        )}

        {/* Actionable next step — the most important thing for a patient */}
        <ActionGuidance code={result.acuity?.code} />

        {result.redactedPii?.length > 0 && (
          <p className={styles.piiNote}>
            <span aria-hidden>🔒</span> Personal identifiers ({result.redactedPii.join(', ').toLowerCase()}) were masked before processing.
          </p>
        )}

        <div className={styles.rationaleBlock}>
          <div className="eyebrow">Rationale</div>
          <p className={styles.rationaleText}><Bilingual text={result.rationale} translated={result.translations?.rationale} /></p>
        </div>

        {/* Explainability — SHAP-surrogate feature contributions from the classifier */}
        {result.explanation?.length > 0 && <ExplanationBars items={result.explanation} source={result.explanationSource} />}

        {/* [XRAI] Counterfactual — the "what would change this?" explanation.
            Computed by the served model as a single symptom edit; never shown
            on the LLM / keyword paths (the backend sends null there). */}
        {result.counterfactual?.sentence && (
          <div className={styles.counterfactualBlock}>
            <div className="eyebrow">What would change this assessment</div>
            <p className={styles.counterfactualText}>{result.counterfactual.sentence}</p>
            <p className={styles.counterfactualFoot}>
              A single-symptom “what if” on the same model that produced the bars above. It describes the model, not medical advice — tell us everything you are experiencing.
            </p>
          </div>
        )}

        {/* Why Care-Routing did what it did, in its own words, plus the one
            logistics question it wants answered. Rendered for every reason the
            backend can set — not just the location one. */}
        {(result.routingReason || clarification?.question) && (
          <div className={styles.routingNote}>
            {result.routingReason && <p className={styles.routingReason}><Bilingual text={result.routingReason} translated={result.translations?.routingReason} /></p>}
            {clarification?.question && <p className={styles.routingQuestion}><Bilingual text={clarification.question} translated={result.translations?.routingQuestion} /></p>}
          </div>
        )}

        {locationRequired ? (
          <div className={styles.locationNeeded}>
            <div className="eyebrow">Find a nearby GP</div>
            <p>Share your location to see verified nearby CHAS clinics, an accurate travel estimate, and OneMap directions. We have not selected a nearest clinic without it.</p>
          </div>
        ) : (
          <div className={styles.statGrid}>
            {hasTravelEstimate && typeof result.waitTimeMin === 'number' && (
              <Stat label="Travel estimate" value={`${result.waitTimeMin} min`} />
            )}
            {result.clinic && <Stat label="Suggested site" value={result.clinic} />}
          </div>
        )}

        {result.travelEstimateSource && !locationRequired && (
          <div className={styles.routeBlock}>
            <div className="eyebrow">Getting there</div>
            <p className={styles.routeSource}>
              {result.travelEstimateSource === 'onemap_route'
                ? 'OneMap route estimate'
                : result.travelEstimateSource === 'geodesic_speed_estimate'
                  ? 'Approximate distance-and-speed estimate'
                  : 'Route estimate unavailable'}
            </p>
            {p2LocationMissing && (
              <p className={styles.emergencyLocationPrompt}>
                Share your location to see the nearest reviewed public 24-hour Emergency Department on the map. We have not selected a hospital without it. If you are in immediate danger or cannot travel safely, call 995 now.
              </p>
            )}
            {result.transportMode === 'public' && result.travelEstimateSource !== 'onemap_route' && (
              <p className={styles.routeFallbackNote}>
                OneMap did not find a public-transport itinerary for this journey. This is a rough estimate only, not transit directions. For a nearby clinic, walking may be more suitable.
              </p>
            )}
            {result.routingPlan?.replanned && (
              <p className={styles.routeReplanNote}>
                Public transport was unavailable. We verified a nearby walking route instead ({result.waitTimeMin} min).
              </p>
            )}
            {result.routeInstructions?.length > 0 && (
              <ol className={styles.routeList}>
                {result.routeInstructions.map((instruction, index) => <li key={index}>{instruction}</li>)}
              </ol>
            )}
            {result.alternativeClinics?.length > 0 && (
              <AlternativeClinics clinics={result.alternativeClinics} />
            )}
            {result.oneMapUrl && Number.isFinite(result.clinicLatitude) && Number.isFinite(result.clinicLongitude) && (
              <div className={styles.mapPreview}>
                <OneMapPreview
                  latitude={result.clinicLatitude}
                  longitude={result.clinicLongitude}
                  clinicName={result.clinic}
                  routeGeometry={result.routeGeometry}
                  transportMode={result.transportMode}
                  routeAvailable={result.routeAvailable}
                />
                <a className={`focusable ${styles.mapLink}`} href={result.oneMapUrl} target="_blank" rel="noopener noreferrer">
                  Open interactive OneMap
                </a>
              </div>
            )}
          </div>
        )}

        {isP2 && !p2RouteRequested && (
          <section className={styles.emergencyTransport} aria-label="Optional emergency self-transport directions">
            <div className="eyebrow">Emergency travel option</div>
            <p>
              If you are in immediate danger, alone, worsening, or cannot travel safely, call 995 now.
              Do not use these directions as ambulance guidance.
            </p>
            {!result.oneMapUrl ? (
              <p className={styles.emergencyTransportNote}>
                Share your location above before requesting a nearby public Emergency Department and self-transport directions.
              </p>
            ) : !showP2Transport ? (
              <button type="button" className="btn-primary" onClick={() => setShowP2Transport(true)}>
                I can travel safely — choose transport
              </button>
            ) : (
              <div className={styles.emergencyTransportControls}>
                <label htmlFor="emergency-transport">How will you travel?</label>
                <select
                  id="emergency-transport"
                  value={p2TransportMode}
                  onChange={(event) => setP2TransportMode(event.target.value)}
                  className={`focusable ${styles.select}`}
                >
                  <option value="taxi">Taxi / ride-hailing</option>
                  <option value="drive">Drive</option>
                  <option value="walk">Walk</option>
                  <option value="public">Public transport</option>
                  <option value="cycle">Cycle</option>
                </select>
                <button type="button" className="btn-primary" onClick={() => onEmergencySelfTransportRoute(p2TransportMode)}>
                  Show self-transport directions
                </button>
                <p className={styles.emergencyTransportNote}>
                  This confirmation is not medical clearance. If symptoms worsen or travel feels unsafe, call 995.
                </p>
              </div>
            )}
          </section>
        )}

        {result.citations?.length > 0 && (
          <div className={styles.citeBlock}>
            <div className="eyebrow">Grounding evidence</div>
            <ul className={styles.citeList}>
              {result.citations.map((c, i) => (
                <li key={i} className={styles.tile}>
                  <div className={styles.citeHead}>
                    <span className={styles.citeTitle}>{c.title}</span>
                    <span className={styles.citeSource}>{c.source}</span>
                  </div>
                  <p className={styles.citeSnippet}>{c.snippet}</p>
                </li>
              ))}
            </ul>
          </div>
        )}

        {Array.isArray(result.llm) && <ModelsUsed calls={result.llm} />}

        {/* Feedback (real cases only — the simulation has no server-side case id)
            + a print-friendly summary the patient can take to a clinic. */}
        {!simulated && result.caseId ? (
          <FeedbackControl caseId={result.caseId} />
        ) : null}

        <div className={`print-hidden ${styles.printRow}`}>
          <button onClick={() => window.print()} className={`btn-ghost ${styles.textSm}`}>🖨 Print summary</button>
          <span className={styles.printHint}>Bring this to your clinic if you seek care.</span>
        </div>

        {simulated && (
          <p className={styles.simNote}>
            ⚠ backend offline — result produced by the in-browser simulation.
          </p>
        )}
      </div>
    </motion.div>
  )
}

let oneMapLeafletLoader

function loadOneMapLeaflet() {
  if (typeof window === 'undefined') return Promise.reject(new Error('Browser map unavailable during server rendering'))
  if (window.L) return Promise.resolve(window.L)
  if (oneMapLeafletLoader) return oneMapLeafletLoader

  oneMapLeafletLoader = new Promise((resolve, reject) => {
    const css = document.createElement('link')
    css.rel = 'stylesheet'
    css.href = 'https://www.onemap.gov.sg/web-assets/libs/leaflet/leaflet.css'
    document.head.appendChild(css)

    const script = document.createElement('script')
    script.src = 'https://www.onemap.gov.sg/web-assets/libs/leaflet/onemap-leaflet.js'
    script.async = true
    script.onload = () => window.L ? resolve(window.L) : reject(new Error('OneMap map library did not load'))
    script.onerror = () => reject(new Error('Unable to load OneMap map library'))
    document.head.appendChild(script)
  })
  // A rejected promise must not be cached: keeping it would disable the map for
  // the whole tab after one CDN hiccup. Dropping it lets the next mount retry.
  // (This also marks the rejection handled, so it is not an unhandled rejection
  // when no component is currently waiting on it.)
  oneMapLeafletLoader.catch(() => {
    oneMapLeafletLoader = null
  })
  return oneMapLeafletLoader
}

// Leaflet sets tooltip content with innerHTML. `clinicName` comes from a
// third-party clinic directory, so it is passed as a text node — never as
// markup. (Constant tooltips elsewhere in this file are safe as strings.)
function textTooltip(value) {
  const node = document.createElement('span')
  node.textContent = value
  return node
}

function OneMapPreview({ latitude, longitude, clinicName, routeGeometry, transportMode, routeAvailable }) {
  const mapElement = useRef(null)
  const [mapFailed, setMapFailed] = useState(false)

  useEffect(() => {
    let map
    let cancelled = false
    setMapFailed(false)

    loadOneMapLeaflet().then((L) => {
      if (cancelled || !mapElement.current) return
      map = L.map(mapElement.current, { zoomControl: true, attributionControl: true })
      map.setView([latitude, longitude], 17)
      L.tileLayer('https://www.onemap.gov.sg/maps/tiles/Default/{z}/{x}/{y}.png', {
        detectRetina: true,
        minZoom: 11,
        maxZoom: 19,
        attribution: '<img src="https://www.onemap.gov.sg/web-assets/images/logo/om_logo.png" style="height:16px;width:16px;vertical-align:middle"/> <a href="https://www.onemap.gov.sg/" target="_blank" rel="noopener noreferrer">OneMap</a> &copy; contributors | <a href="https://www.sla.gov.sg/" target="_blank" rel="noopener noreferrer">Singapore Land Authority</a>',
      }).addTo(map)
      L.circleMarker([latitude, longitude], {
        radius: 10,
        color: '#ffffff',
        weight: 3,
        fillColor: '#1e6b4f',
        fillOpacity: 1,
      }).addTo(map).bindTooltip(textTooltip(clinicName || 'Selected clinic'), { permanent: true, direction: 'top', offset: [0, -10] })
      const route = Array.isArray(routeGeometry)
        ? routeGeometry.filter((point) => Array.isArray(point) && point.length === 2 && point.every(Number.isFinite))
        : []
      if (route.length >= 2) {
        L.circleMarker(route[0], {
          radius: 8,
          color: '#ffffff',
          weight: 3,
          fillColor: '#2266aa',
          fillOpacity: 1,
        }).addTo(map).bindTooltip('Your location', { permanent: true, direction: 'top', offset: [0, -10] })
        const isPublicTransport = transportMode === 'public'
        const line = L.polyline(route, {
          color: isPublicTransport ? '#2266aa' : '#1e6b4f',
          weight: 5,
          opacity: 0.85,
          dashArray: isPublicTransport ? '10 8' : undefined,
        }).addTo(map)
        map.fitBounds(line.getBounds(), { padding: [28, 28], maxZoom: 17 })
      }
    }).catch(() => {
      // Say so rather than leaving a blank grey box. The OneMap link below
      // remains a functional fallback when the map CDN is blocked.
      if (!cancelled) setMapFailed(true)
    })

    return () => {
      cancelled = true
      map?.remove()
    }
  }, [latitude, longitude, clinicName, routeGeometry, transportMode])

  const hasRoute = routeAvailable && Array.isArray(routeGeometry) && routeGeometry.length >= 2

  return (
    <>
      {/* `hidden` rather than unmounted: the ref must survive a failure so a
          later retry (the loader cache is cleared on rejection) can still
          initialise into this element. */}
      <div
        className={styles.mapCanvas}
        ref={mapElement}
        hidden={mapFailed}
        role="img"
        aria-label={`Map showing ${clinicName || 'the selected clinic'}`}
      />
      {mapFailed && (
        <p className={styles.mapUnavailable} role="status">
          Map unavailable — the OneMap map library could not be loaded. Use the OneMap link below for directions.
        </p>
      )}
      <p className={styles.mapLegend}>
        {hasRoute ? (
          <>
            <span className={transportMode === 'public' ? styles.publicRouteKey : styles.routeKey} />
            {transportMode === 'public' ? 'OneMap public-transport route' : 'OneMap route'}
            <span className={styles.startKey} /> Your location
          </>
        ) : (
          <>Clinic location only — a OneMap route was unavailable.</>
        )}
        <span className={styles.clinicKey} /> Clinic
      </p>
    </>
  )
}

// Dismissible AI-use + privacy disclosure (PDPC Stakeholder-Interaction pillar).
// Tells the public plainly: this is AI-assisted, a clinician decides, and personal
// identifiers are automatically masked before processing.
function DisclosureNotice() {
  // Read the ack AFTER mount: the server can't see localStorage and always
  // renders the notice, so reading it during render made a returning visitor's
  // first client render differ (React #418 hydration error on the live site).
  const [open, setOpen] = useState(true)
  useEffect(() => {
    try { if (localStorage.getItem('cr_disclosure_ack') === '1') setOpen(false) } catch { /* keep open */ }
  }, [])
  if (!open) return null
  const dismiss = () => {
    try { localStorage.setItem('cr_disclosure_ack', '1') } catch { /* ignore */ }
    setOpen(false)
  }
  return (
    <div className={styles.disclosure}>
      <span className={styles.disclosureIcon} aria-hidden>ℹ︎</span>
      <div className={styles.disclosureText}>
        <span className={styles.semiInk}>How this works &amp; your privacy.</span>{' '}
        CareRoute is an <span className={styles.medium}>AI assistant that helps prioritise care — it is not a diagnosis</span>, and a
        clinician reviews every urgent case. Before your text is processed, we automatically{' '}
        <span className={styles.medium}>mask personal identifiers</span> (e.g. NRIC, phone, email). Please don’t enter more personal
        detail than needed.
      </div>
      <button onClick={dismiss} className={`focusable ${styles.dismissBtn}`}>
        Got it
      </button>
    </div>
  )
}

// Plain-language "what to do now" — an actionable next step per acuity.
function ActionGuidance({ code }) {
  const advice = adviceOf(code)
  const critical = advice.tone === 'critical'
  const border = critical ? styles.borderCritical : advice.tone === 'urgent' ? styles.borderUrgent : styles.borderCalm
  const bg = critical ? styles.bgCritical : advice.tone === 'urgent' ? styles.bgUrgent : styles.bgCalm
  return (
    <div className={`${styles.action} ${border} ${bg}`}>
      <div className="eyebrow">What to do now</div>
      <p className={styles.actionHeadline}>{advice.headline}</p>
      <ul className={styles.stepList}>
        {advice.steps.map((s, i) => (
          <li key={i} className={styles.stepItem}>
            <span className={styles.stepDot} aria-hidden />
            {s}
          </li>
        ))}
      </ul>
    </div>
  )
}

// Feedback control — wires POST /api/cases/{id}/feedback (PDPC feedback/redress).
// Which model served each LLM step of this case, and at which tier. The router
// (backend/app/llm.py) picks the tier by how heavy the task is; see lib/models.js.
function ModelsUsed({ calls }) {
  const { count, models, escalated } = summarise(calls)
  return (
    <section className={styles.citeBlock} aria-labelledby="models-heading">
      <div className="eyebrow" id="models-heading">AI models used for this case</div>
      {count === 0 ? (
        <p className={styles.modelEmpty}>
          No language model was called — the rules and the trained severity model decided this case.
        </p>
      ) : (
        <>
          <p className={styles.modelSummary}>
            {count} call{count === 1 ? '' : 's'} · {models.join(', ')}
            {escalated > 0 && ` · ${escalated} escalated to the strongest model`}
          </p>
          <ul className={styles.citeList}>
            {calls.map((c, i) => {
              const d = describeCall(c)
              return (
                <li key={i} className={`${styles.tile} ${styles.modelRow}`}>
                  <div className={styles.citeHead}>
                    <span className={styles.citeTitle}>
                      {d.agent} <span className={styles.modelWhat}>· {d.what}</span>
                    </span>
                    <TierBadge tier={d.tier} />
                  </div>
                  <div className={styles.modelMeta}>
                    <span className={styles.modelName}>{d.model}</span>
                    {d.ms != null && <span>{(d.ms / 1000).toFixed(2)} s</span>}
                    {d.note && <span className={styles.modelNote}>{d.note}</span>}
                  </div>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </section>
  )
}

function TierBadge({ tier }) {
  const cls = { fast: styles.tierFast, deep: styles.tierDeep, max: styles.tierMax }[tier] ?? styles.tierOther
  return (
    <span className={`${styles.tierBadge} ${cls}`} title={TIERS[tier] ?? TIERS.default}>
      {tier}
    </span>
  )
}

function FeedbackControl({ caseId }) {
  const [sent, setSent] = useState(false)
  const [helpful, setHelpful] = useState(null)
  const send = async (isHelpful) => {
    setHelpful(isHelpful)
    setSent(true)
    try { await submitFeedback(caseId, { helpful: isHelpful, comment: '' }) } catch { /* best-effort */ }
  }
  return (
    <div className={styles.feedback}>
      <span className={styles.feedbackLabel}>Was this helpful?</span>
      {sent ? (
        <span className={styles.feedbackThanks}>
          Thanks — {helpful ? 'glad it helped' : 'we’ll use this to improve'} ✓
        </span>
      ) : (
        <div className={styles.feedbackBtns}>
          <button onClick={() => send(true)} className={`focusable ${styles.feedbackBtn}`} aria-label="Helpful">👍 Yes</button>
          <button onClick={() => send(false)} className={`focusable ${styles.feedbackBtn}`} aria-label="Not helpful">👎 No</button>
        </div>
      )}
    </div>
  )
}

/**
 * The next-best verified clinics Care Routing compared before choosing.
 * Renders only what the backend states: name, address, its travel estimate and
 * the estimate's source, and a CHAS tag only when the lookup verified it.
 * Open status and hours are deliberately not shown unless known, because the
 * public directory does not verify them (they arrive as "unknown").
 */
function AlternativeClinics({ clinics }) {
  return (
    <section className={styles.altBlock} aria-labelledby="alt-clinics-heading">
      <div id="alt-clinics-heading" className="eyebrow">Other nearby options</div>
      <ul className={styles.altList}>
        {clinics.map((clinic) => {
          const hasTravel = typeof clinic.travel_time_min === 'number'
          const source = clinic.travel_estimate_source === 'onemap_route' ? 'OneMap' : 'approx.'
          return (
            <li key={clinic.clinic_id || clinic.name} className={styles.altItem}>
              <div className={styles.altName}>
                <span>{clinic.name}</span>
                {clinic.affordability_match === 'chas_eligible' && <span className={styles.altTag}>CHAS</span>}
              </div>
              <div className={styles.altMeta}>
                {clinic.address}
                {hasTravel && ` · ${clinic.travel_time_min} min (${source})`}
                {clinic.open_status && clinic.open_status !== 'unknown' && ` · ${clinic.open_status}`}
              </div>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

function Stat({ label, value }) {
  return (
    <div className={styles.tile}>
      <div className={styles.statLabel}>{label}</div>
      <div className={styles.statValue}>{value}</div>
    </div>
  )
}

// Signed feature-contribution bars (a SHAP-style local explanation). Bars grow
// right from a centre axis for features pushing toward MORE urgent (coral), and
// left for features that are reassuring / push toward less urgent (pine).
// [Responsible-AI] What the bars ARE, by provenance. Real SHAP values, an LLM's
// self-reported factors and a keyword surrogate are three different kinds of
// evidence; rendering them identically would let a guess borrow SHAP's authority.
const EXPLANATION_SOURCE_LABEL = {
  shap: 'SHAP values from the trained model',
  llm: 'factors self-reported by the language model (secondary path)',
  keyword: 'deterministic keyword surrogate (fallback path)',
  simulation: 'in-browser simulation — backend unreachable, not a model output',
}

function ExplanationBars({ items, source }) {
  const max = Math.max(0.001, ...items.map((i) => Math.abs(i.weight || 0)))
  const sourceLabel = EXPLANATION_SOURCE_LABEL[source] || 'source not reported'
  return (
    <div className={styles.explBlock}>
      <div className="eyebrow">Why this acuity · feature contributions</div>
      <div className={styles.explSource} data-source={source || 'unknown'}>{sourceLabel}</div>
      <div className={styles.explList}>
        {items.map((it, i) => {
          const w = it.weight || 0
          const pos = w >= 0
          const pctW = (Math.abs(w) / max) * 50 // half-width max, from centre
          return (
            <div key={i} className={styles.explRow}>
              <div className={styles.explFeature} title={it.feature}>
                {it.feature}
              </div>
              <div className={styles.explBarWrap}>
                {/* centre axis */}
                <div className={styles.explAxis} />
                <motion.div
                  initial={{ width: 0 }}
                  animate={{ width: `${pctW}%` }}
                  transition={{ duration: 0.6, ease: 'easeOut' }}
                  className={styles.explBar}
                  style={{
                    background: pos ? '#E5533B' : '#3E7A5E',
                    left: pos ? '50%' : undefined,
                    right: pos ? undefined : '50%',
                  }}
                />
              </div>
              <div className={styles.explValue} style={{ color: pos ? '#C0392B' : '#3E7A5E' }}>
                {w >= 0 ? '+' : ''}{w.toFixed(2)}
              </div>
            </div>
          )
        })}
      </div>
      <p className={styles.explFootnote}>→ toward more urgent · ← reassuring</p>
    </div>
  )
}
