---
tags: [runbook, handoff, active, careroute]
updated: 2026-08-18
---
# HITL Handshake Runbook — reproduce James's verified state on Heriz's branch

Back to [[Home]]. Companion to [[HITL Clarification Handshake]] (the design note — read that second).

**Audience: an LLM coding agent, or Heriz, working on `feature/human-in-the-loop-agent`.**
**Goal: end up byte-identical to `feature/classifier-hitl-handshake`, with the same test counts
and the same script output.**

---

## ⛔ THE ONE RULE THAT MAKES THIS DETERMINISTIC

> **Generate nothing. Write no code. Author no tests.**
> Every file in this runbook is copied byte-exact out of git. If you find yourself composing a
> Python function, a test body, or a docstring, **you have left the runbook** — stop and re-read it.

Two LLMs asked to *write* the same tests produce two different files. Two LLMs asked to *run
`git checkout`* produce the same bytes. That difference is the entire reason this document exists.

The runbook has been executed end-to-end against `origin/feature/human-in-the-loop-agent`
(at `2e645f2`) in a clean worktree, and every expected value below is a **measured** result from
that run, not a prediction.

---

## Scope — what this does and does NOT do

**DOES:** bring across the verification that the classifier→HITL clarifying-question handshake is
*delivered* — two tests, one demo script, one design note.

**DOES NOT:** implement the `ask` action. `hitl.py` is **not modified by this runbook at all.**
That work is Heriz's, it requires decisions no agent should make on his behalf, and it is specified
separately in [[HITL Clarification Handshake]] §3–§6.

If you complete this runbook and `git status` shows `backend/app/agents/hitl.py` as modified,
**you have done something wrong.** Revert it.

---

## Step 0 — Preconditions

```bash
cd <repo root>
git rev-parse --abbrev-ref HEAD          # expect: feature/human-in-the-loop-agent
git status --short                       # expect: empty (commit or stash first)
git fetch origin
```

Confirm the classifier side is present on this branch — the handshake cannot exist without it:

```bash
git merge-base --is-ancestor 36fd709 HEAD && echo OK
```

Expected: `OK`. If it prints nothing, stop: this branch predates the classifier's A2A payload and
nothing below will work. Merge `origin/uat` first.

## Step 1 — Copy the three files, byte-exact

```bash
git checkout origin/feature/classifier-hitl-handshake -- \
  backend/scripts/show_clarification_handshake.py \
  backend/tests/agents/test_classifier.py \
  "docs/vault/HITL Clarification Handshake.md"
```

`test_classifier.py` is safe to take wholesale: this branch's copy is **already identical** to the
pre-change version on James's branch (verified with `git diff`), so this adds his two new tests and
changes nothing else.

Verify the bytes rather than trusting the copy:

```bash
git hash-object backend/scripts/show_clarification_handshake.py
git hash-object backend/tests/agents/test_classifier.py
git hash-object "docs/vault/HITL Clarification Handshake.md"
```

Expected, exactly:

```
35d730112130934f55fe4f276df8e8451accd0ba
cd90a9d6bc1af9549df9f08ac261993fff0269e5
d97d8d523ffe22c16615ad1dab557e7ab48ecc47
```

Any other hash means the file was edited rather than copied. Re-run Step 1.

**Do NOT copy `docs/vault/Changelog.md`.** It is the one file that conflicts (append-vs-append on
both branches). Read James's entry with
`git show origin/feature/classifier-hitl-handshake:docs/vault/Changelog.md | head -60` and add your
own entry above it, newest-first, when you commit.

## Step 2 — Dependencies

This branch carries Marcus's routing work, which imports `geopy` and `bs4`. A venv set up before
that merge will fail at import with `ModuleNotFoundError` before any of this runs — this was hit
during verification and is not optional:

```bash
cd backend
C:/venvs/careroute/Scripts/python.exe -m pip install -r requirements.txt
```

## Step 3 — Verify (measured on this branch, not predicted)

```bash
cd backend
C:/venvs/careroute/Scripts/python.exe scripts/show_clarification_handshake.py
```

Expected output, exactly:

```
classifier -> P3_URGENT 0.427 | threshold 0.5
proposal   -> {'feature': 'cold_symptoms', 'label': 'cold symptoms', 'question': 'Do you also have a fever or any difficulty breathing?', 'gain': 2.482}
hitl inbox -> 1 msg; clarification visible = True
hitl action-> {'source': 'deterministic', 'escalated': True, 'reason': 'Classifier confidence 0.43 is below the 0.50 escalation threshold.'}

GAP: the question was delivered and ignored. HITL's action space is ('escalate to a clinician', 'do not escalate') and must become escalate / ask / proceed. Owner: Heriz.
```

Then:

```bash
C:/venvs/careroute/Scripts/python.exe -m pytest -m "classifier or hitl" -q
```

Expected: **`40 passed, 1 skipped`** (classifier alone: `36 passed, 1 skipped`).

The deselected count will differ from James's branch — this branch has more tests overall. That is
expected and is not a failure. Only the 40/1 and 36/1 figures must match.

Lint **the two files this runbook adds**, not the whole tree:

```bash
C:/venvs/careroute/Scripts/python.exe -m ruff check \
  scripts/show_clarification_handshake.py tests/agents/test_classifier.py
```

Expected: `All checks passed!`.

> ⚠️ **`ruff check .` over the whole tree currently reports 5 errors on this branch, and they are
> not yours.** All five are pre-existing in `app/agents/routing.py` (Marcus's file) — unused imports
> (`F401`) and redefinitions (`F811`) from the routing merge. **Do not fix them here**: it is
> another owner's file, and a lint-only commit touching it will collide with his work. Tell Marcus.
> Note this *will* fail the MLOps lint gate in CI, so it needs fixing by someone before this branch
> merges anywhere — just not in this runbook.

## Step 4 — Commit

```bash
git add backend/scripts/show_clarification_handshake.py \
        backend/tests/agents/test_classifier.py \
        "docs/vault/HITL Clarification Handshake.md" docs/vault/Changelog.md
git commit
```

Conventional Commits, e.g. `test(hitl): bring across the verified classifier handshake`.

---

## Files you must NOT touch

| File | Why |
|---|---|
| `backend/app/agents/classifier.py` | James's. The handshake works; changing it breaks the expected output above. |
| `backend/app/agents/hitl.py` | Yours, but **out of scope for this runbook** — see [[HITL Clarification Handshake]]. |
| `backend/app/agents/supervisor.py` | Shared/unowned. Nothing here needs it. |
| `backend/app/agents/messaging.py` | Platform. The bus already delivers correctly. |

If a step seems to require editing any of these, the step is wrong — stop and ask James.

## If the output differs

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: geopy` / `bs4` | venv predates the routing merge | Step 2 |
| `clarification visible = False` | this branch lacks `36fd709` | merge `origin/uat`, re-check Step 0 |
| `hitl inbox -> 0 msg` | delivery broken — James's side | do not patch it; tell James |
| `gain` ≠ 2.482 | the ML model is present and altering the path | note it and tell James; do not "fix" the expected value |
| `38 passed` | Step 1 copied nothing | re-run Step 1, check the hashes |
| `ruff check .` → 5 errors in `routing.py` | pre-existing, Marcus's file | leave them; tell Marcus |
| any test edited to make it pass | you left the runbook | `git checkout` the file again |

That last row is the important one. **These tests are evidence. A test edited to go green is not a
passing test, it is a deleted one.**

---

## STOP — what comes after this is not automatable

Once Step 3 is green, the handshake is verified and the runbook is finished. The remaining work —
widening HITL's action space to escalate / ask / proceed — is specified in
[[HITL Clarification Handshake]] and **deliberately leaves four decisions open** (§5): the `gain`
threshold, which acuity codes may never be asked at, what happens to a declined proposal, and
whether HITL stays a POLICY_NODE.

**An LLM must not choose these.** They are triage-workload and clinical-safety judgements, and the
number James used in a throwaway prototype (`1.0`) is explicitly not defended. Bring them to Heriz.

Note that §4 of the design note lists **five** floors, not four. The third — an incoming
`state.escalated == True`, set upstream by the Supervisor's Safety A2A fail-safe
(`_apply_safety_failsafe`) — is one Heriz added to `hitl.py` after James's merge, and it was found
while verifying this runbook. Asking must never replace it either. The note has been corrected;
this branch was right and James's read of the floors was the stale one.
