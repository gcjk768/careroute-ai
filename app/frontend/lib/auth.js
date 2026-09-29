// Staff auth, browser side. The real gate is SERVER-side: /api/staff/session
// checks the password and sets an httpOnly cookie that the escalation proxy
// requires (lib/staffSession.js). localStorage only keeps the display name and
// a hint for the portal shell. With no staff key configured (local dev, CI)
// the server does not require a password and any login succeeds.
const KEY = 'careroute_staff'

export function getStaff() {
  try {
    return JSON.parse(localStorage.getItem(KEY))
  } catch {
    return null
  }
}

// Resolves to the session, or throws Error(message) when the server refuses.
export async function loginStaff(name, password) {
  const res = await fetch('/api/staff/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: password || '' }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `Sign-in failed (${res.status})`)
  }
  const session = { name: (name && name.trim()) || 'Staff', at: Date.now() }
  localStorage.setItem(KEY, JSON.stringify(session))
  return session
}

// { required, authenticated } from the server; null if it can't be reached.
export async function staffSession() {
  try {
    const res = await fetch('/api/staff/session', { cache: 'no-store' })
    return res.ok ? await res.json() : null
  } catch {
    return null
  }
}

export function logoutStaff() {
  localStorage.removeItem(KEY)
  fetch('/api/staff/session', { method: 'DELETE' }).catch(() => {})
}
