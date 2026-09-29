// Capture screenshots of the running app into docs/screenshots/.
//
// A reader should be able to see what CareRoute actually looks like without
// installing it. Run both servers first (backend :8000, frontend :5173) --
// `./dev.sh` does both -- then:
//
//   node scripts/capture-screenshots.mjs
//
// Uses the Playwright + system Chrome already configured for the frontend
// tests, so nothing is downloaded.
//
import { chromium } from '../frontend/node_modules/playwright/index.mjs'
import { mkdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const OUT = join(ROOT, 'docs', 'screenshots')
const BASE = process.env.CAREROUTE_BASE_URL ?? 'http://127.0.0.1:5173'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ channel: 'chrome' })
const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
const shot = async (name, full = true) => {
  await page.screenshot({ path: join(OUT, `${name}.png`), fullPage: full })
  console.log(`  ok   ${name}.png`)
}
const settle = (ms) => page.waitForTimeout(ms)

try {
  // 1. The patient page, before anything is entered.
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await settle(1500)
  await shot('patient-intake')

  // 2. A real triage run. Red-flag case, so it escalates and shows the full result.
  const box = page.locator('textarea').first()
  await box.fill('sudden weakness on one side and slurred speech')
  for (const [label, value] of [[/age/i, '65+'], [/sex|gender/i, 'F']]) {
    const sel = page.locator('select').filter({ hasText: '' })
    try { await sel.first().selectOption({ label: value }) } catch { /* optional field */ }
    void label
  }
  await page.getByRole('button', { name: /triage|assess|submit|check/i }).first().click()
  // The pipeline streams over SSE. Do NOT wait on text like "995" or "Emergency" --
  // the persistent emergency banner contains both, so it matches instantly and
  // screenshots a half-finished run. Wait for the in-progress state to clear.
  await page.waitForFunction(
    () => !/Triaging|RUNNING/i.test(document.body.innerText),
    { timeout: 180_000 },
  )
  await settle(2500)
  await shot('patient-result')

  // 3. The staff portal. The gate is a DEMO login -- the page itself says
  // "demo login · any name". There is no password and no real authentication,
  // so entering a name is the intended demo path, not a bypass.
  await page.goto(BASE + '/staff/login', { waitUntil: 'networkidle' })
  await settle(1200)
  await shot('staff-login')

  await page.locator('input[type="text"], input:not([type])').first().fill('Dr. A. Rahman')
  await page.getByRole('button', { name: /enter staff portal/i }).click()
  await page.waitForURL(/\/staff(?!\/login)/, { timeout: 30_000 })
  await settle(2500)
  await shot('staff-portal')

  // The staff surfaces are separate ROUTES reached by links, not tabs within /staff.
  // The clinician queue only has content because the triage run above escalated.
  for (const [path, name] of [
    ['/staff/clinician', 'staff-clinician'],
    ['/staff/governance', 'staff-governance'],
  ]) {
    try {
      await page.goto(BASE + path, { waitUntil: 'networkidle' })
      await settle(4000)          // governance computes a live fairness audit
      await shot(name)
    } catch (e) { console.log(`  skip ${name}: ${String(e.message).split('\n')[0].slice(0, 70)}`) }
  }
} catch (e) {
  console.error('FAILED:', String(e.message).split('\n')[0].slice(0, 200))
  await shot('failure-state')
  process.exitCode = 1
} finally {
  await browser.close()
}
