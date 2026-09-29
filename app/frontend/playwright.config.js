// Playwright config for the CareRoute frontend.
//
// WHY `webServer` STARTS ONLY THE FRONTEND
// ----------------------------------------
// The BACKEND must already be running on :8000 before these tests start, and
// that is deliberate rather than an oversight.
//
// `lib/api.js` falls back to an in-browser SIMULATION whenever the backend is
// unreachable, and renders a complete, plausible triage result from it. So an
// E2E suite that boots only the frontend would PASS with the agents entirely
// switched off — it would be asserting against hardcoded keyword rules in the
// browser, not against the agent pipeline.
//
// `tests/e2e/agents.spec.js` therefore fails loudly if the backend is absent,
// instead of quietly testing the simulation. Start the backend first:
//
//     ./dev.sh                  (backend + frontend together)
//   or
//     cd backend && .venv/Scripts/python -m uvicorn app.main:app --port 8000
//
import { defineConfig, devices } from '@playwright/test'

const FRONTEND_PORT = 5173
const BASE_URL = `http://127.0.0.1:${FRONTEND_PORT}`

// 'bundled' means "use the browser Playwright ships with" (CI). Anything else is
// treated as a channel name for an already-installed browser (local default:
// system Google Chrome). See the `projects` note below for why.
const CHANNEL = process.env.PLAYWRIGHT_CHANNEL ?? 'chrome'
const CHANNEL_OPTION = CHANNEL === 'bundled' ? {} : { channel: CHANNEL }

export default defineConfig({
  testDir: './tests/e2e',

  // The triage stream runs a real multi-agent pipeline (and may call an LLM
  // provider before falling back), so it is legitimately slower than a typical
  // page assertion. These are wall-clock budgets, not correctness thresholds.
  timeout: 120_000,
  expect: { timeout: 30_000 },

  // Fail the run rather than mask a flaky agent path behind a retry.
  retries: 0,
  fullyParallel: false,
  workers: 1,

  reporter: [['list'], ['html', { open: 'never' }]],

  use: {
    baseURL: BASE_URL,
    // Trace and screenshot are produced by the browser itself. Video is
    // deliberately NOT enabled: it needs a separate ffmpeg binary, whose
    // download fails on this TLS-intercepting network the same way the browser
    // download does. The trace viewer already gives a frame-by-frame replay.
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },

  // Which browser binary to drive, chosen by environment:
  //
  //   local (default)  channel 'chrome' -> the Google Chrome already installed
  //                    on the machine. `npx playwright install` fails on this
  //                    network with SELF_SIGNED_CERT_IN_CHAIN (a TLS-intercepting
  //                    proxy re-signs the download), and the fix for that is NOT
  //                    to disable TLS verification while fetching an executable
  //                    that then runs locally. Using system Chrome downloads
  //                    nothing at all.
  //
  //   CI               PLAYWRIGHT_CHANNEL=bundled -> no channel, so Playwright
  //                    uses the browsers baked into the
  //                    mcr.microsoft.com/playwright image. Same reason: the CI
  //                    runner must not have to download a browser either.
  projects: [
    {
      name: 'chrome',
      use: {
        ...devices['Desktop Chrome'],
        ...CHANNEL_OPTION,
        // These flags stop Chrome throttling a renderer it considers
        // backgrounded or occluded. Kept as a sensible default for a suite that
        // holds one SSE connection open for ~40s. They were once tried as THE
        // fix for three red-flag specs failing with net::ERR_NETWORK_IO_SUSPENDED
        // and did nothing (still 3 passed / 3 failed). The real cause was found
        // on 2026-09-02 -- see the webServer note below -- and is fixed in the
        // backend, not here.
        launchOptions: {
          args: [
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--disable-ipc-flooding-protection',
          ],
        },
      },
    },
  ],

  // A production build rather than `next dev`, so the suite runs the same
  // server shape the Docker image ships (one caveat below).
  //
  // HISTORY, so nobody re-investigates it: three red-flag specs failed here
  // with the UI stuck on "Triaging..." and net::ERR_NETWORK_IO_SUSPENDED in
  // the traces. Root cause, measured 2026-09-02 from inside Chrome with a
  // synthetic SSE server behind a throwaway Next rewrite: Next proxies /api
  // rewrites through http-proxy with a default 30 s idle `proxyTimeout`
  // (next/dist/server/lib/router-utils/proxy-request.js). When the backend is
  // silent for 30 s the proxy aborts the upstream request but never ends the
  // browser's response -- no data, no error, forever. 26 s silent: delivered.
  // 32 s silent: stalls. 40 s with an SSE comment every 10 s: delivered. The
  // red-flag path is quiet ~30 s on the LLM and Care Routing ~24-40 s on
  // OneMap, which is why only long streams failed and curl (which does not go
  // idle the same way through the proxy) never showed it.
  //
  // Fix: backend/app/main.py wraps the orchestrator in `_with_heartbeat`,
  // emitting `: keepalive` every CAREROUTE_SSE_HEARTBEAT_SECONDS (10 s) of
  // silence. Comment frames have no `data:` line, so lib/api.js ignores them.
  // Verified: this suite 6/6 against `next start` + the real backend.
  //
  // Remaining caveat, deliberately not papered over: next.config.mjs sets
  // `output: 'standalone'`, and Next warns that `next start` is not the right
  // server for that mode ("use node .next/standalone/server.js"). The Docker
  // image ships standalone, so the command below is close to, not identical
  // to, the production path.
  //
  // `reuseExistingServer` is false so a stale `next dev` on this port cannot
  // silently serve the suite -- that exact mix-up produced a misleading result
  // once already.
  webServer: {
    command: 'npm run build && npm run start',
    url: BASE_URL,
    reuseExistingServer: false,
    timeout: 300_000,
  },
})
