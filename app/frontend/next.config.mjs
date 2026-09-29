/** @type {import('next').NextConfig} */

// The frontend talks to the FastAPI backend, which serves everything under /api.
// We proxy /api through the Next server so the browser always makes a same-origin
// call (SSE-friendly, no CORS). In dev the backend is on localhost:8000; in Docker
// it's the `backend` service — set BACKEND_URL AT BUILD TIME.
//
// BUILD TIME, not runtime: `rewrites()` is evaluated by `next build` and its
// destination is frozen into .next/routes-manifest.json (check the file — the
// literal URL is in there). Setting BACKEND_URL when the server STARTS has no
// effect on an already-built app. frontend/Dockerfile therefore takes it as an
// ARG in the build stage and docker-compose.yml passes it under `build.args`.
const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000'

const nextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle so the production image stays small.
  output: 'standalone',
  async rewrites() {
    // `fallback`, not the plain array form. A plain array is `afterFiles`,
    // which Next applies BEFORE dynamic routes — and the staff proxy at
    // app/api/escalations/[[...path]]/route.js is a dynamic route, so the
    // rewrite would swallow /api/escalations* and the server-side staff key
    // would never be attached. `fallback` rewrites run only when no page or
    // route handler matched, so the proxy wins for its paths and everything
    // else (/api/triage/stream, /api/health, /api/fairness) still goes straight
    // to the backend exactly as before.
    return {
      fallback: [
        {
          source: '/api/:path*',
          destination: `${BACKEND_URL}/api/:path*`,
        },
      ],
    }
  },
}

export default nextConfig
