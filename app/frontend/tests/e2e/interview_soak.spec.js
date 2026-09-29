// 100-scenario soak of the clarifying interview, through the real UI against the
// real backend. Opt-in (~15 min rules-only, longer with an LLM):
//
//   CAREROUTE_SOAK=1 npx playwright test interview_soak
//
// Invariants, not exact acuities (an LLM is non-deterministic):
//   every case  served by the backend, ends in a decision within the 4-question
//               budget, never repeats a question
//   red flag    decided on the first turn, never asked, P1/P2
//   vague       asked at least one question before deciding
//   adversarial may instead be BLOCKED by the input guardrail (a correct outcome)
// One JSON line per scenario is APPENDED to test-results/interview-soak.jsonl as it
// finishes (delete the file before a fresh run). Appending, not an afterAll dump:
// Playwright restarts the worker after a failure, which resets module state.
import { appendFileSync, mkdirSync, readFileSync } from 'node:fs'
import { expect, test } from '@playwright/test'

const BUDGET = 4
// The 100 scenarios live in fixtures/soak_scenarios.json, shared with the LangGraph
// run (backend/scripts/graph_soak.py) so both exercise exactly the same cases.
const SCENARIOS = JSON.parse(readFileSync('tests/e2e/fixtures/soak_scenarios.json', 'utf8'))
const ANSWERS = ['No', 'Not sure', 'Yes', 'free']
const FREE_TEXT = 'It started yesterday, it is mild, nothing else.'

test.skip(!process.env.CAREROUTE_SOAK, 'soak run is opt-in: CAREROUTE_SOAK=1')
test.describe.configure({ timeout: 300_000 })

test.beforeAll(async ({ request }) => {
  // Against AWS: CAREROUTE_API_URL=<alb> with playwright.remote.config.js.
  const res = await request.get(`${process.env.CAREROUTE_API_URL ?? 'http://127.0.0.1:8000'}/api/health`)
  expect(res.ok(), 'backend /api/health did not return 2xx').toBeTruthy()
})

function record(row) {
  mkdirSync('test-results', { recursive: true })
  appendFileSync('test-results/interview-soak.jsonl', JSON.stringify(row) + '\n')
}

function finalEvent(body) {
  const frames = body.split('\n').filter((l) => l.startsWith('data:')).map((l) => JSON.parse(l.slice(5)))
  return frames.findLast((f) => f.event === 'final')
}

SCENARIOS.forEach(({ kind, text }, i) => {
  test(`#${String(i + 1).padStart(3, '0')} ${kind}: ${text.slice(0, 50)}`, async ({ page }) => {
    const finals = []
    page.on('response', async (res) => {
      if (res.url().includes('/triage/stream')) finals.push(finalEvent(await res.text().catch(() => '')))
    })
    const strategy = ANSWERS[i % ANSWERS.length]
    const asked = []
    const started = Date.now()

    // networkidle = hydrated: text typed before React hydrates is reset and
    // "Run triage" stays disabled (seen once in 100 runs).
    await page.goto('/', { waitUntil: 'networkidle' })
    await page.getByLabel('Your symptoms').fill(text)
    await expect(page.getByRole('button', { name: 'Run triage' })).toBeEnabled()
    await page.getByRole('button', { name: 'Run triage' }).click()

    const decided = page.getByText(/^case /)
    const pending = page.getByTestId('pending-question')
    const blocked = page.getByRole('alert').filter({ hasText: /was blocked/i })
    for (let turn = 1; ; turn += 1) {
      await expect(decided.or(pending).or(blocked)).toBeVisible({ timeout: 120_000 })
      if (await blocked.count()) {
        expect(kind, 'only adversarial input may be blocked').toBe('adversarial')
        record({ i: i + 1, kind, text, strategy, questions: asked, outcome: 'blocked', ms: Date.now() - started })
        return
      }
      if (await decided.count()) break
      expect(turn, 'interview exceeded its question budget').toBeLessThanOrEqual(BUDGET)
      const q = (await pending.innerText()).trim()
      expect(asked, `question repeated: ${q}`).not.toContain(q)
      asked.push(q)
      if (strategy === 'free') {
        await page.getByLabel('Your answer').fill(FREE_TEXT)
        await page.getByRole('button', { name: 'Send' }).click()
      } else {
        await page.getByRole('button', { name: strategy, exact: true }).click()
      }
    }
    await expect(page.getByText(/backend offline/i)).toHaveCount(0)
    await expect.poll(() => finals.filter(Boolean).length, { timeout: 15_000 }).toBe(asked.length + 1)
    const last = finals.filter(Boolean).at(-1)

    record({
      i: i + 1, kind, text, strategy, questions: asked, outcome: 'decided',
      acuity: last.acuity?.code, confidence: last.confidence, escalated: last.escalated,
      escalationReason: last.escalationReason, careTier: last.careTier, ms: Date.now() - started,
    })

    if (kind === 'redflag') {
      expect(asked, 'a red-flag case must never be asked').toHaveLength(0)
      expect(last.acuity?.code).toMatch(/^P[12]/)
    }
    if (kind === 'vague') expect(asked.length, 'a vague complaint must be interviewed').toBeGreaterThan(0)
  })
})
