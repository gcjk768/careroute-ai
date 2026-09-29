"""[Platform] The reasoning-layer TEMPLATE — how a policy node becomes an agent
without giving up its deterministic guarantees.

THE PROBLEM THIS SOLVES
-----------------------
Three of the five member-owned workers (Safety-Override, Care-Routing,
Human-in-the-Loop) are deterministic policy nodes. The reviewer asked whether
they are genuinely agents. The tempting fix — replace the rules with an LLM — is
the wrong one: it would trade a guarantee for a probability. `redflags.py` says
it plainly ("These rules are intentionally NOT LLM-based... the last line of
defense"), and `test_triage_eval.py` gates on red-flag recall of exactly 1.0.

So the pattern here is ADDITIVE, never a replacement:

    deterministic result  ──┐
                            ├──►  monotone merge  ──►  final result
    reasoning layer       ──┘     (can only escalate)

  * The deterministic path stays EXACTLY as it is and becomes the FLOOR.
  * The reasoning layer may only make the outcome MORE cautious, never less.
  * Any failure — LLM down, timeout, unparsable JSON, layer disabled — returns
    None and the deterministic floor stands alone.

Consequences that make this safe to merge:
  * With the LLM unavailable (how the whole test suite and the offline demo run)
    behaviour is byte-identical to before this module existed. No regression.
  * Because the merge is monotone, the worst case for a hallucinating layer is
    an unnecessary escalation — never a missed one. Precision can degrade;
    RECALL, the safety-critical direction, cannot.

HOW TO USE THIS TEMPLATE (for the 3 policy-node owners)
-------------------------------------------------------
  1. Subclass `ReasoningLayer` in YOUR agent's file. Set `SLUG`, `SYSTEM`, and
     `ENV_FLAG`.
  2. Implement `build_prompt(state)` and `parse(data)`.
  3. Give your agent an `async def areason(state)` that calls the layer and
     stores the outcome on its own declared CaseState lane.
  4. Merge in `run()` so the deterministic result is the floor.
  5. Update your `CAPABILITY`: `reasoning` becomes non-empty, `action_space`
     gains real alternatives, `classification` becomes AGENT, `upgrade_path`
     goes empty. Add `"llm.complete"` to your `TOOL_ALLOWLIST`.
  6. Add the evaluation for it to `app/evals/plan.py` (the reviewer's Point 2)
     and write its dataset under `tests/fixtures/`.

Worked example: `safety.SemanticRedFlagLayer` (Safety-Override, owner Aaron).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .. import llm


def _flag_enabled(env_flag: str, default: str = "1") -> bool:
    """Feature flag for a reasoning layer. Default ON — the layer is monotone, so
    enabling it can only add caution — but every layer stays individually
    killable in production without a redeploy of the deterministic core."""
    return os.environ.get(env_flag, default).strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class ReasoningOutcome:
    """What a reasoning layer concluded. Deliberately small and serialisable so
    it can go straight into the audit trail and the SSE stream."""

    triggered: bool
    label: str = ""
    rationale: str = ""
    confidence: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "label": self.label,
            "rationale": self.rationale,
            "confidence": round(float(self.confidence), 4),
            "detail": self.detail,
        }


class ReasoningLayer:
    """Base class for an additive, bounded LLM reasoning step.

    Subclasses implement `build_prompt` and `parse`. This class owns everything
    that must NOT vary between agents: the feature flag, the failure contract
    (never raise, return None), JSON-mode invocation, and the guarantee that the
    caller can always fall back to its deterministic result.
    """

    #: Short identifier used in audit/telemetry, e.g. "safety.semantic".
    SLUG: str = "reasoning"

    #: The system prompt. Subclasses MUST override with a bounded, single-purpose
    #: instruction — a reasoning layer that can be talked into a broad task is an
    #: injection surface (OWASP LLM01).
    SYSTEM: str = ""
    #: The llm.ROUTES task this layer's call is routed as (picks its model tier).
    TASK: str | None = None

    #: Environment variable that kills this layer independently of the others.
    ENV_FLAG: str = "CAREROUTE_AGENT_REASONING"

    @property
    def enabled(self) -> bool:
        return _flag_enabled(self.ENV_FLAG)

    def build_prompt(self, state) -> str:  # pragma: no cover - abstract
        raise NotImplementedError(f"{type(self).__name__} must implement build_prompt()")

    def parse(self, data: dict) -> ReasoningOutcome | None:  # pragma: no cover - abstract
        raise NotImplementedError(f"{type(self).__name__} must implement parse()")

    async def reason(self, state) -> ReasoningOutcome | None:
        """Run the layer. Returns None on ANY failure — never raises.

        `None` is not an error state for the caller: it means "no additional
        signal", and the deterministic result stands. That is why every failure
        mode collapses to the same return value.
        """
        if not self.enabled or not self.SYSTEM.strip():
            return None
        try:
            # Called through the `llm` module (not a bare import) so the test
            # suite's single kill-switch — patching `llm.complete` — disables
            # every reasoning layer at once, exactly as it does for the workers.
            raw = await llm.complete(self.SYSTEM, self.build_prompt(state), json_mode=True, task=self.TASK)
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            return self.parse(data)
        except Exception:  # noqa: BLE001 - deliberately broad; the deterministic floor is authoritative
            # Deliberately broad: LLM down, timeout, invalid JSON, schema drift,
            # or a subclass bug must all degrade to the deterministic floor
            # rather than take down a triage case.
            return None

    # ------------------------------------------------------------------
    # Shared parsing helpers — so every subclass coerces the same way.
    # ------------------------------------------------------------------
    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1"}
        return bool(value)

    @staticmethod
    def _as_confidence(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _as_text(value: Any, limit: int = 240) -> str:
        text = str(value or "").strip()
        return text if len(text) <= limit else text[: limit - 3] + "..."
