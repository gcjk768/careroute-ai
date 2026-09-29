// Staff login / status / logout. See lib/staffSession.js.
import { NextResponse } from 'next/server'
import { COOKIE, authRequired, checkPassword, cookieOptions, issueToken, validToken } from '@/lib/staffSession'

export const dynamic = 'force-dynamic'

export async function GET(request) {
  return NextResponse.json({
    required: authRequired(),
    authenticated: validToken(request.cookies.get(COOKIE)?.value),
  })
}

export async function POST(request) {
  if (!authRequired()) return NextResponse.json({ authenticated: true, required: false })
  const { password } = await request.json().catch(() => ({}))
  if (!checkPassword(password)) {
    // ponytail: fixed delay per failed attempt slows guessing on one connection;
    // add a per-IP limit (or an ALB WAF rate rule) if the portal faces real traffic.
    await new Promise((r) => setTimeout(r, 1000))
    return NextResponse.json({ detail: 'Invalid staff password' }, { status: 401 })
  }
  const res = NextResponse.json({ authenticated: true, required: true })
  res.cookies.set(COOKIE, issueToken(), cookieOptions(request))
  return res
}

export async function DELETE(request) {
  const res = NextResponse.json({ authenticated: false })
  res.cookies.set(COOKIE, '', { ...cookieOptions(request), maxAge: 0 })
  return res
}
