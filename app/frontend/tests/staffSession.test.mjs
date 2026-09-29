// node --test tests/staffSession.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import { authRequired, checkPassword, issueToken, validToken, MAX_AGE } from '../lib/staffSession.js'

test('open mode: no staff key means no login required', () => {
  delete process.env.CAREROUTE_STAFF_API_KEY
  assert.equal(authRequired(), false)
  assert.equal(validToken(undefined), true)
})

test('keyed mode: only a fresh, correctly signed token passes', () => {
  process.env.CAREROUTE_STAFF_API_KEY = 'k1'
  const now = Date.now()
  const tok = issueToken(now)
  assert.equal(validToken(tok, now), true)
  assert.equal(validToken(undefined, now), false)
  assert.equal(validToken('garbage', now), false)
  assert.equal(validToken(tok.replace(/\d+/, (e) => String(Number(e) + 99999)), now), false) // extended expiry
  assert.equal(validToken(tok, now + (MAX_AGE + 1) * 1000), false) // expired
  process.env.CAREROUTE_STAFF_API_KEY = 'k2'
  assert.equal(validToken(tok, now), false) // key rotated
})

test('password check fails closed when unset', () => {
  delete process.env.CAREROUTE_STAFF_PASSWORD
  assert.equal(checkPassword('anything'), false)
  process.env.CAREROUTE_STAFF_PASSWORD = 'right'
  assert.equal(checkPassword('right'), true)
  assert.equal(checkPassword('wrong'), false)
  assert.equal(checkPassword(undefined), false)
})
