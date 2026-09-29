// Promptfoo response transform for CareRoute's /api/triage/stream endpoint.
//
// The endpoint answers with Server-Sent Events, not JSON. Promptfoo's graders
// need ONE string to judge, so this collapses the stream into what a person
// would actually read:
//
//   * input refused by the guardrail  -> the guardrail's own block message
//     (an "error" event follows a "guardrail" event with status "blocked")
//   * triage completed                -> acuity + rationale + handoff summary
//     from the single "final" event
//   * anything else (stream cut, no final) -> the raw event kinds, so a broken
//     run reads as broken rather than as a pass.
//
// Signature per promptfoo docs: (json, text, context) => string | object.
// `json` is undefined for SSE bodies; `text` is the raw body.
module.exports = function transform(json, text) {
  const events = [];
  for (const block of String(text || '').replace(/\r\n/g, '\n').split('\n\n')) {
    for (const line of block.split('\n')) {
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (!payload) continue;
      try { events.push(JSON.parse(payload)); } catch { /* keep-alive or partial */ }
    }
  }

  const guard = events.find((e) => e.event === 'guardrail' && e.status === 'blocked');
  const err = events.find((e) => e.event === 'error');
  if (guard || err) {
    return `BLOCKED BY GUARDRAIL: ${(guard && guard.detail) || (err && err.message) || 'input refused'}`;
  }

  const fin = events.find((e) => e.event === 'final');
  if (fin) {
    const parts = [
      `Acuity ${fin.acuity && fin.acuity.code} — ${fin.careTier}.`,
      fin.rationale || '',
      fin.handoffSummary || '',
    ].filter(Boolean);
    return parts.join(' ');
  }

  return `NO FINAL EVENT (stream events: ${events.map((e) => e.event).join(',') || 'none'})`;
};
