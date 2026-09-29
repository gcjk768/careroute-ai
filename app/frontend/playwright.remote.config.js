// Run the e2e specs against a DEPLOYED stack instead of a local build:
//
//   CAREROUTE_BASE_URL=http://<alb> CAREROUTE_API_URL=http://<alb> CAREROUTE_SOAK=1 \
//     npx playwright test interview_soak --config playwright.remote.config.js
//
// Same browser/project settings as playwright.config.js, minus the local webServer.
import base from './playwright.config.js'

const remote = {
  ...base,
  webServer: undefined,
  use: { ...base.use, baseURL: process.env.CAREROUTE_BASE_URL },
}

export default remote
