# Orchestrator migration — what changed and what you need to do

**Audience:** James, Aaron, Marcus, Heriz.
**Author:** Sham Goh (Symptom-Intake). **Date:** 2026-08-18.
**Commit:** `5483aa7` — `feat(intake)!: Symptom-Intake takes over the orchestrator role`

This file is written so you can paste it straight into your AI assistant and say
*"apply this to my branch."* Everything is stated as before/after with exact file
paths and symbol names.

---

## 1. The change in one paragraph

There used to be a separate `Supervisor` agent whose only job was to call the six
workers in order. **It no longer exists as an agent.** The `SymptomIntakeAgent`
now does that job as well as its own: it normalises the patient's text (step 1,
unchanged) and then sequences classifier → safety → routing → hitl → reflection.
The sequencing code moved out of `supervisor.py` into a new
`agents/orchestration.py`, which is a **mixin, not an agent** — it has no `SLUG`,
no `CAPABILITY`, no `COMMS`, and nothing ever sends it a message.

```
BEFORE                                   AFTER
  Supervisor (agent #7)                    (deleted)
    └─ calls intake                        SymptomIntakeAgent
    └─ calls classifier                      ├─ normalises text   (its own job)
    └─ calls safety                          └─ calls classifier, safety,
    └─ calls routing                            routing, hitl, reflection
    └─ calls hitl
    └─ calls reflection
```

---

## 2. Does anything you own break?

**No. Nothing in your agent needs to change for it to keep working.**

We ran the full suite after the change: **562 passed, 17 skipped, exit 0**, and
that includes every one of your test files, unmodified.

The reason nothing breaks is that `supervisor.py` still exists as a **deprecated
alias**:

```python
# backend/app/agents/supervisor.py
Supervisor = SymptomIntakeAgent
```

So your existing code keeps working:

```python
from app.agents.supervisor import Supervisor   # still imports fine
sup = Supervisor()                              # you now get the intake agent
sup._deliver(bus, my_agent)                     # same method, same behaviour
sup._safety_request(state, seq)                 # same method, same behaviour
```

Those six methods now also have **public names** (`deliver_inbox`, `publish`,
`open_message`, `safety_request`, `verify_safety_response`,
`apply_safety_failsafe`). The underscore spellings are aliases of the same
functions, so switch when convenient — not on a deadline. Full list in
`orchestration.py`'s module docstring and in `docs/vault/Intake Handoff Guide.md`.

Files of yours that do this today, all still green:

| File | Owner |
|---|---|
| `backend/scripts/show_a2a.py` | James |
| `backend/tests/agents/test_routing_a2a.py` | Marcus |
| `backend/tests/test_safety_routing_a2a_live.py` | Marcus |
| `backend/tests/agents/test_comms.py` | shared |

**The one thing to understand:** when your code says `Supervisor()`, you are now
holding *Sham's intake agent*. It behaves identically today. But you are now
coupled to that agent without having chosen to be, which is why this document
exists rather than the change landing silently.

---

## 3. Data contracts — what changed and what did not

### 3.1 `CaseState` — UNCHANGED

**No fields added. No fields removed. No fields renamed. No types changed.**

If you were expecting new data fields to read: there are none. Nothing to add to
your agent.

### 3.2 `AgentContract` (`writes` / `returns`) — UNCHANGED for every agent

Including intake's. `SymptomIntakeAgent.run()` still writes only:

```python
writes  = {"normalised_symptoms", "detected_language", "intake_keywords"}
returns = {"source", "normalised_symptoms", "detected_language", "keywords"}
```

Orchestration lives in a *different method* (`orchestrate()`), which mutates
nothing itself — it only calls workers. So `test_contracts.py` is unaffected and
the isolation boundary you rely on is exactly as strong as before.

### 3.3 `COMMS` — only intake's changed, and only by ADDING

| Agent | Before | After |
|---|---|---|
| **intake** publishes | `symptoms.normalised` | `symptoms.normalised`, **`case.opened`**, **`safety.assessment.requested`** |
| **intake** subscribes | `case.opened` | `case.opened`, **`decision.reviewed`**, **`safety.override`** |
| classifier / safety / routing / hitl / reflection | — | **unchanged** |

Nothing was removed, so no existing subscription stopped being delivered.

### 3.4 A2A message senders/recipients — **THIS IS THE ONE REAL DATA CHANGE**

Three messages changed the agent named in `sender` or `recipient`. The intents,
payload keys and payload values are all identical.

| Intent | Field | Before | After |
|---|---|---|---|
| `case.opened` | `sender` | `"supervisor"` | **`"intake"`** |
| `safety.assessment.requested` | `sender` | `"supervisor"` | **`"intake"`** |
| `decision.reviewed` | `recipient` | `"supervisor"` | **`"intake"`** |

