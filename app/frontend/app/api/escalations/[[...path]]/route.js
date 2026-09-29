// Server-side proxy for the STAFF escalation endpoints.
//
// The backend now guards /api/escalations* behind a shared key
// (CAREROUTE_STAFF_API_KEY, header X-Staff-Key — see backend/app/config.py).
// The key must never reach the browser, so this Route Handler attaches it on
// the server and forwards the request. The browser keeps calling the same
// /api/escalations URLs as before: Next resolves app routes BEFORE the
// `rewrites()` proxy in next.config.mjs, so only these paths come here and
// everything else (triage stream, health, fairness) still goes straight to
// the backend via the rewrite.
//
// With the key unset the backend is open and this proxy simply forwards —
// so a local demo, the Playwright suite and CI need no configuration.
//
// When the key IS set, the caller must hold a staff session cookie
// (lib/staffSession.js) — otherwise this proxy would lend the key to anyone.
//
// BACKEND_URL is read at RUNTIME here (unlike the build-time rewrite), so in
// Docker it can be passed as an ordinary `environment:` entry.
import { COOKIE, validToken } from '@/lib/staffSession'

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000'

async function forward(request, { params }) {
  if (!validToken(request.cookies.get(COOKIE)?.value)) {
    return new Response(JSON.stringify({ detail: 'Staff sign-in required' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  const { path = [] } = await params
  const target = `${BACKEND_URL}/api/escalations${path.length ? '/' + path.join('/') : ''}`

  const headers = { Accept: 'application/json' }
  const key = (process.env.CAREROUTE_STAFF_API_KEY || '').trim()
  if (key) headers['X-Staff-Key'] = key
  const contentType = request.headers.get('content-type')
  if (contentType) headers['Content-Type'] = contentType

  const init = { method: request.method, headers, cache: 'no-store' }
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    init.body = await request.text()
  }

  let upstream
  try {
    upstream = await fetch(target, init)
  } catch {
    // Same signal the browser used to get when the backend was unreachable
    // through the rewrite: no response. A 502 keeps the client's offline
    // fallback (lib/api.js) behaving as it did.
    return new Response(JSON.stringify({ detail: 'backend unreachable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      'Content-Type': upstream.headers.get('content-type') || 'application/json',
      'Cache-Control': 'no-store',
    },
  })
}

export const GET = forward
export const POST = forward
export const dynamic = 'force-dynamic'
