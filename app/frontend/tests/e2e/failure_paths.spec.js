// Failure-path contract for the triage stream, the routing explanation, and the
// clinician decision form.
//
// WHY THIS FILE EXISTS
// --------------------
// `lib/api.js` degrades to an in-browser SIMULATION when the backend is
// unreachable. That fallback used to be reached by ANY failure — an HTTP 429
// from the rate limiter, a 422 from validation, even a connection dropped
// half-way through a real run — so a patient could be shown a fabricated
// clinical recommendation (captioned only "backend offline") in situations
// where the service had actually refused or aborted the request.
//
// The rule this file pins down:
//
//   simulate ONLY on a genuine network failure with zero real frames delivered.
//   Every other failure surfaces an error state — never a result, never the
//   idle "Awaiting intake" card, and never a simulated run pasted on top of a
//   partial real one.
//
// Like `routing_ui.spec.js` these are deterministic UI contract tests: the
// stream is mocked, so they assert what the UI does with a given server
// behaviour, not that the backend produces it.
import { expect, test } from '@playwright/test'

const SYMPTOMS = 'Mild sore throat and a runny nose for two days, no fever.'
const SIM_BANNER = /backend offline/i

const sse = (payload) => `data: ${JSON.stringify(payload)}\n\n`

/** Answer POST /api/triage/stream with an HTTP error instead of a stream. */
async function mockHttpFailure(page, { status, body, contentType = 'application/json' }) {
  await page.route('**/api/triage/stream', async (route) => {
    await route.fulfill({
      status,
      headers: { 'content-type': contentType },
      body: typeof body === 'string' ? body : JSON.stringify(body),
    })
  })
}

/** Replay a well-formed SSE body (a complete response). */
async function mockStreamBody(page, frames) {
  await page.route('**/api/triage/stream', async (route) => {
    await route.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache, no-transform' },
      body: frames.map(sse).join(''),
    })
  })
}

async function runTriage(page) {
  await page.goto('/')
  await page.getByLabel('Your symptoms').fill(SYMPTOMS)
  await page.getByRole('button', { name: 'Run triage' }).click()
}

// Next.js mounts a permanent, empty role="alert" route announcer on every page
// (#__next-route-announcer__), so `getByRole('alert')` is never unique. This is
// the app's own alerts only.
const appAlerts = (page) => page.locator('[role="alert"]:not(#__next-route-announcer__)')

// The patient shell adds a second standing alert (the 995 emergency banner), so
// the triage error block is addressed by its heading too.
const triageError = (page) => appAlerts(page).filter({ hasText: /triage could not complete/i })

/** No simulated run may be rendered, in any shape. */
async function expectNoSimulation(page) {
  await expect(page.getByText(SIM_BANNER), 'the in-browser simulation ran').toHaveCount(0)
  await expect(page.getByText(/^case SIM-/), 'a simulated case id was rendered').toHaveCount(0)
}

test.describe('Triage stream — HTTP errors are surfaced, never simulated', () => {
  test('a 429 from the rate limiter shows a wait-and-retry error', async ({ page }) => {
    await mockHttpFailure(page, { status: 429, body: { detail: 'Rate limit exceeded — please wait and retry.' } })
    await runTriage(page)

    await expect(triageError(page)).toContainText(/too many requests/i)
    await expectNoSimulation(page)
    // Neither a result nor a fallback to the idle card.
    await expect(page.getByText('Awaiting intake')).toHaveCount(0)
    await expect(page.getByText(/^case /), 'a recommendation was rendered for a refused request').toHaveCount(0)
    // The run must not be left stuck in the "Triaging…" state.
    await expect(page.getByRole('button', { name: 'Run triage' })).toBeEnabled()
  })

  test('a 422 surfaces the backend own validation detail', async ({ page }) => {
    await mockHttpFailure(page, {
      status: 422,
      body: { detail: [{ loc: ['body', 'text'], msg: 'Symptom text must not be empty', type: 'value_error' }] },
    })
    await runTriage(page)

    await expect(triageError(page)).toContainText('Symptom text must not be empty')
    await expectNoSimulation(page)
  })

  test('a 5xx shows a generic service error', async ({ page }) => {
    await mockHttpFailure(page, { status: 500, body: { detail: 'Internal Server Error' } })
    await runTriage(page)

    await expect(triageError(page)).toContainText(/the triage service returned an error/i)
    await expectNoSimulation(page)
  })
})

