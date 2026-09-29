// The clarifying interview, driven through the real UI against the real backend.
//
// A vague complaint must be ASKED a question (the chat thread appears with an
// answer box), an answer must lead to either another question or a decision,
// and a red-flag complaint must never be asked anything. Same anti-simulation
// guard as agents.spec.js: the run must be served by the backend.
import { expect, test } from '@playwright/test'

const SIM_BANNER = /backend offline/i
const VAGUE_TEXT = 'I feel a bit unwell and off today, nothing specific.'
const CARDIAC_TEXT = 'crushing chest pain radiating to my left arm with breathlessness since this morning'

test.beforeAll(async ({ request }) => {
  const res = await request.get('http://127.0.0.1:8000/api/health')
  expect(res.ok(), 'backend /api/health did not return 2xx').toBeTruthy()
})

async function submitComplaint(page, text) {
  await page.goto('/')
  await page.getByLabel('Your symptoms').fill(text)
  await page.getByRole('button', { name: 'Run triage' }).click()
}

test.describe('Clarifying interview (chat)', () => {
  test('a vague complaint is asked a question and can be answered in the thread', async ({ page }) => {
    await submitComplaint(page, VAGUE_TEXT)

    const thread = page.getByRole('region', { name: 'Clarifying questions' })
    await expect(thread).toBeVisible({ timeout: 90_000 })
    await expect(page.getByText(SIM_BANNER)).toHaveCount(0)

    // The first question of a vague complaint is the symptom screen, in the thread, with quick replies.
    const question = page.getByTestId('pending-question')
    await expect(question).toBeVisible({ timeout: 90_000 })
    await expect(question).toContainText(/\?$/)
    await expect(page.getByText(/Question 1 of \d/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Not sure' })).toBeVisible()
    // No recommendation while a question is open.
    await expect(page.getByText(/^case /)).toHaveCount(0)

    // Answer "No": the thread keeps the exchange and the pipeline runs again.
    await page.getByRole('button', { name: 'No', exact: true }).click()
    await expect(thread.getByText('No', { exact: true })).toBeVisible()

    // Either a different next question or the decision, within the budget.
    for (let turn = 2; turn <= 5; turn += 1) {
      const decided = page.getByText(/^case /)
      const next = page.getByTestId('pending-question')
      await expect(decided.or(next)).toBeVisible({ timeout: 90_000 })
      if (await decided.count()) break
      await expect(page.getByText(new RegExp(`Question ${turn} of \\d`))).toBeVisible()
      await page.getByLabel('Your answer').fill('not sure')
      await page.getByRole('button', { name: 'Send' }).click()
    }
    await expect(page.getByText(/^case /)).toBeVisible({ timeout: 90_000 })
  })

  test('a red-flag complaint is never asked anything', async ({ page }) => {
    await submitComplaint(page, CARDIAC_TEXT)
    await expect(page.getByText(/^case /)).toBeVisible({ timeout: 90_000 })
    await expect(page.getByRole('region', { name: 'Clarifying questions' })).toHaveCount(0)
    await expect(page.getByText(/P1|P2|Emergency/i).first()).toBeVisible()
  })
})
