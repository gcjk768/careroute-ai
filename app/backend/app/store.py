"""In-memory persistence for cases, escalations, and fairness metrics.

A real deployment would back this with a database; for this demo a simple
process-local store is sufficient and keeps the system dependency-free.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

from . import persistence
from .models import (
    Acuity,
    AcuityCode,
    CaseRecord,
    Citation,
    EscalationDetail,
    FairnessDrift,
    FairnessResponse,
    FairnessSubgroup,
)
from .redact import redact


def _utcnow() -> datetime:
    """The store's clock — one seam so tests can freeze time (SLA checks)."""
    return datetime.now(UTC)


def now_iso() -> str:
    return _utcnow().isoformat()


def sla_minutes() -> float:
    """[HITL] Minutes a clinician has to decide an escalation. Read per call so
    operators and tests can change it without a restart."""
    try:
        return float(os.environ.get("CAREROUTE_HITL_SLA_MINUTES", "15"))
    except ValueError:
        return 15.0


# [HITL] Patient-facing safety-netting shown while a P1/P2 review is overdue.
SAFETY_NET_ADVICE = (
    "A clinician has not reviewed your case yet. If your symptoms get worse, call 995 "
    "or go to the nearest Emergency Department now - do not wait for our reply."
)
_SAFETY_NET_ACUITY = frozenset({"P1_RESUSCITATION", "P2_EMERGENT"})


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp back to an aware datetime; on any malformed
    value return the epoch (aware) so it is treated as 'expired' (fail-closed)."""
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# [AI-Security] ASI06 (episodic-memory poisoning) hardening constants.
#
# The session id is an UNTRUSTED, client-supplied value: a caller can send any
# `sessionId` they like on /api/triage/stream, so it must be treated as an
# attacker-controlled key into episodic memory. We therefore:
#   - VALIDATE its shape (opaque token charset + bounded length) before it is
#     ever used as an index key, so it can't be used for injection / unbounded
#     memory growth;
#   - SEGMENT strictly by session id (a session only ever recalls its OWN
#     prior cases — never another session's);
#   - EXPIRE recalled cases with a TTL, so a stale or deliberately-planted old
#     case can't keep biasing a much later visit's reasoning;
#   - CAP the number of retained ids per session, so a spoofed id can't be used
#     to flood the store.
# --------------------------------------------------------------------------
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# Recalled prior cases older than this are ignored (continuity, not a permanent
# record — a visit weeks ago should not silently escalate today's triage).
_SESSION_TTL = timedelta(hours=24)
# Hard cap on ids retained per session (oldest dropped first).
_MAX_SESSION_CASES = 50


def memory_digest(case: CaseRecord) -> str:
    """[AI-Security][ASI06] SHA-256 over the fields episodic recall actually feeds
    back into later reasoning.

    Same tamper-EVIDENCE idea as the hash-chained audit log (`app/audit.py`), scoped
    to a different asset. `recall_session` replays a prior visit into the context of
    a new triage, so a record mutated after it was written becomes a *persistent*
    influence on future decisions — OWASP Agentic **ASI06, Memory & Context
    Poisoning**. The digest is taken at write time and re-checked at read time.

    Only decision-bearing fields are covered. Hashing the whole record would make
    the check fire on edits that cannot steer a later triage (a route instruction,
    a wait time) and a check that cries wolf is one that gets switched off.
    """
    payload = "|".join([
        case.caseId,
        case.sessionId or "",
        case.normalisedSymptoms,
        case.acuity.code,
        f"{case.confidence:.6f}",
        case.careTier,
        str(case.escalated),
        case.createdAt,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def valid_session_id(session_id: str | None) -> bool:
    """[AI-Security] True only for a well-formed, bounded, opaque session token.
    Rejects None/empty and anything outside the allow-listed charset/length so a
    spoofed or malformed `sessionId` never becomes an episodic-memory key."""
    return bool(session_id) and isinstance(session_id, str) and bool(_SESSION_ID_RE.match(session_id))


logger = logging.getLogger("careroute.store")

# [MLOps][HITL] Ground-truth label log (JSONL, one row per decided escalation).
# De-identified: acuity codes + agreement only, no symptom text. Override the
# location with CAREROUTE_GROUND_TRUTH_LOG.
_GROUND_TRUTH_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "monitoring", "ground_truth.jsonl")
)


def _record_ground_truth(escalation: EscalationDetail) -> None:
    """Append the clinician-labelled outcome for a decided escalation and bump
    the HITL agreement metric. Best-effort: never breaks the decision flow."""
    agreement = "unlabelled"
    if escalation.modelAgreed is not None:
        agreement = "agreed" if escalation.modelAgreed else "overridden"
    try:
        from . import metrics
        metrics.observe_hitl_decision(agreement)
    except Exception:
        logger.debug("HITL agreement metric not recorded", exc_info=True)
    try:
        path = os.environ.get("CAREROUTE_GROUND_TRUTH_LOG", _GROUND_TRUTH_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": escalation.decidedAt,
                "caseId": escalation.caseId,
                "modelAcuity": escalation.acuity.code,
                "clinicianAcuity": escalation.finalAcuity,
                "agreement": agreement,
                "decision": escalation.decision,
                # [XRAI] Accountability: WHO labelled this case. The dashboard
                # used to send a hard-coded name, so every row read the same.
                "clinician": escalation.clinician or "",
            }) + "\n")
    except Exception:  # noqa: BLE001 - best-effort persistence; the decision flow must not break
        logger.debug("ground-truth append skipped", exc_info=False)


def _learn_from_decision(escalation: EscalationDetail) -> None:
    """[Agentic][MLOps] Close the HITL loop: the labelled decision becomes a
    precedent (HITL memory) and, on disagreement, a retraining example.
    Best-effort: never breaks the decision flow."""
    try:
        from .memory import precedents, retrain_queue

        precedents.record_decision(escalation)
        retrain_queue.enqueue(escalation)
    except Exception:  # noqa: BLE001 - learning is advisory; the clinician's decision must stand
        logger.warning("precedent/retrain capture skipped", exc_info=False)


class Store:
    def __init__(self, redis_client=None) -> None:
        self._redis = redis_client
        self.cases: dict[str, CaseRecord] = {}
        self.escalations: dict[str, EscalationDetail] = {}
        # [Agentic] Episodic memory: ordered case ids per patient session, so a
        # returning session can be given continuity/recall of prior visits. A
        # SQL store (SQLite/Postgres) is the durable production drop-in; the
        # `recall_session` contract stays the same.
        self.session_index: dict[str, list[str]] = {}
        # [AI-Security][ASI06] caseId -> digest taken when the case entered
        # episodic memory. Held BESIDE the record rather than on it so a writer
        # who mutates the record does not also get to rewrite its own checksum,
        # and so CaseRecord's API schema is unchanged.
        self._memory_digests: dict[str, str] = {}
        # [HITL] Escalation ids already counted as SLA-breached (count once).
        self._sla_counted: set[str] = set()
        self._seed_escalations()
        # [Microservices] Reload what a previous gateway process decided.
        if self._redis is not None:
            self._load()

    # ------------------------------------------------------------------
    # Cases
    # ------------------------------------------------------------------
    def save_case(self, case: CaseRecord) -> None:
        # The case itself is always persisted (keyed by its trusted, server-
        # generated caseId). It is only added to EPISODIC memory (the per-session
        # index) when the client-supplied sessionId is well-formed -- a spoofed /
        # malformed sessionId therefore can't write into episodic memory at all.
        self.cases[case.caseId] = case
        persistence.write(self._redis, "hset", persistence.KEY_CASES, case.caseId, case.model_dump_json())
        if valid_session_id(case.sessionId):
            ids = self.session_index.setdefault(case.sessionId, [])
            ids.append(case.caseId)
            # [AI-Security][ASI06] Fingerprint the record as it enters episodic
            # memory, so `recall_session` can prove it is replaying what was
            # actually decided rather than what someone later wrote.
            self._memory_digests[case.caseId] = memory_digest(case)
            persistence.write(self._redis, "hset", persistence.KEY_DIGESTS, case.caseId,
                              self._memory_digests[case.caseId])
            # [AI-Security] Bound per-session retention so a spoofed id can't be
            # used to flood/poison the store; drop the oldest ids past the cap.
            if len(ids) > _MAX_SESSION_CASES:
                dropped = ids[:-_MAX_SESSION_CASES]
                for old in dropped:
                    self._memory_digests.pop(old, None)
                del ids[:-_MAX_SESSION_CASES]
                persistence.write(self._redis, "hdel", persistence.KEY_DIGESTS, *dropped)
            persistence.write(self._redis, "hset", persistence.KEY_SESSIONS, case.sessionId, json.dumps(ids))

    def get_case(self, case_id: str) -> CaseRecord | None:
        return self.cases.get(case_id)

    def recall_session(self, session_id: str, limit: int = 10) -> list[CaseRecord]:
        """[Agentic][AI-Security] Return THIS session's non-expired prior cases,
        most recent first.

        Hardened episodic recall: an invalid/spoofed session id recalls nothing,
        recall is scoped strictly to the given session, cases older than the TTL are
        excluded so stale/planted history can't bias current reasoning, and every
        record's integrity digest is re-checked before it is replayed.

        [ASI06] The integrity check FAILS CLOSED: a record whose digest does not
        match, or that has no digest at all, is dropped from recall rather than
        returned with a warning. "Absent is not clean" -- a missing digest and a
        broken one are the same evidence, that this record cannot be shown to be
        what was decided, and only one of the two readings is safe to act on.
        """
        if not valid_session_id(session_id):
            return []
        ids = self.session_index.get(session_id, [])
        cutoff = datetime.now(UTC) - _SESSION_TTL
        fresh: list[CaseRecord] = []
        for i in ids:
            case = self.cases.get(i)
            if case is None:
                continue
            if _parse_iso(case.createdAt) < cutoff:
                continue  # expired -- outside the episodic-memory window
            expected = self._memory_digests.get(i)
            if expected is None or expected != memory_digest(case):
                # Log the case id only. The record is suspect, so re-publishing
                # its contents into a log is the last thing this should do.
                logger.warning(
                    "episodic memory integrity check failed for case %s; excluded from recall", i
                )
                continue
            fresh.append(case)
        return list(reversed(fresh))[:limit]

    def verify_episodic_memory(self, session_id: str) -> dict:
        """[AI-Security][ASI06] Report the integrity of a session's episodic memory.

        Separated from `recall_session` because the two have different jobs: recall
        must silently exclude a suspect record so reasoning is never influenced by
        it, while an operator needs to know that something WAS excluded. A control
        that only fails quietly is indistinguishable from one that never fires.
        """
        ids = self.session_index.get(session_id, []) if valid_session_id(session_id) else []
        tampered = [
            i for i in ids
            if (case := self.cases.get(i)) is not None
            and self._memory_digests.get(i) != memory_digest(case)
        ]
        return {
            "sessionId": session_id,
            "tracked": len(ids),
            "tampered": tampered,
            "verified": not tampered,
        }

    # ------------------------------------------------------------------
    # Escalations
    # ------------------------------------------------------------------
    def create_escalation_from_case(self, case: CaseRecord, reason: str) -> EscalationDetail:
        escalation = EscalationDetail(
            id=new_id("esc"),
            caseId=case.caseId,
            reason=reason,
            confidence=case.confidence,
            acuity=case.acuity,
            createdAt=now_iso(),
            status="pending",
            patientSummary=case.normalisedSymptoms,
            rationale=case.rationale,
            citations=case.citations,
            evidence=case.evidence,
            normalisedSymptoms=case.normalisedSymptoms,
            language=case.language,
            # [Responsible-AI] Carry the classifier's SHAP-surrogate
            # explanation through to the escalation so a reviewing
            # clinician sees WHY the model reached this acuity, not just what.
            explanation=case.explanation,
            explanationSource=case.explanationSource,
            # [Agentic] Carry the Clinician-Handoff packet through as well —
            # the summary is what the reviewer reads FIRST, ahead of the raw
            # rationale/evidence fields above.
            handoffSummary=case.handoffSummary,
            handoffCitations=case.handoffCitations,
            handoffQuestions=case.handoffQuestions,
        )
        self.apply_sla(escalation)  # due time is known at creation; persisted with the record
        self.escalations[escalation.id] = escalation
        persistence.write(self._redis, "hset", persistence.KEY_ESCALATIONS, escalation.id,
                          escalation.model_dump_json())
        return escalation

    def list_escalations(self) -> list[EscalationDetail]:
        for e in self.escalations.values():
            self.apply_sla(e)
        self._observe_overdue()
        return sorted(self.escalations.values(), key=lambda e: e.createdAt, reverse=True)

    def get_escalation(self, escalation_id: str) -> EscalationDetail | None:
        escalation = self.escalations.get(escalation_id)
        return self.apply_sla(escalation) if escalation is not None else None

    def escalation_for_case(self, case_id: str) -> EscalationDetail | None:
        escalation = next((e for e in self.escalations.values() if e.caseId == case_id), None)
        return self.apply_sla(escalation) if escalation is not None else None

    # ------------------------------------------------------------------
    # [HITL] SLA — evaluated on read, no background thread
    # ------------------------------------------------------------------
    def apply_sla(self, escalation: EscalationDetail) -> EscalationDetail:
        """Stamp `slaDueAt` / `slaBreached`. A decided escalation is breached iff
        it was decided late; a pending one iff the clock is past due. Each
        escalation is counted once on careroute_hitl_sla_breached_total."""
        due = _parse_iso(escalation.createdAt) + timedelta(minutes=sla_minutes())
        end = _parse_iso(escalation.decidedAt) if escalation.decidedAt else _utcnow()
        escalation.slaDueAt = due.isoformat()
        escalation.slaBreached = end > due
        if escalation.slaBreached and escalation.id not in self._sla_counted:
            self._sla_counted.add(escalation.id)
            from . import metrics
            metrics.inc(metrics.HITL_SLA_BREACHED)
        return escalation

    def open_overdue(self) -> int:
        return sum(
            1 for e in self.escalations.values()
            if e.status == "pending" and self.apply_sla(e).slaBreached
        )

    def _observe_overdue(self) -> None:
        from . import metrics
        metrics.set_gauge(metrics.HITL_OPEN_OVERDUE, self.open_overdue())

    def patient_status(self, case_id: str) -> dict | None:
        """[HITL] What the PATIENT may see about their review: status only, no
        clinician note. Adds safety-netting when a P1/P2 review is overdue."""
        escalation = self.escalation_for_case(case_id)
        if escalation is None:
            return None if case_id not in self.cases else {
                "caseId": case_id, "escalated": False, "reviewStatus": None,
                "slaBreached": False, "safetyNet": None,
            }
        overdue = escalation.status == "pending" and escalation.slaBreached
        return {
            "caseId": case_id,
            "escalated": True,
            "reviewStatus": escalation.status,
            "slaBreached": escalation.slaBreached,
            "safetyNet": SAFETY_NET_ADVICE
            if overdue and escalation.acuity.code in _SAFETY_NET_ACUITY else None,
        }

    def decide_escalation(self, escalation_id: str, decision: str, note: str, clinician: str,
                          final_acuity: str | None = None) -> EscalationDetail | None:
        escalation = self.escalations.get(escalation_id)
        if escalation is None:
            return None
        escalation.status = "decided"
        escalation.decision = decision
        # [AI-Security] LLM02: a clinician's free-text note is persisted and
        # shown on the dashboard, so structured identifiers are masked exactly
        # as they are in patient text (SECURITY.md row LLM02).
        escalation.note = redact(note or "")[0]
        escalation.clinician = clinician
        escalation.decidedAt = now_iso()
        # [MLOps][HITL] Ground-truth capture: the clinician's final acuity IS
        # the label for this case. Record agreement vs the model's triage and
        # append the labelled pair to the ground-truth log — the live-accuracy
        # signal and the future retraining data that close the HITL loop.
        valid_codes = {c.value for c in AcuityCode}
        if final_acuity in valid_codes:
            escalation.finalAcuity = final_acuity
            escalation.modelAgreed = final_acuity == escalation.acuity.code
        _record_ground_truth(escalation)
        _learn_from_decision(escalation)
        self.apply_sla(escalation)
        self._observe_overdue()
        self.escalations[escalation_id] = escalation
        persistence.write(self._redis, "hset", persistence.KEY_ESCALATIONS, escalation_id,
                          escalation.model_dump_json())
        return escalation

    def _seed_escalations(self) -> None:
        seeds = [
            {
                "reason": "Safety-override triggered: possible acute coronary syndrome.",
                "confidence": 0.55,
                "acuity": Acuity.from_code("P1_RESUSCITATION"),
                "patientSummary": "58yo male, crushing central chest pain radiating to left arm, onset 20 min ago, sweating.",
                "rationale": "Chest pain with radiation and diaphoresis is a classic ACS presentation; safety-override forced P1 regardless of classifier confidence.",
                "evidence": ["chest pain", "radiating to left arm", "sweating", "onset 20 minutes ago"],
                "normalisedSymptoms": "Chest pain (central, crushing, radiating to left arm), diaphoresis, acute onset.",
                "language": "en",
                "explanation": [
                    {"feature": "chest pain", "weight": 0.95},
                    {"feature": "pain in my left arm", "weight": 0.8},
                    {"feature": "safety_override:cardiac_chest_pain", "weight": 1.0},
                ],
            },
            {
                "reason": "Low classifier confidence (0.42) on ambiguous presentation.",
                "confidence": 0.42,
                "acuity": Acuity.from_code("P3_URGENT"),
                "patientSummary": "34yo female, intermittent abdominal pain for 3 days, low-grade fever, unsure of severity.",
                "rationale": "Symptom description was vague and self-contradictory; model confidence fell below the 0.6 escalation threshold, routed to clinician for review.",
                "evidence": ["abdominal pain 3 days", "low-grade fever", "vague severity description"],
                "normalisedSymptoms": "Intermittent lower abdominal pain, 3 days duration, low-grade fever.",
                "language": "en",
                "explanation": [
                    {"feature": "fever", "weight": 0.3},
                    {"feature": "persistent", "weight": 0.25},
                    {"feature": "vague severity description", "weight": -0.2},
                ],
            },
            {
                "reason": "Safety-override triggered: suicidal ideation disclosed.",
                "confidence": 0.61,
                "acuity": Acuity.from_code("P2_EMERGENT"),
                "patientSummary": "22yo, reports feeling hopeless and states thoughts of self-harm this week.",
                "rationale": "Direct disclosure of self-harm ideation triggers mandatory human-in-the-loop review regardless of confidence.",
                "evidence": ["thoughts of self-harm", "feeling hopeless", "this week"],
                "normalisedSymptoms": "Reports low mood, hopelessness, and passive self-harm ideation over the past week.",
                "language": "en",
                "explanation": [
                    {"feature": "suicidal", "weight": 0.85},
                    {"feature": "safety_override:suicidal_ideation", "weight": 1.0},
                ],
            },
            {
                "reason": "Low classifier confidence (0.48) — conflicting symptom signals.",
                "confidence": 0.48,
                "acuity": Acuity.from_code("P4_NON_URGENT"),
                "patientSummary": "45yo, mild dizziness on standing for 2 days, otherwise well, on new blood pressure medication.",
                "rationale": "Dizziness could be benign orthostatic response to new medication or an early sign of something more serious; model was not confident enough to route to self-care automatically.",
                "evidence": ["mild dizziness on standing", "new BP medication", "2 days duration"],
                "normalisedSymptoms": "Postural dizziness for 2 days, recent new antihypertensive medication, no other symptoms.",
                "language": "en",
                "explanation": [
                    {"feature": "mild", "weight": -0.4},
                    {"feature": "new BP medication (unfamiliar context)", "weight": 0.15},
                ],
            },
        ]

        for seed in seeds:
            case_id = new_id("case")
            escalation = EscalationDetail(
                id=new_id("esc"),
                caseId=case_id,
                reason=seed["reason"],
                confidence=seed["confidence"],
                acuity=seed["acuity"],
                createdAt=now_iso(),
                status="pending",
                patientSummary=seed["patientSummary"],
                rationale=seed["rationale"],
                citations=[
                    Citation(
                        title="Clinical Guidance Reference",
                        snippet="Seeded example escalation for demo purposes.",
                        source="CareRoute AI Seed Data",
                    )
                ],
                evidence=seed["evidence"],
                normalisedSymptoms=seed["normalisedSymptoms"],
                language=seed["language"],
                explanation=seed.get("explanation", []),
            )
            self.escalations[escalation.id] = escalation

    def _load(self) -> None:
        """Replace the in-memory state with what Redis holds. On a first start
        Redis has no escalations, so the demo seeds are written INTO it once;
        after that the seeds come back from Redis instead of being re-minted
        with new ids on every restart."""
        r = self._redis
        try:
            escalations = r.hgetall(persistence.KEY_ESCALATIONS)
            cases = r.hgetall(persistence.KEY_CASES)
            sessions = r.hgetall(persistence.KEY_SESSIONS)
            digests = r.hgetall(persistence.KEY_DIGESTS)
        except Exception:  # noqa: BLE001 - start serving from memory; writes will retry per call
            logger.warning("store: redis unavailable at start-up; serving from memory", exc_info=False)
            return
        if escalations:
            self.escalations = {k: EscalationDetail.model_validate_json(v) for k, v in escalations.items()}
        else:
            for escalation in self.escalations.values():
                persistence.write(r, "hset", persistence.KEY_ESCALATIONS, escalation.id,
                                  escalation.model_dump_json())
        self.cases = {k: CaseRecord.model_validate_json(v) for k, v in cases.items()}
        self.session_index = {k: json.loads(v) for k, v in sessions.items()}
        self._memory_digests = dict(digests)


def build_fairness_response() -> FairnessResponse:
    """Realistic, hand-seeded fairness/monitoring snapshot."""
    return FairnessResponse(
        overallAccuracy=0.887,
        redFlagRecall=0.964,
        fairnessGapBefore=0.146,
        fairnessGapAfter=0.052,
        subgroups=[
            FairnessSubgroup(name="Age 0-17", accuracy=0.852, n=214),
            FairnessSubgroup(name="Age 18-39", accuracy=0.901, n=612),
            FairnessSubgroup(name="Age 40-64", accuracy=0.894, n=548),
            FairnessSubgroup(name="Age 65+", accuracy=0.861, n=331),
            FairnessSubgroup(name="Sex: Female", accuracy=0.892, n=863),
            FairnessSubgroup(name="Sex: Male", accuracy=0.881, n=842),
        ],
        drift=FairnessDrift(data=0.031, target=0.018, concept=0.044),
        modelVersion="careroute-triage-v1.3.0",
        updatedAt=now_iso(),
        demographicParity={
            "byGroup": {"65+ · Female": 0.214, "18-39 · Male": 0.181},
            "statisticalParityDifference": 0.051,
        },
        equalOpportunity={
            "byGroup": {"65+ · Female": 0.94, "18-39 · Male": 0.97},
            "equalOpportunityGap": 0.03,
        },
        counterfactual={"attribute": "sex", "n": 10, "sexFlipRate": 0.0, "meanAcuityDelta": 0.0},
    )


store = Store(redis_client=persistence.connect())