test.describe('Triage stream — a mid-stream drop never replays a simulation', () => {
  // page.route() can only fulfil a COMPLETE response, so it cannot express
  // "connection died after N frames". Patching window.fetch is the only way to
  // hand the app a real ReadableStream that errors part-way — which is exactly
  // the shape lib/api.js has to handle. The frames below are delivered from
  // `pull`, so each one is provably consumed before the stream errors.
  async function mockDroppedStream(page, frames) {
    await page.addInitScript((payloads) => {
      const realFetch = window.fetch.bind(window)
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : input?.url ?? ''
        if (!url.includes('/api/triage/stream')) return realFetch(input, init)
        const encoder = new TextEncoder()
        let i = 0
        const body = new ReadableStream({
          pull(controller) {
            if (i < payloads.length) {
              controller.enqueue(encoder.encode(`data: ${JSON.stringify(payloads[i++])}\n\n`))
              return
            }
            controller.error(new TypeError('Failed to fetch'))
          },
        })
        return Promise.resolve(
          new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
        )
      }
    }, frames)
  }

  test('keeps the partial real run and reports the interruption', async ({ page }) => {
    await mockDroppedStream(page, [
      { event: 'case_open', caseId: 'case_dropped_0001' },
      { event: 'agent_active', agent: 'intake', label: 'Normalising symptoms' },
      { event: 'agent_result', agent: 'intake', label: 'Symptom-Intake', summary: 'Structured intake record created.', data: {} },
      { event: 'agent_active', agent: 'classifier', label: 'Estimating acuity' },
    ])
    await runTriage(page)

    await expect(triageError(page)).toContainText(/interrupted/i)
    // The whole point: real partial output survives and is not overwritten.
    await expectNoSimulation(page)
    // Exactly one agent completed before the drop; a replayed simulation would
    // finish the whole pipeline and render a final recommendation on top of it.
    await expect(page.getByText('✓'), 'the partial real run was extended or replaced').toHaveCount(1)
    await expect(page.getByText(/^case /), 'a recommendation appeared after a dropped stream').toHaveCount(0)
    await expect(page.getByText('Awaiting intake')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Run triage' })).toBeEnabled()
  })
})

test.describe('Triage stream — a backend error event is surfaced', () => {
  test('a guardrail block shows the reason instead of resetting to idle', async ({ page }) => {
    await mockStreamBody(page, [
      { event: 'case_open', caseId: 'case_guardrail_0001' },
      { event: 'guardrail', status: 'blocked', detail: 'Input blocked: prompt-injection pattern detected.' },
      { event: 'error', message: 'Input blocked: prompt-injection pattern detected.' },
    ])
    await runTriage(page)

    await expect(triageError(page)).toContainText('prompt-injection pattern detected')
    await expect(page.getByText('Awaiting intake')).toHaveCount(0)
    await expectNoSimulation(page)
  })

  test('reset clears the error and returns the column to idle', async ({ page }) => {
    await mockStreamBody(page, [
      { event: 'case_open', caseId: 'case_guardrail_0002' },
      { event: 'error', message: 'Input blocked: prompt-injection pattern detected.' },
    ])
    await runTriage(page)
    await expect(triageError(page)).toBeVisible()

    await page.getByRole('button', { name: 'Reset' }).click()
    await expect(triageError(page)).toHaveCount(0)
    await expect(page.getByText('Awaiting intake')).toBeVisible()
  })
})

