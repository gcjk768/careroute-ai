// node --test tests/models.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import { describeCall, questionCall, summarise } from '../lib/models.js'

const calls = [
  { task: 'intake.normalise', tier: 'fast', reason: 'base', model: 'gpt-4o-mini', ms: 410, cached: false },
  { task: 'classifier.classify', tier: 'deep', reason: 'base', model: 'gpt-4.1-mini', ms: 900, cached: true },
  { task: 'reflection.critic', tier: 'max', reason: 'low_confidence', model: 'gpt-4.1', ms: 1800, cached: false },
  { task: 'classifier.question', tier: 'fast', reason: 'base', model: 'gpt-4o-mini', ms: 700, cached: false },
]

test('each call names its agent, tier and model', () => {
  assert.deepEqual(describeCall(calls[0]), {
    agent: 'Symptom-Intake', what: 'normalise the complaint', tier: 'fast', model: 'gpt-4o-mini', ms: 410, note: '',
  })
})

test('escalation and cache are spelled out; a cached call has no latency', () => {
  assert.equal(describeCall(calls[2]).note, 'escalated: low confidence')
  const cached = describeCall(calls[1])
  assert.equal(cached.note, 'served from cache')
  assert.equal(cached.ms, null)
})

test('unknown tasks and tiers degrade to readable defaults', () => {
  const d = describeCall({ task: 'new.task', tier: 'weird', reason: 'unrouted', model: null, ms: 5 })
  assert.equal(d.agent, 'Agent')
  assert.equal(d.tier, 'default')
  assert.equal(d.model, 'provider default')
  assert.equal(d.note, 'not routed')
})

test('summary counts calls, distinct models and escalations', () => {
  assert.deepEqual(summarise(calls), { count: 4, models: ['gpt-4o-mini', 'gpt-4.1-mini', 'gpt-4.1'], escalated: 1 })
  assert.deepEqual(summarise(undefined), { count: 0, models: [], escalated: 0 })
})

test('the interview question is attributed to the call that worded it', () => {
  assert.equal(questionCall(calls).model, 'gpt-4o-mini')
  assert.equal(questionCall([]), null)
})
