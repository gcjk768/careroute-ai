// The clarifying interview, client side. Pure helpers, no React, so they can be
// unit-tested with `node --test` (see tests/interview.test.mjs).
//
// The conversation is STATELESS on the server: every turn re-posts the original
// complaint plus the whole transcript (`clarifications`), and the backend
// replies with either the next question (`final.interview.done === false`,
// `final.clarification` set) or the decision (`done === true`). See
// docs/design/specs/2026-09-26-clarifying-chat-interview-design.md.

export const QUICK_REPLIES = ['Yes', 'No', 'Not sure']
export const MAX_ANSWER_CHARS = 300

/** True while the backend is waiting for an answer rather than done deciding. */
export function isInterviewPending(result) {
  if (!result) return false
  // An older backend has no `interview` block and never asks: treat as done.
  if (!result.interview) return false
  return result.interview.done === false && Boolean(result.clarification?.question)
}

/** The transcript entry the backend expects back for `clarification` answered with `answer`. */
export function toClarification(clarification, answer) {
  return {
    question: clarification.question,
    answer: String(answer ?? '').trim().slice(0, MAX_ANSWER_CHARS),
    feature: clarification.feature ?? null,
    source: clarification.source ?? null,
    statement: clarification.statement ?? null,
  }
}

/** A new transcript with the pending question answered. Never mutates its input. */
export function appendAnswer(transcript, clarification, answer) {
  const entry = toClarification(clarification, answer)
  if (!entry.answer) return transcript
  return [...(transcript || []), entry]
}

/** "Question 2 of 4" for the thread footer; null when the interview is over. */
export function interviewProgress(result) {
  if (!isInterviewPending(result)) return null
  const { round = 0, budget = 4 } = result.interview
  return { current: Math.min(round + 1, budget), budget }
}