test.describe('Care-Routing explanation (routingReason / routingClarification)', () => {
  const BASE_FINAL = {
    event: 'final',
    caseId: 'case_reason_0001',
    acuity: { code: 'P4_NON_URGENT', label: 'Non-urgent', score: 4 },
    careTier: 'GP',
    confidence: 0.8,
    escalated: false,
    rationale: 'Routed to a GP.',
    citations: [],
    explanation: [],
    waitTimeMin: null,
    clinic: null,
    travelEstimateSource: 'unavailable',
    routeAvailable: false,
    routeInstructions: [],
    routeGeometry: [],
    routingReason: null,
    routingClarification: null,
    alternativeClinics: [],
    oneMapUrl: null,
  }

  test('renders a routingReason the UI has no hard-coded sentence for', async ({ page }) => {
    const reason = "The supplied location is outside OneMap's Singapore coverage."
    await mockStreamBody(page, [
      { event: 'case_open', caseId: BASE_FINAL.caseId },
      { ...BASE_FINAL, routingReason: reason },
    ])
    await runTriage(page)

    await expect(page.getByText(`case ${BASE_FINAL.caseId}`)).toBeVisible()
    await expect(page.getByText(reason)).toBeVisible()
  })

  test('renders the structured clarification question and keeps the location call-to-action', async ({ page }) => {
    await mockStreamBody(page, [
      { event: 'case_open', caseId: 'case_reason_0002' },
      {
        ...BASE_FINAL,
        caseId: 'case_reason_0002',
        routingReason: 'Location is required to find nearby clinics.',
        routingClarification: {
          kind: 'location',
          question: 'May we use your location to find nearby verified clinics and directions?',
          required_for: 'nearby_clinic_routing',
        },
      },
    ])
    await runTriage(page)

    await expect(page.getByText('case case_reason_0002')).toBeVisible()
    await expect(page.getByText('May we use your location to find nearby verified clinics')).toBeVisible()
    await expect(page.getByText('Find a nearby GP')).toBeVisible()
  })

  test('a transport-mode clarification is shown without the location call-to-action', async ({ page }) => {
    await mockStreamBody(page, [
      { event: 'case_open', caseId: 'case_reason_0003' },
      {
        ...BASE_FINAL,
        caseId: 'case_reason_0003',
        routingReason: 'Please choose how you will travel before requesting a route.',
        routingClarification: {
          kind: 'transport_mode',
          question: 'How will you travel to the clinic?',
          options: ['walk', 'cycle', 'public', 'drive', 'taxi'],
          required_for: 'route_estimate',
        },
      },
    ])
    await runTriage(page)

    await expect(page.getByText('How will you travel to the clinic?')).toBeVisible()
    await expect(page.getByText('Please choose how you will travel before requesting a route.')).toBeVisible()
    await expect(page.getByText('Find a nearby GP')).toHaveCount(0)
  })
})

test.describe('Clinician decision — a failed POST must not look recorded', () => {
  const ESCALATION = {
    id: 'ESC-9001',
    caseId: 'CR-90001',
    reason: 'Red-flag: CARDIAC_CHEST_PAIN',
    confidence: 0.97,
    acuity: { code: 'P1', label: 'Resuscitation', score: 1 },
    createdAt: new Date(Date.now() - 5 * 60000).toISOString(),
    status: 'pending',
    patientSummary: '58M — crushing chest pain radiating to left arm.',
    normalisedSymptoms: 'chest pain (crushing); diaphoresis',
    language: 'English',
    rationale: 'Deterministic red-flag forced P1.',
    evidence: ['Onset < 1h'],
    citations: [],
  }

  async function openDashboard(page, decisionStatus) {
    await page.addInitScript(() => {
      localStorage.setItem('careroute_staff', JSON.stringify({ name: 'Staff', at: Date.now() }))
    })
    // One handler for the whole API surface: Playwright matches the most
    // recently registered route first, so a single switch avoids ordering bugs.
    await page.route('**/api/**', async (route) => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/decision')) {
        await route.fulfill({
          status: decisionStatus,
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(
            decisionStatus === 200 ? { ...ESCALATION, status: 'decided' } : { detail: 'Escalation not found' },
          ),
        })
        return
      }
      if (url.pathname.endsWith('/api/escalations')) {
        await route.fulfill({ status: 200, headers: { 'content-type': 'application/json' }, body: JSON.stringify([ESCALATION]) })
        return
      }
      if (url.pathname.includes('/api/escalations/')) {
        await route.fulfill({ status: 200, headers: { 'content-type': 'application/json' }, body: JSON.stringify(ESCALATION) })
        return
      }
      await route.fulfill({ status: 200, headers: { 'content-type': 'application/json' }, body: '{}' })
    })
    await page.goto('/staff/clinician')
    await expect(page.getByText(ESCALATION.patientSummary).first()).toBeVisible()
  }

  test('a rejected decision shows an error and leaves the case pending', async ({ page }) => {
    await openDashboard(page, 404)

    await page.getByRole('button', { name: 'Submit final decision' }).click()

    await expect(appAlerts(page)).toContainText(/still pending/i)
    // The optimistic "decided" state must NOT have been applied.
    await expect(page.getByText('recorded decision')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Submit final decision' })).toBeEnabled()
  })

  test('an accepted decision is recorded', async ({ page }) => {
    await openDashboard(page, 200)

    await page.getByRole('button', { name: 'Submit final decision' }).click()

    await expect(page.getByText('recorded decision')).toBeVisible()
    await expect(appAlerts(page)).toHaveCount(0)
  })
})
