---
tags: [architecture, careroute, active]
updated: 2026-08-19
---
# Intake Handoff Guide — how to test against the Symptom-Intake agent

Back to [[Home]]. Related: [[App Overview]] · [[Evaluation Plan]] · [[Agent Capability Audit]]

**For: James, Aaron, Marcus, Heriz.** Read this before wiring your agent to
intake's output. Owner: **Sham Goh** — ping me if anything here is wrong.

> [!info] The Supervisor agent is gone
> Symptom-Intake now holds the orchestrator role: it is both the first worker and
> the agent that sequences the other five. `supervisor.py` is a deprecated alias,
> so your existing `Supervisor()` calls still work unchanged. What that means for
> your data contracts is in
> [`docs/plans/orchestrator-migration-guide.md`](../plans/orchestrator-migration-guide.md).

You do **not** need to run the whole pipeline to test against my agent. There is
a harness that hands you a realistic `CaseState` so you can drive your own worker
in isolation.

---

## TL;DR

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt   # once
python scripts/try_agent.py             # is my agent + yours actually working?
python scripts/try_agent.py routing     # everything YOUR agent receives, in full
```

No pytest, no orchestrator, no full pipeline. In Python:

```python
from tests.agents.intake_handoff import stage_case, drive

setup = stage_case("routing")      # the case as it ARRIVES at your agent
result, state = drive("routing")   # ...or just run your agent on it
```

> [!warning] `pytest -m intake` collects the whole suite first
> If `numpy` / `scikit-learn` / `reportlab` are missing you get **8 collection
> errors** before any intake test runs — the ML modules fail to import. That is
> an environment problem, not my agent. Either install the requirements, or scope
> the run past them:
> ```bash
> pytest tests/agents -m intake     # skips the ML test modules entirely
> ```

---

## 0. Which helper do you want?

|  | Use this | Why |
|---|---|---|
| **James** (classifier) | `case_from_intake(...)` | You sit directly downstream of me, so what I leave behind *is* your input. |
| **Aaron, Marcus, Heriz** | `stage_case(...)` | You do **not**. Read on. |

`case_from_intake()` populates my three fields and nothing else. If you are the
classifier that is the whole truth. If you are safety, routing or hitl it is
misleading — you would get `acuity_code="P3_URGENT"`, `confidence=0.5`,
`care_tier="GP"`, which are the **dataclass defaults**, not a classified case.
Your test would pass against fiction.

`stage_case(slug)` replays the real pipeline from intake up to (not including)
your agent, and hands you both halves of what the orchestrator would give you:

```python
setup = stage_case("hitl")
setup.state             # CaseState with every upstream lane genuinely populated
setup.inbox             # the A2A messages your COMMS.subscribes entitles you to
setup.writes_by_stage   # {'intake': [...], 'classifier': [...], 'safety': [...]}
setup.skipped           # upstream agents still unimplemented on your branch
setup.deliver(agent)    # hand over the inbox, exactly as the orchestrator does
```

**The inbox half matters as much as the state.** `CaseState` only holds the
*latest* value of a field; the bus holds what each agent *claimed* when it acted.
If your agent calls `received_payload(...)`, that path is untested without an
inbox — and outside a full pipeline run, `stage_case` is the only way to get one.

---

## 1. What you get from me

I write exactly three fields on `CaseState`. Nothing else — my `AgentContract`
fails the build if I touch yours.

| Field | Type | What it is |
|---|---|---|
| `normalised_symptoms` | `str` | One cleaned clinical sentence |
| `detected_language` | `str` | ISO 639-1: `en` `es` `zh` `ms` `fr` `ta` |
| `intake_keywords` | `list[str]` | **0–6** symptom keywords, most severe first |

Over the A2A bus I publish **`symptoms.normalised`** to `classifier`, carrying
`{normalised_symptoms, detected_language, keywords}`.

`raw_text` is **never modified.** Whatever the patient typed is still there, so
you can always read `f"{state.normalised_symptoms} {state.raw_text}"` — which is
what the classifier and the red-flag scan both already do.

---

## 2. ⚠️ The one thing that will break you

**`intake_keywords` can be an empty list.** This is by design, not a bug.

When the LLM is unavailable **and** the input is not English, my deterministic
path has no English vocabulary to match, so it returns `[]`. That happens every
time the ASI10 kill switch is engaged, and it is exactly how the whole test suite
runs.

```python
state.intake_keywords[0]        # 💥 IndexError in production
state.intake_keywords[:3]       # ✅ fine — empty slice is fine
state.intake_keywords or [...]  # ✅ fine — fall back to your own signal
```

There is a test for this you can run against your own agent:

```bash
pytest -m intake -k survives_empty_keywords
```

If it fails for your agent, the fix is in your file. E1 measures this case
deliberately — see [[Evaluation Plan]].

---

## 3. ⚠️ `normalised_symptoms` is not always English

| Situation | `normalised_symptoms` | `detected_language` |
|---|---|---|
| English input | cleaned original | `en` |
| Non-English, **LLM up** | **translated to English** | `es` / `zh` / `ms` / `fr` / `ta` |
| Non-English, **LLM down** | cleaned original, still in that language | `es` / `zh` / `ms` / `fr` / `ta` |

So `detected_language != "en"` does **not** tell you the sentence is foreign — it
tells you the *patient* wrote in that language. Check the text, not the code.

**Aaron, this one is for you:** when the LLM is up, non-English emergencies now
arrive as English, so `redflags.py` matches them. `"Tengo dolor en el pecho"` →
`cardiac_chest_pain` fires. With the LLM down it does not, which is still the gap
your semantic layer covers. Your coverage now partly depends on my translation —
worth knowing rather than discovering.

---

## 4. Test your agent in isolation

Everything lives in
[`tests/agents/intake_handoff.py`](../../backend/tests/agents/intake_handoff.py).
It is a **plain module, not a test file** — importing it collects and runs
nothing. Four ready-made scenarios:

| Scenario | What it exercises |
|---|---|
| `english-emergency` | Chest pain + breathlessness, both keywords present |
| `translated-emergency` | Tamil in, **English** `normalised_symptoms` out |
| `mild-case` | Fever + cough — a non-escalating case |
| `zero-keyword-untranslated` | **Empty keywords**, untranslated sentence (the trap above) |

```python
from tests.agents.intake_handoff import stage_case, drive, case_from_intake

