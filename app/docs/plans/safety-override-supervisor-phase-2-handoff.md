# Safety-Override Phase 2 Supervisor Handoff Plan

## Purpose

This plan captures the Supervisor-owned work required to complete Phase 2 of the Safety-Override protocol.

Safety-owned implementation is already in place for:

- consuming `acuity.classified` and `safety.assessment.requested`
- validating request/classifier/state agreement
- correlating `requestSeq`
- emitting an enriched `safety.override` payload
- requiring human review when Safety detects protocol issues
- keeping patient text out of the Safety message payload

The remaining work belongs to the Supervisor/platform lane because it changes orchestration, message publication, route gating, audit behavior, and pipeline-level tests.

## Scope Boundary

Do not implement these changes from the Safety-Override branch unless platform ownership has approved it.

Expected Supervisor/platform files:

- `backend/app/agents/supervisor.py`
- `backend/tests/agents/test_comms.py`
- `backend/tests/test_pipeline.py`
- `backend/tests/test_api_e2e.py`, if SSE or final payload behavior changes

Safety-owned tests currently covering the receiving side live in:

- `backend/tests/agents/test_safety.py`

## Required Protocol

The intended Phase 2 message flow is:

```text
case.opened
symptoms.normalised
acuity.classified
safety.assessment.requested
safety.override
care.routed
review.decision
decision.reviewed
```

Routing must not start until the Supervisor has received and verified the Safety response for the current request.

## Supervisor Changes

### 1. Update Supervisor COMMS

Add `safety.assessment.requested` to `Supervisor.COMMS.publishes`.

Add `safety.override` to `Supervisor.COMMS.subscribes`.

This makes the request/response protocol explicit and enforceable through `enforce_comms()`.

### 2. Publish Directed Safety Request

After the classifier publishes `acuity.classified`, the Supervisor should publish a directed message to Safety:

```json
{
  "classifierAcuity": "P3_URGENT",
  "classifierConfidence": 0.48,
  "classifierMessageSeq": 2,
  "fastPathDetected": false
}
```

Requirements:

- recipient is `safety`
- intent is `safety.assessment.requested`
- payload excludes raw patient text
- payload excludes normalized patient text
- payload avoids evidence strings if they may contain patient wording
- `classifierMessageSeq` must be the actual sequence number assigned to the `acuity.classified` message

### 3. Deliver Request Before Safety Runs

The existing `_deliver(bus, self.safety)` call should deliver both:

- `acuity.classified`
- `safety.assessment.requested`

Safety already validates these messages in its own lane.

### 4. Verify Safety Response Before Routing

After Safety emits `safety.override`, but before Care-Routing starts, the Supervisor must verify:

- a `safety.override` response exists
- `requestSeq` matches the current `safety.assessment.requested` message
- `priorAcuity` matches the classifier acuity
- `forcedAcuity` is not less urgent than `priorAcuity`
- `rule` belongs to the closed `RED_FLAG_RULES` vocabulary when present
- model metadata does not introduce an arbitrary acuity code
- response protocol issues are handled as fail-safe conditions

### 5. Apply Fail-Safe on Protocol Failure

If the Safety response is missing, stale, malformed, or inconsistent, the Supervisor must:

- audit a protocol issue
- preserve the deterministic Safety result already applied to `CaseState`
- force clinician review
- never lower acuity
- only proceed to Care-Routing after the fail-safe state is applied

The fail-safe should not erase a valid deterministic Safety trigger.

## Test Plan

### Communication Tests

Add or update `backend/tests/agents/test_comms.py` to prove:

- Supervisor is allowed to publish `safety.assessment.requested`
- Safety receives both classifier announcement and Supervisor request
- Safety response correlates to the request through `requestSeq`
- removing the request creates a Safety protocol issue
- removing or withholding the response activates Supervisor fail-safe behavior

### Pipeline Tests

Update `backend/tests/test_pipeline.py` to expect this message order:

```text
case.opened
symptoms.normalised
acuity.classified
safety.assessment.requested
safety.override
care.routed
review.decision
decision.reviewed
```

Add route-gating assertions:

- Care-Routing starts only after verified `safety.override`
- stale or missing Safety response changes behavior by forcing review
- deterministic Safety override remains effective during protocol failure

### Privacy Tests

Add regression coverage proving:

- `safety.assessment.requested` contains no raw patient text
- `safety.assessment.requested` contains no normalized patient text
- `safety.override` contains no raw or normalized patient text
- audit entries for the request/response protocol do not contain patient text

### API/SSE Tests

If the streamed event sequence or final payload changes, update `backend/tests/test_api_e2e.py` to verify:

- the directed Safety request is visible only as an `agent_message`
- existing `safety_override` SSE event remains backward compatible
- final payload still includes ordered `messages`
- final payload contains no patient text in message payloads

## Verification Commands

Run from `backend/`:

```bash
.venv\Scripts\python -m pytest -m safety
.venv\Scripts\python -m pytest -m comms
.venv\Scripts\python -m pytest -m contract
.venv\Scripts\python -m pytest tests/test_pipeline.py
.venv\Scripts\python -m pytest tests/test_api_e2e.py
```

Before merge, run the full backend suite:

```bash
.venv\Scripts\python -m pytest
```

## Definition Of Done

Phase 2 is complete when:

- Supervisor publishes a directed `safety.assessment.requested` message after classification
- Safety emits an enriched, correlated `safety.override`
- Supervisor verifies that response before routing
- communication failure forces HITL without lowering acuity
- deterministic Safety behavior remains unchanged
- message and audit payloads exclude patient text
- Safety, communication, contract, pipeline, API, and full backend tests pass