Full conversation now:

```
intake      -> intake      : case.opened
intake      -> classifier  : symptoms.normalised
classifier  -> broadcast   : acuity.classified
intake      -> safety      : safety.assessment.requested
safety      -> broadcast   : safety.override
routing     -> broadcast   : care.routed
hitl        -> reflection  : review.decision
reflection  -> intake      : decision.reviewed
```

> `intake -> intake` for `case.opened` looks odd and is correct: the orchestrator
> opens the case, and the orchestrator is also the intake worker. It is delivered
> normally because intake subscribes to that intent.

**Who this affects:**

* **Aaron** — your Phase 2 validation reads `safety.assessment.requested`. The
  *payload* is unchanged (`classifierAcuity`, `classifierConfidence`,
  `classifierMessageSeq`, `fastPathDetected`). Only `sender` changed. **If you
  assert `sender == "supervisor"` anywhere, that assertion needs updating.**
* **Marcus** — same, in `test_routing_a2a.py` / `test_safety_routing_a2a_live.py`
  if you check senders.
* Everyone else — no impact.

**Do not hard-code the new name either.** Use the constant:

```python
from app.agents.capability import ORCHESTRATOR_SLUG   # == "intake"

assert msg.sender == ORCHESTRATOR_SLUG                # survives the role moving again
```

### 3.5 Audit trail — one actor renamed

The final aggregation entry changed actor:

```
BEFORE:  actor="supervisor", action="aggregate"
AFTER:   actor="intake",     action="aggregate"
```

Per-agent `action="message"` entries are unchanged — each still uses the sending
agent's own slug.

### 3.6 SSE events and the HTTP API — UNCHANGED

* Event names unchanged: `case_open`, `guardrail`, `agent_active`,
  `agent_result`, `agent_message`, `safety_override`, `final`, `error`.
* Event shapes unchanged.
* There was never an `agent_active` event for `supervisor`, so nothing was lost.
* `agent_message` events carry `sender` / `recipient`, so they reflect §3.4.
* The final payload's `messages[]` array reflects §3.4. Every other field is
  unchanged.

### 3.7 Tool allow-lists

`intake` gained the two orchestration pseudo-tools:

```python
TOOL_ALLOWLIST = ["llm.complete", "route", "aggregate"]
```

Every other agent is **explicitly denied** `route` and `aggregate` in
`tests/fixtures/tool_access_cases.json`. If you add either to your own agent's
allow-list, E6 will fail — deliberately. Only the orchestrator may sequence.

---

## 4. What each of you should actually do

### Everyone — the 2-minute version

```bash
git fetch && git merge origin/uat        # or rebase, however you work
cd backend && python -m pytest -m <your-marker>
```

If it passes, you are done. Nothing else is required.

### James (classifier)

1. Nothing in `classifier.py` changed. Your agent is untouched.
2. `capability.py` now has `ORCHESTRATOR_SLUG = "intake"` and the guard reads
   that constant instead of the literal `"supervisor"`. The rule is unchanged —
   exactly one agent may declare `ORCHESTRATOR`.
3. `reflection.py:emit()` now sends `decision.reviewed` to
   `recipient=ORCHESTRATOR_SLUG`. It was hard-coded to `"supervisor"`, an agent
   that no longer exists, so the critic's verdict was going nowhere.
4. `scripts/show_a2a.py` still works unmodified. Optional cleanup: use
   `SymptomIntakeAgent` directly.

### Aaron (safety)

1. Check for `sender == "supervisor"` assertions in `tests/agents/test_safety.py`.
   Replace with `ORCHESTRATOR_SLUG`.
2. Your `safety.assessment.requested` payload contract is **unchanged** —
   correlation via `classifierMessageSeq` works exactly as before.
3. Your semantic layer, red-flag rules and override logic are untouched.

### Marcus (routing)

1. `test_routing_a2a.py` and `test_safety_routing_a2a_live.py` import
   `Supervisor`. They pass as-is. Optional: switch to `SymptomIntakeAgent`.
2. Your async `run()` change is preserved — the orchestrator awaits routing.

### Heriz (hitl, handoff)

1. No changes needed. `hitl` is untouched.
2. `handoff.py` says it needs to decide *"where it plugs into `orchestrate()`"* —
   that method now lives in `agents/orchestration.py` (`PipelineOrchestrator`).
   Same method, new file.

---

## 5. How to test against the orchestrator

Unchanged, and this is the easy path — you do **not** need the full pipeline:

```bash
cd backend
python scripts/try_agent.py             # readiness board, all agents
python scripts/try_agent.py safety      # everything YOUR agent receives, in full
```