# The whole upstream chain, replayed. What YOU actually receive.
setup = stage_case("routing", "zero-keyword-untranslated")
agent = CareRoutingAgent()
setup.deliver(agent)                 # give it the A2A inbox first
result = agent.run(setup.state)

# Same thing in one line
result, state = drive("routing", "zero-keyword-untranslated")

# Override any CaseState field after the replay
setup = stage_case("hitl", "mild-case", age_band="65+", sex="Female")

# Just my agent's output, no chain (this is the one James wants)
state = case_from_intake("mild-case")
```

The LLM is **off** in all of these, so results are reproducible, nothing touches
the network, and you are testing the shape you get when the ASI10 kill switch is
engaged.

Two drift tests keep this honest, so you can trust it:
`test_mock_data_has_not_drifted_from_real_intake` re-runs my agent and checks the
mocks still match, and `test_replayed_supervisor_messages_match_the_real_supervisor`
checks the replayed `case.opened` / `safety.assessment.requested` still match what
the orchestrator really sends. If either drifts, my build breaks, not yours.

**Aaron:** `stage_case("safety")` gives you a real `safety.assessment.requested`
whose `classifierMessageSeq` points at the actual `acuity.classified` on the bus,
so your Phase 2 correlation check has something genuine to correlate against.
That is pinned by `test_safety_request_correlates_to_the_real_classifier_message`.

---

## 5. Readiness board

Before a dry run or a demo:

```bash
python scripts/try_agent.py
```

```
READINESS BOARD   (scenario: english-emergency)
  READY     classifier  inbox=['symptoms.normalised']
  READY     safety      inbox=['acuity.classified', 'safety.assessment.requested']
  READY     routing     inbox=['acuity.classified', 'safety.override']
  READY     hitl        inbox=['acuity.classified', 'safety.override', 'care.routed']
  READY     reflection  inbox=['safety.override', 'care.routed', 'review.decision']
```

`READY` means the agent ran on a fully-staged case with its inbox delivered and
returned every key its `CONTRACT` declares. `TEMPLATE` means that owner hasn't
implemented `run()` yet on the branch you are on. `BROKEN` / `ERROR` means it ran
and failed — that one is a real defect.

For one agent in full — what it inherits, from whom, its inbox, what it changed,
and what it publishes:

```bash
python scripts/try_agent.py routing
python scripts/try_agent.py safety --scenario zero-keyword-untranslated
python scripts/try_agent.py intake "chest pian for two dayz"   # my agent on any text
```

It also flags any field you wrote **outside your declared `CONTRACT.writes`**,
which is the thing most likely to break someone else's build.

The same board is in pytest, if you prefer: `pytest -m intake -k readiness -s`.

---

## 5b. Can your agent talk to the orchestrator on its own?

Separate question from the handoff above, and separately tested in
[`tests/agents/test_orchestrator_comms.py`](../../backend/tests/agents/test_orchestrator_comms.py):

```bash
pytest -m comms -k report_orchestrator -s
```

```
Each agent's conversation with the orchestrator:
  OK    intake      in=[case.opened]                                            out=symptoms.normalised -> classifier
  OK    classifier  in=[symptoms.normalised]                                    out=acuity.classified   -> broadcast
  OK    safety      in=[acuity.classified, safety.assessment.requested]         out=safety.override     -> broadcast
  OK    routing     in=[acuity.classified, safety.override]                     out=care.routed         -> broadcast
  OK    hitl        in=[acuity.classified, safety.override, care.routed]        out=review.decision     -> reflection
  OK    reflection  in=[safety.override, care.routed, review.decision]          out=decision.reviewed   -> intake
