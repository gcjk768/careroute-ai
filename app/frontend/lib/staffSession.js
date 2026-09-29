// Server-only staff session: a signed, httpOnly cookie issued by
// /api/staff/session after a password check, and required by the escalation
// proxy. Without it, the proxy attached the staff key for ANY caller — an
// anonymous browser could read and decide escalations (confused deputy).
//
// Enforced only when CAREROUTE_STAFF_API_KEY is set (the AWS stack); with it
// unset the backend is open anyway, so local dev, Playwright and CI need no
// login. With the key set but no CAREROUTE_STAFF_PASSWORD, login fails closed.
import { createHash, createHmac, timingSafeEqual } from 'node:crypto'

export const COOKIE = 'cr_staff'
export const MAX_AGE = 8 * 60 * 60 // seconds — one shift

const staffKey = () => (process.env.CAREROUTE_STAFF_API_KEY || '').trim()
export const authRequired = () => Boolean(staffKey())

const sign = (exp) => createHmac('sha256', staffKey()).update(`staff:${exp}`).digest('base64url')

// Hash both sides first so timingSafeEqual gets equal lengths and the
// comparison time does not leak the password length.
const sameSecret = (a, b) =>
  timingSafeEqual(createHash('sha256').update(a).digest(), createHash('sha256').update(b).digest())

export function checkPassword(password) {
  const expected = process.env.CAREROUTE_STAFF_PASSWORD || ''
  return Boolean(expected) && typeof password === 'string' && sameSecret(password, expected)
}

export function issueToken(now = Date.now()) {
  const exp = Math.floor(now / 1000) + MAX_AGE
  return `${exp}.${sign(exp)}`
}

export function validToken(token, now = Date.now()) {
  if (!authRequired()) return true
  const [exp, sig] = String(token || '').split('.')
  if (!/^\d+$/.test(exp || '') || !sig) return false
  if (Number(exp) < now / 1000) return false
  return sameSecret(sig, sign(exp))
}

export function cookieOptions(request) {
  const https =
    request.headers.get('x-forwarded-proto') === 'https' || new URL(request.url).protocol === 'https:'
  return { httpOnly: true, sameSite: 'strict', secure: https, path: '/', maxAge: MAX_AGE }
}