```python
from tests.agents.intake_handoff import stage_case, drive

setup = stage_case("routing")     # the case as it ARRIVES at your agent
result, state = drive("routing")  # ...or just run your agent on it
```

`stage_case` already uses `ORCHESTRATOR_SLUG`, so the messages it stages carry
the correct sender automatically.

Full detail: `docs/vault/Intake Handoff Guide.md`.

---

## 6. Frontend

Four places referenced a Supervisor that no longer exists. All fixed in this
change; listed so nobody re-adds them:

| File | Was | Now |
|---|---|---|
| `frontend/lib/acuity.js` | `AGENTS[0] = {key:'supervisor', name:'Supervisor'}` | `ORCHESTRATOR` exported separately; `AGENTS` is the six workers |
| `frontend/lib/useTriage.js` | `states.supervisor` | `states.orchestrator` |
| `frontend/components/PipelineVisualizer.jsx` | filtered out `supervisor`; `states.supervisor` | renders `ORCHESTRATOR` as the conductor; `states.orchestrator` |
| `frontend/components/PipelinePage.jsx` | hard-coded `` `supervisor → ${e.agent}` `` | `` `orchestrator → ${e.agent}` `` |
| `frontend/components/PipelinePage.jsx` | *"a team of **seven** agents… coordinated by a Supervisor"* | *"a team of **six** agents… the first one also acts as the coordinator"* |
| `frontend/components/PipelinePage.jsx` | *"Watch the **Supervisor** activate…"* | *"Watch **Symptom-Intake** activate…"* |
| `frontend/components/PatientTriage.jsx` | *"watch the **Supervisor** activate the worker agents"* | *"watch the agents activate one by one"* |
| `PipelineVisualizer.module.css` | `.supervisorRow` | `.orchestratorRow` |

The last four were **user-visible copy**, not just code — the landing page told
patients the system had seven agents coordinated by a Supervisor. Both halves of
that sentence were wrong after this change.

The visualiser still shows a conductor node above the fan-out. It is now labelled
**Symptom-Intake / Orchestrator**, and intake also appears in the worker row —
two indicators, one agent, which is the honest picture.

### Verified, not assumed

`npm install` was run and the frontend was built and executed against a live
backend:

```
npm run build   ->  9/9 pages generated, 0 errors
npm run lint    ->  clean (1 pre-existing font warning in app/layout.jsx, unrelated)
grep "Supervisor" .next/static/chunks/  ->  0 occurrences in the compiled bundle
```

Both servers were started (backend :8000, frontend :5173) and a real triage was
streamed **through the Next.js proxy**, i.e. the exact path the browser takes:

```
POST /api/triage/stream   "Tengo un dolor fuerte en el pecho y no puedo respirar"  (es)

agents activated: intake -> classifier -> safety -> routing -> hitl -> reflection
  [0] intake      -> intake      : case.opened
  [1] intake      -> classifier  : symptoms.normalised
  [2] classifier  -> broadcast   : acuity.classified
  [3] intake      -> safety      : safety.assessment.requested
  [4] safety      -> broadcast   : safety.override
  [5] routing     -> broadcast   : care.routed
  [6] hitl        -> reflection  : review.decision
  [7] reflection  -> intake      : decision.reviewed

acuity: P1_RESUSCITATION | tier: Emergency Department | escalated: True
```

A Spanish emergency reaching P1 also confirms the translation -> `redflags.py`
chain still works end to end with a live LLM.

> **One limit, stated:** the pages are client-rendered, so the agent nodes are
> not in the server HTML and could not be asserted from `curl`. The DOM was not
> inspected in a browser. Everything above the DOM — build, lint, bundle
> contents, routing, the proxy, and the full SSE stream — was verified.

---

## 7. Reverting

The whole change is one commit.

```bash
git revert 5483aa7
```

That restores the separate `Supervisor` agent, sets `ORCHESTRATOR_SLUG` back to
`"supervisor"`, and returns `reflection.py`'s recipient. Nothing else depends on
it.

---

## 8. The honest trade-off

Stated plainly so nobody discovers it in a review.

**What was gained:** every worker is called through Symptom-Intake, and the
orchestration engine has a named owner rather than being unowned platform code.

**What was given up:** the agent that produces the pipeline's input is now also
the agent that decides who consumes it. There is no independent party between
those two jobs any more. That is a reduction in defence-in-depth.

**What did NOT change, and this is the important part:** the step order is still
fixed in code. Intake gained the role, not discretion over the sequence — an
order an LLM could rearrange would not be safety-gated. `AgentContract`
enforcement, the safety gate between safety and routing, and the red-flag
override are all unchanged and still mechanical.
