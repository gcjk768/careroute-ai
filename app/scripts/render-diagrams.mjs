// Render every Mermaid source in docs/diagrams/src/*.mmd to a PNG in docs/diagrams/generated/.
//
// WHY THIS EXISTS
// ---------------
// GitLab draws at most ~2000 characters of Mermaid per page. Past that it stops
// rendering and shows "Displaying this diagram might cause performance issues on
// this page" (measured: a README with 6284 chars across 6 blocks triggered it and
// most diagrams never drew). Images have no such budget, so the README can carry
// every diagram as a PNG while the Mermaid source stays in this repo as the
// editable original.
//
// Uses the Playwright + system Chrome already configured for frontend/, so nothing
// is downloaded. Install the renderer's deps in ONE command -- `--no-save` prunes
// anything not listed, so installing them separately removes the previous one:
//
//   cd frontend && npm install --no-save mermaid @iconify-json/logos @iconify-json/mdi && cd ..
//   node scripts/render-diagrams.mjs
//
import { chromium } from '../frontend/node_modules/playwright/index.mjs'
import { readFileSync, writeFileSync, mkdirSync, readdirSync, statSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const OUT = join(ROOT, 'docs', 'diagrams', 'generated')
const MERMAID = join(ROOT, 'frontend', 'node_modules', 'mermaid', 'dist', 'mermaid.min.js')

// Sources are standalone .mmd files, NOT fenced blocks inside the Markdown. Two reasons:
//   1. GitLab renders at most ~2000 chars of Mermaid per page, and the icon syntax is verbose
//      enough that a page with two diagrams blows the budget and stops drawing.
//   2. The icons come from Iconify packs this script registers; GitLab's own renderer has none,
//      so an in-page block would draw the diagram WITHOUT its icons anyway.
// The Markdown embeds the generated PNG and links back to the .mmd.
const SRC_DIR = join(ROOT, 'docs', 'diagrams', 'src')

function collect() {
  return readdirSync(SRC_DIR)
    .filter((f) => f.endsWith('.mmd'))
    .sort()
    .map((f) => ({
      file: `docs/diagrams/src/${f}`,
      name: f.replace(/\.mmd$/, ''),
      code: readFileSync(join(SRC_DIR, f), 'utf8'),
    }))
}

const jobs = collect()
if (!jobs.length) { console.error('no .mmd sources found in docs/diagrams/src/'); process.exit(1) }
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ channel: 'chrome', args: ['--allow-file-access-from-files'] })
const page = await browser.newPage({ deviceScaleFactor: 3 })
await page.goto(pathToFileURL(join(ROOT, 'docs', 'diagrams')).href) // a real origin, so file:// scripts load
await page.addScriptTag({ path: MERMAID })

// Icon packs. Mermaid's `@{ icon: "logos:gitlab" }` shapes need the pack registered
// up front; we pass the Iconify JSON in from node_modules so nothing is fetched at
// render time. Icons make the generated PNGs readable at a glance — note they are a
// property of THIS renderer, so a Mermaid block relying on them will not draw icons
// in GitLab's built-in renderer.
// simple-icons carries the official brand marks (mlflow, dvc, pytest, ruff,
// trivy, owasp, scikitlearn...) that 'logos' does not.
const ICONS = Object.fromEntries(['logos', 'mdi', 'simple-icons'].map((p) => [
  p, JSON.parse(readFileSync(join(ROOT, 'frontend', 'node_modules', '@iconify-json', p, 'icons.json'), 'utf8')),
]))
await page.evaluate((packs) => {
  window.mermaid.registerIconPacks(
    Object.entries(packs).map(([name, icons]) => ({ name, loader: () => icons })),
  )
  window.mermaid.initialize({
    startOnLoad: false, theme: 'default',
    flowchart: { useMaxWidth: false, wrappingWidth: 700 }, sequence: { useMaxWidth: false },
  })
}, ICONS)

let ok = 0, failed = []
for (const job of jobs) {
  try {
    await page.evaluate(async (code) => {
      document.body.innerHTML = '<div id="t" style="display:inline-block;background:#fff;padding:20px"></div>'
      const { svg } = await window.mermaid.render('m' + Date.now(), code)
      document.getElementById('t').innerHTML = svg
    }, job.code)
    await page.locator('#t').screenshot({ path: join(OUT, job.name + '.png') })
    // Icon shapes place the label BELOW the node, so a top-down flowchart turns into a
    // very tall, very narrow strip that reads badly in a README. Flag it rather than
    // leave it to be noticed on the rendered page.
    const box = await page.locator('#t').boundingBox()
    const ratio = box.height / box.width
    const warn = ratio > 2.5 ? '  <-- very tall, consider `flowchart LR`' : ''
    console.log(`  ok   ${job.name}.png   ${Math.round(box.width)}x${Math.round(box.height)}${warn}`)
    ok++
  } catch (e) {
    console.log(`  FAIL ${job.name}: ${String(e.message).split('\n')[0].slice(0, 120)}`)
    failed.push(job.name)
  }
}
await browser.close()
console.log(`\n${ok}/${jobs.length} rendered into docs/diagrams/generated/`)
if (failed.length) { console.error('failed: ' + failed.join(', ')); process.exit(1) }