```

It drives each worker **alone** through the orchestrator's own `deliver_inbox()`
and `publish()` — not a reimplementation — so it proves the real round trip:

1. the orchestrator hands you **only** the intents you declared in `subscribes`;
2. you run, with no other worker involved;
3. your reply lands in all three places it must — the bus, `state.messages`, and
   the audit trail;
4. nothing you publish carries patient text.

> [!warning] The one most likely to fail for you
> `test_agent_survives_the_orchestrator_sending_nothing` delivers an **empty
> inbox**. An agent that does `received_payload("x")["y"]` crashes here, and
> would crash in production the first time a message is dropped. If it fails,
> the fix is in your file.

---

## 5c. The orchestrator's public API

If you drive the orchestrator directly (Marcus and Aaron already do), these are
the **supported** methods. They will not be renamed without telling you:

```python
from app.agents import SymptomIntakeAgent
orch = SymptomIntakeAgent()

orch.orchestrate(state, audit=..., log=..., delay=...)  # the whole pipeline
orch.deliver_inbox(bus, agent)                          # hand an agent its inbox
orch.publish(bus, state, message, audit)                # record a message
orch.open_message(state)                                # the `case.opened` message
orch.safety_request(state, classifier_seq)              # Phase 2 request to Safety
orch.verify_safety_response(bus, state, seq)            # the route gate -> issues
orch.apply_safety_failsafe(state, issues)               # force review, never lower acuity
orch.build_rationale(state) / orch.build_citations(state)
```

> [!note] These used to be private, and you were already calling them
> Twelve underscore-prefixed methods were being called from four of your test
> files — `_deliver`, `_publish`, `_safety_request`, `_verify_safety_response`
> and others. Private by naming convention, public in practice: no stability
> promise, but real code depending on them.
>
> **Nothing you have breaks.** The old underscore spellings still work as
> aliases, pinned by `test_legacy_underscore_names_still_work`. Switch when
> convenient, not on a deadline.

Everything else on that class is internal — `_timed_async`, `_timed_sync`,
`_safety_pregate`, `_apply_cross_visit_rule`, `_reflection_rerun`,
`_bump_more_urgent`. Those may change shape. If you need one, ask me and I will
promote it properly rather than you depending on an underscore.

---

## 6. Things that are stable, and things that aren't

**Safe to depend on:**
- the three field names and their types
- keywords are lowercase canonical labels, max 6, most severe first
- `raw_text` is untouched
- the agent never raises — any LLM failure degrades to the deterministic path
- `run()` is `async`; `emit()` is sync

**Don't depend on:**
- the exact keyword *strings* — they come from `_SYMPTOM_VOCABULARY` in my file
  and I add to it when E1 finds a gap (it already has, four times). If you need a
  fixed set, tell me and I'll freeze the ones you rely on.
- the wording of `normalised_symptoms` when the LLM ran — it is model output and
  varies between runs. Match on keywords, not on the sentence.
- `source` being `"llm"` or `"fallback"` for any given input; it depends on
  whether a provider was reachable.

---

## 7. Speed

The LLM only fires when the deterministic pass finds **nothing**, or when the
input is not English. English cases with keywords never touch the network.

- English, keywords found → **instant**
- LLM path → **~5–7 seconds** (measured through a local CLI provider, since removed)

If your test suite feels slow against intake, you're hitting the LLM path. Use
`case_from_intake()` mocks instead, or disable the LLM the way the root
`conftest.py` does.

---

## 8. Where things live

| | |
|---|---|
| Agent | [`backend/app/agents/intake.py`](../../backend/app/agents/intake.py) |
| **Handoff helpers** (import these) | [`backend/tests/agents/intake_handoff.py`](../../backend/tests/agents/intake_handoff.py) |
| **CLI** (no code needed) | [`backend/scripts/try_agent.py`](../../backend/scripts/try_agent.py) |
| Tests that keep the helpers honest | [`backend/tests/agents/test_intake_handoff.py`](../../backend/tests/agents/test_intake_handoff.py) |
| My own tests | [`backend/tests/agents/test_intake.py`](../../backend/tests/agents/test_intake.py) |
| E1 evaluation | [`backend/tests/test_eval_intake.py`](../../backend/tests/test_eval_intake.py) |
| E1 gold set | [`backend/tests/fixtures/intake_gold.json`](../../backend/tests/fixtures/intake_gold.json) — 44 rows, 6 languages |

Run mine: `pytest -m intake` · Run E1: `pytest -m eval -k intake`

Design decisions (why it cleans rather than summarises, why the fallback doesn't
translate, why Tamil is supported) are in the module docstring at the top of
`intake.py`. Read that before proposing a change to my agent — most of the
obvious ideas are already answered there, with the reason.
