"""DEPRECATED — the Supervisor role moved into the Symptom-Intake agent.

    from app.agents.supervisor import Supervisor   # still works
    Supervisor is SymptomIntakeAgent               # ...but it is this now

WHAT CHANGED
------------
Orchestration used to live in a separate, platform-owned `Supervisor` class in
this file. The team moved the role so that every worker is called through
Symptom-Intake. Today:

  * `orchestration.PipelineOrchestrator`  — the machinery (step order, A2A
    plumbing, safety gate, Reflection loop). Owned by Sham Goh. Declares no
    identity of its own.
  * `intake.SymptomIntakeAgent`           — mixes it in, and therefore IS the
    orchestrator. `SLUG == "intake"`, `CAPABILITY.classification == ORCHESTRATOR`.

`Supervisor` is kept as an alias so existing imports, tests and any teammate's
in-flight branch keep working rather than failing at import time. It is the same
object, not a subclass, so `Supervisor() is` an intake agent and
`Supervisor.SLUG == "intake"`.

IF YOU ARE UPDATING CODE
------------------------
Use `SymptomIntakeAgent` directly. Anything asserting `SLUG == "supervisor"` or
expecting a separate orchestrator object needs changing, not aliasing — that
assumption is what moved.

TO REVERT
---------
The whole takeover is one commit. `git revert` it and this file comes back as
the real Supervisor, with `capability.ORCHESTRATOR_SLUG` returning to
"supervisor".
"""
from __future__ import annotations

from .intake import SymptomIntakeAgent
from .orchestration import PipelineOrchestrator

#: Backwards-compatible alias. See the module docstring — this is the intake
#: agent, which now holds the orchestrator role.
Supervisor = SymptomIntakeAgent

__all__ = ["PipelineOrchestrator", "Supervisor", "SymptomIntakeAgent"]
