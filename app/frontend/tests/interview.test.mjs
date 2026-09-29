// node --test tests/interview.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import { appendAnswer, interviewProgress, isInterviewPending, toClarification, MAX_ANSWER_CHARS } from '../lib/interview.js'

const question = {
  feature: 'cold_symptoms', label: 'cold symptoms', gain: 1.7, source: 'template',
  question: 'Do you also have a fever or any difficulty breathing?', statement: 'fever',
}

test('pending only while the backend is asking', () => {
  assert.equal(isInterviewPending(null), false)
  assert.equal(isInterviewPending({ interview: { round: 0, budget: 4, done: false }, clarification: question }), true)
  assert.equal(isInterviewPending({ interview: { round: 2, budget: 4, done: true }, clarification: null }), false)
  // A decided turn with a stray question, or an older backend without the block, is done.
  assert.equal(isInterviewPending({ interview: { done: false }, clarification: null }), false)
  assert.equal(isInterviewPending({ clarification: question }), false)
})

test('an answer becomes the transcript entry the backend expects', () => {
  const entry = toClarification(question, '  no  ')
  assert.deepEqual(entry, {
    question: question.question, answer: 'no', feature: 'cold_symptoms', source: 'template', statement: 'fever',
  })
  assert.equal(toClarification({ question: 'q?' }, 'x'.repeat(500)).answer.length, MAX_ANSWER_CHARS)
  assert.deepEqual(toClarification({ question: 'q?' }, 'a'), { question: 'q?', answer: 'a', feature: null, source: null, statement: null })
})

test('appending never mutates and drops an empty answer', () => {
  const before = [toClarification({ question: 'q1?' }, 'a1')]
  const after = appendAnswer(before, question, 'yes')
  assert.equal(before.length, 1)
  assert.equal(after.length, 2)
  assert.equal(after[1].answer, 'yes')
  assert.equal(appendAnswer(before, question, '   '), before)
  assert.equal(appendAnswer(undefined, question, 'yes').length, 1)
})

test('progress counts the pending question against the budget', () => {
  assert.deepEqual(interviewProgress({ interview: { round: 1, budget: 4, done: false }, clarification: question }), { current: 2, budget: 4 })
  assert.equal(interviewProgress({ interview: { round: 1, budget: 4, done: true } }), null)
})
