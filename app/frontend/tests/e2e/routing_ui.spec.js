// Care-Routing result panel: what the UI shows for the logistics fields the
// backend returns (route, replan, alternative clinics).
//
// WHY THIS FILE MOCKS THE STREAM
// ------------------------------
// The real P4 clinic path needs a Singapore location, a transport mode and a
// live OneMap account; CI has none of those, so `agents.spec.js` (which always
// hits the real backend) cannot reach these fields. This spec is a UI contract
// test instead: it intercepts /api/triage/stream and replays a recorded final
// event, exactly the shape `app/main.py` emits, so the rendering is verified
// deterministically. It is NOT evidence that the routing agent works — that is
// backend/tests/agents/test_routing*.py.
import { expect, test } from '@playwright/test'

const SYMPTOMS = 'A slight itchy rash appeared this morning after using a new soap. I otherwise feel well.'

/** A final event trimmed from a real 2026-09-02 run (public transport → verified walking replan). */
const FINAL_EVENT = {
  event: 'final',
  caseId: 'case_ui_contract_0001',
  acuity: { code: 'P4_NON_URGENT', label: 'Non-urgent', score: 4 },
  careTier: 'GP',
  confidence: 0.82,
  escalated: false,
  escalationReason: null,
  rationale: 'Routed to GP (FINEST HEALTH MEDICAL CENTRE), estimated wait 2 min.',
  citations: [],
  explanation: [],
  waitTimeMin: 2,
  clinic: 'FINEST HEALTH MEDICAL CENTRE',
  clinicLatitude: 1.33462,
  clinicLongitude: 103.85652,
  travelEstimateSource: 'onemap_route',
  routeAvailable: true,
  routeInstructions: ['Walk to FINEST HEALTH MEDICAL CENTRE: about 120 m (2 min).'],
  routeGeometry: [],
  transportMode: 'walk',
  requestedTransportMode: 'public',
  routingReason: null,
  routingClarification: null,
  routingPlan: {
    requested_transport: 'public',
    effective_transport: 'walk',
    replanned: true,
    steps: ['No verified public-transport itinerary was available.'],
    outcome: 'verified_nearby_walking_alternative',
  },
  alternativeClinics: [
    {
      clinic_id: 'chas-310018-oei-kho-clinic-and-surgery',
      name: 'OEI & KHO CLINIC AND SURGERY',
      address: '18 LORONG 7 TOA PAYOH #01-250',
      travel_time_min: 3,
      travel_estimate_source: 'onemap_route',
      open_status: 'unknown',
      hours_freshness: 'unknown',
      affordability_match: 'chas_eligible',
    },
    {
      clinic_id: 'chas-310047-teoh-clinic-family-practice',
      name: 'TEOH CLINIC FAMILY PRACTICE',
      address: '47 LORONG 6 TOA PAYOH #01-142',
      travel_time_min: 6,
      travel_estimate_source: 'geodesic_speed_estimate',
      open_status: 'unknown',
      hours_freshness: 'unknown',
      affordability_match: 'not_verified',
    },
  ],
  oneMapUrl: null,
}

const sse = (payload) => `data: ${JSON.stringify(payload)}\n\n`

/** Replay a minimal but well-formed stream ending in `finalEvent`. */
async function mockTriageStream(page, finalEvent) {
  await page.route('**/api/triage/stream', async (route) => {
    const body =
      sse({ event: 'case_open', caseId: finalEvent.caseId }) +
      sse({ event: 'agent_active', agent: 'routing', label: 'Care Routing' }) +
      sse({ event: 'agent_result', agent: 'routing', label: 'Care Routing', summary: 'Routed to GP', data: {} }) +
      sse(finalEvent)
    await route.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache, no-transform' },
      body,
    })
  })
}

async function runMockedTriage(page, finalEvent) {
  await mockTriageStream(page, finalEvent)
  await page.goto('/')
  await page.getByLabel('Your symptoms').fill(SYMPTOMS)
  await page.getByRole('button', { name: 'Run triage' }).click()
  await expect(page.getByText(`case ${finalEvent.caseId}`)).toBeVisible({ timeout: 30_000 })
}

test.describe('Care-Routing result panel (UI contract, mocked stream)', () => {
  test('lists the alternative clinics the backend returned, with travel time and CHAS tag', async ({ page }) => {
    await runMockedTriage(page, FINAL_EVENT)

    const panel = page.getByRole('region', { name: /other nearby options/i })
    await expect(panel).toBeVisible()

    const items = panel.getByRole('listitem')
    await expect(items).toHaveCount(2)

    await expect(items.nth(0)).toContainText('OEI & KHO CLINIC AND SURGERY')
    await expect(items.nth(0)).toContainText('18 LORONG 7 TOA PAYOH #01-250')
    await expect(items.nth(0)).toContainText('3 min')
    await expect(items.nth(0)).toContainText(/OneMap/i)
    await expect(items.nth(0)).toContainText(/CHAS/)

    await expect(items.nth(1)).toContainText('TEOH CLINIC FAMILY PRACTICE')
    await expect(items.nth(1)).toContainText('6 min')
    await expect(items.nth(1)).toContainText(/approx/i)
    // Not CHAS-verified → no CHAS tag; the UI must not invent eligibility.
    await expect(items.nth(1)).not.toContainText(/CHAS/)
  })

  test('shows no alternatives panel when the backend returned none', async ({ page }) => {
    await runMockedTriage(page, { ...FINAL_EVENT, caseId: 'case_ui_contract_0002', alternativeClinics: [] })
    await expect(page.getByRole('region', { name: /other nearby options/i })).toHaveCount(0)
  })

  test('explains a public-transport replan next to the selected clinic', async ({ page }) => {
    await runMockedTriage(page, { ...FINAL_EVENT, caseId: 'case_ui_contract_0003' })
    await expect(page.getByText(/Public transport was unavailable/)).toBeVisible()
    await expect(page.getByText('FINEST HEALTH MEDICAL CENTRE').first()).toBeVisible()
  })
})
