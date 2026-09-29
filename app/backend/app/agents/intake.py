"""Symptom-Intake worker.

OWNER: Sham Goh

Collects symptoms multilingually (voice-capable), normalises them, and
populates the case state for the downstream agents.

WHAT THIS FILE IS
-----------------
Step 1 of the pipeline. Everything after it — the Severity-Classifier's feature
extraction, Safety-Override's red-flag scan, the cross-visit escalation rule —
reads what this agent writes, so a symptom dropped here is a symptom the rest of
the system never sees.

Edit only THIS file (and this agent's tests in tests/agents/test_intake.py).
The shared CaseState / contract lives in base.py (platform-owned).

WHAT THIS AGENT MUST SATISFY
----------------------------
1. CONTRACT — mutate ONLY the CaseState fields in `CONTRACT.writes`, and return
   a dict containing exactly the keys in `CONTRACT.returns`.
   tests/agents/test_contracts.py snapshots the state and fails the build on any
   step outside the lane.
2. COMMS — `emit()` is pure plumbing over the fields `run()` sets. Whatever
   `run()` writes is what the Severity-Classifier receives.
3. TOOL_ALLOWLIST — `llm.complete` and nothing else, called via the module
   (`from .. import llm`), NOT as a bare `from ..llm import complete`: the test
   suite's kill-switch patches `llm.complete`, and a bare import escapes it.
4. A DETERMINISTIC FALLBACK IS MANDATORY. The whole suite runs with the LLM
   patched out, so `_fallback()` is the path almost every test exercises.

DESIGN DECISIONS — the deterministic path
-----------------------------------------
The docstring's open questions, answered. These are the choices E1 measures, so
they are recorded here rather than left implicit.

- IT CLEANS, IT DOES NOT SUMMARISE. `normalised_symptoms` is the *cleaned
  original utterance*, never a rewritten or re-worded one. Rewriting is where a
  deterministic path would either drop a symptom or invent one, and E1 sets
  clinical-content recall at >=0.95 and hallucination rate at exactly 0.0 — a
  cleaning-only transform satisfies both by construction.

- IT DOES NOT TRANSLATE. This is the known, deliberate gap: `redflags.py` is an
  English-only literal regex table, so with the LLM unavailable a Spanish or
  Chinese emergency matches ZERO deterministic rules. Covering that is exactly
  what Safety-Override's `SemanticRedFlagLayer` exists for (see
  docs/vault/Agent Capability Audit.md), and translating here would quietly
  remove the rationale for that layer. E1 therefore scores the two paths
  separately. Raw text still reaches Safety-Override untouched — the classifier
  and the red-flag scan both read `normalised_symptoms + raw_text` — so cleaning
  never costs the pipeline information it would otherwise have had.

- LANGUAGE DETECTION IS HEURISTIC, in three tiers: script is decisive (CJK ->
  zh, Tamil -> ta); otherwise `_LANGUAGE_HINTS` marker-word counts decide; the
  declared `state.language` is only a tie-breaker, because it may be absent or
  wrong. KNOWN LIMITATION: `CaseState.language` defaults to "en", so an unset
  declaration is indistinguishable from a declared English one — which is why
  strong marker evidence (>=2 hits) is allowed to override it.

- TAMIL IS SUPPORTED BECAUSE THIS IS A SINGAPORE SERVICE. The evaluation plan
  named en/es/zh/ms/fr, which covers two languages barely spoken here and omits
  one of the country's four official ones. Spanish and French are kept — they
  cost nothing and demonstrate the method generalises — but Tamil is the one a
  real patient at a Singapore polyclinic is likely to type. Detection is exact
  (own Unicode block); extraction still routes through the LLM path, same as
  every other non-English input. E1's dataset must therefore cover ta as well.

- VOICE INPUT IS HANDLED, narrowly: `state.is_voice` strips transcription
  disfluencies and stutter-repeats only. Nothing that could carry clinical
  meaning is removed (e.g. "like" is left alone — "feels like" is a symptom
  description, not a filler).

- KEYWORDS come from `_SYMPTOM_VOCABULARY` below, an English surface-form table
  ordered most-severe-first so that truncating to MAX_KEYWORDS can never drop
  chest pain in favour of a cough. Every keyword must be evidenced in the input;
  nothing is inferred. The vocabulary is deliberately IN THIS FILE rather than
  imported from `ml/features.py` — that module is the Severity-Classifier's lane
  (README ownership table lists this agent as self-contained).

Evaluation E1 (tests/test_eval_intake.py, gold set tests/fixtures/intake_gold.json,
44 rows across en/es/zh/ms/fr/ta plus voice noise) scores this agent on both paths
separately. Its first run found four defects in THIS file, all now fixed at root:

  * the vocabulary matched clinical shorthand, not how people write. An inserted
    copula ("my throat IS closing") or an expanded contraction ("it WILL NOT stop
    bleeding") defeated the match entirely, and three P1 presentations extracted
    nothing;
  * on the LLM path the translations were correct every time — "a strong pain in
    my chest", "my chest hurts a lot", "difficulty breathing" — and this table
    missed all of them, so measured quality was tracking the model's word choice
    rather than its accuracy;
  * "my head hurts" / "my stomach hurts", the commonest plain-English phrasings,
    were absent;
  * the LLM trigger was too greedy (see `run()`).

Bars were never lowered to make it pass. That is the point of the evaluation.
"""

# NOTE for Sham Goh (Symptom-Intake + orchestration owner) — added 2026-09-25 by James.
# What improved around this agent in the 24-25 Sep live-test fixes (details in
# orchestration.py's note and docs/vault/Changelog.md):
# - Untranslated input: when this agent cannot read the complaint (non-English,
#   LLM down) the case is floored at P3 and escalated, and `state.unreadable_input`
#   now stops Reflection bumping it again (a Malay/Tamil mild cough went to P2).
# - The guardrail that runs BEFORE this agent now also decodes disguised
#   injections (ciphers, base64, look-alike letters, spacing): PyRIT bypass rate
#   82.9% -> 6.1%. The text you receive is still exactly what the patient typed.
# Your code in this file is unchanged. Tests: tests/test_pipeline.py.
from __future__ import annotations

import json
import re
import unicodedata
from typing import ClassVar

from .. import llm  # module import on purpose — see constraint 3 above
from .base import EMBEDDED_INSTRUCTION_GUARD, AgentContract, CaseState, enforce_tool_access
from .capability import ORCHESTRATOR, AgentCapability
from .messaging import AgentComms, AgentMessage, ConsumesMessages, enforce_comms
from .orchestration import (
    ORCHESTRATOR_PUBLISHES,
    ORCHESTRATOR_SUBSCRIBES,
    ORCHESTRATOR_TOOLS,
    PipelineOrchestrator,
)

OWNER = "Sham Goh"

#: Hard cap on emitted keywords. Mirrors CAPABILITY.action_space ("<=6") — the
#: classifier uses the first few as clinician-facing evidence, so a long tail of
#: weak matches would dilute the explanation rather than improve it.
MAX_KEYWORDS = 6

#: Language used when nothing else is known. The red-flag table and the
#: classifier's keyword features are English, so English is the safe default.
_DEFAULT_LANGUAGE = "en"

#: Marker words per language, used to detect the language the patient actually
#: WROTE IN (which may differ from `state.language`). Deliberately common
#: function words and body/symptom nouns rather than a full model: this path must
#: run with zero network calls and stay auditable.
#:
#: Referenced by name from app/evals/plan.E1_INTAKE_EXTRACTION — the E1 gold
#: dataset must cover every language declared here.
#: Matched against accent-folded lowercase text, so entries are unaccented.
_LANGUAGE_HINTS: dict[str, tuple[str, ...]] = {
    # English first: ties resolve to the default rather than to a coincidence.
    "en": ("i have", "have", "my", "and", "the", "for", "with", "days", "feel", "not", "is", "pain"),
    "es": ("tengo", "dolor", "fiebre", "mucho", "muy", "me duele", "no puedo", "respirar",
           "cabeza", "garganta", "estoy", "dias", "desde"),
    "fr": ("j'ai", "douleur", "fievre", "mal a", "tete", "gorge", "depuis", "respirer",
           "je ne peux pas", "jours", "tres", "je suis"),
    "ms": ("saya", "sakit", "demam", "kepala", "tekak", "batuk", "tidak boleh", "bernafas",
           "hari", "sangat", "dada", "susah"),
    # Chinese and Tamil are detected by SCRIPT (below); their markers exist so the
    # table names every language E1 covers, and so a romanised edge case
    # ("saya sakit" typed by a Tamil speaker, "wo tou tong") still has some signal.
    "zh": ("痛", "疼", "发烧", "發燒", "呼吸", "头痛", "頭痛", "咳嗽", "胸口", "不能"),
    "ta": ("வலி", "காய்ச்சல்", "தலைவலி", "மூச்சு", "இருமல்", "வயிறு", "நெஞ்சு", "எனக்கு"),
}

#: CJK unified ideographs. Script is decisive evidence — no marker-word count can
#: outweigh it, and no other language in the table uses this range.
_CJK = re.compile(r"[一-鿿]")

#: Tamil block (U+0B80–U+0BFF). Tamil is one of Singapore's FOUR official
#: languages, so a Singapore triage service that cannot read it has a real
#: coverage hole — not a hypothetical one. Like CJK it has its own script, which
#: makes detection exact rather than probabilistic: no marker-word count, no
#: ambiguity with the Latin-script languages, and no dependence on the declared
#: `state.language`.
#:
#: Note the asymmetry this leaves: detection is exact, but `_SYMPTOM_VOCABULARY`
#: is English-only, so the deterministic path detects `ta` and extracts ZERO
#: keywords — which is precisely the empty-keyword condition `run()` uses to
#: reach the LLM. Tamil therefore routes to the translating path by design.
_TAMIL = re.compile(r"[஀-௿]")

#: Marker hits needed before detection overrides the DECLARED language. Two
#: independent markers is weak evidence in absolute terms but strong relative to
#: a declaration that defaults to "en" whether or not the patient set it.
_MIN_HINTS_TO_OVERRIDE_DECLARED = 2

#: Transcription disfluencies removed only when `state.is_voice`. Kept short and
#: unambiguous on purpose: anything that could carry clinical meaning stays.
_VOICE_FILLERS: tuple[str, ...] = (
    "um", "umm", "uh", "uhh", "erm", "er", "ah", "eh", "hmm", "mm", "mmm",
    "you know", "i mean",
)

#: Canonical keyword -> surface forms that evidence it, ordered MOST SEVERE
#: FIRST so truncation to MAX_KEYWORDS drops the least clinically salient match.
#: Surface forms are matched with a left word boundary and an open right edge, so
#: "cough" also matches "coughing" and "vomit" also matches "vomiting" without
#: needing every inflection listed. Bare fragments that would over-match (e.g.
#: "cut", which would fire on "cutlery") are excluded rather than relied on.
_SYMPTOM_VOCABULARY: tuple[tuple[str, tuple[str, ...]], ...] = (
    # E1 finding (LLM path): the model translates correctly into NATURAL English —
    # "a strong pain in my chest", "my chest hurts a lot", "my chest feels tight" —
    # and every one of those missed a table written in clinical shorthand. The
    # translation was never the weak link; this table was. Extraction quality was
    # tracking the model's word choice, which varies between runs.
    ("chest pain", ("chest pain", "chest tightness", "chest pressure", "tight chest",
                    "chest feels tight", "crushing chest", "chest hurt", "chest is hurt",
                    "pain in my chest", "pain in the chest")),
    ("breathlessness", ("can't breathe", "cant breathe", "cannot breathe", "breathless",
                        "shortness of breath", "short of breath", "difficulty breathing",
                        "trouble breathing", "hard to breathe", "struggling to breathe",
                        "gasping")),
    # E1 finding: patients write "my face IS drooping", not "face droop", and
    # "my speech IS slurred", not "slurred speech". An inserted copula defeated
    # the whole match and three P1 presentations extracted nothing. The forms
    # below are the ones real phrasing produces, not the clinical shorthand.
    ("stroke signs", ("face droop", "facial droop", "face is droop", "slurred speech",
                      "speech is slurred", "speech was slurred", "sudden confusion",
                      "weakness one side", "numb arm")),
    # E1 finding: "won't stop bleeding" was listed in both contracted spellings
    # but not EXPANDED — and "it will not stop bleeding" is how it gets typed.
    ("severe bleeding", ("severe bleeding", "heavy bleeding", "won't stop bleeding",
                         "wont stop bleeding", "will not stop bleeding", "not stop bleeding",
                         "coughing up blood", "vomiting blood")),
    # E1 finding: same copula problem — "my throat IS closing" is the natural
    # phrasing for the single most time-critical presentation in the table.
    ("allergic reaction", ("anaphylaxis", "throat closing", "throat is closing",
                           "throat swelling", "throat is swelling", "tongue swelling",
                           "tongue is swelling", "epipen", "allergic reaction")),
    ("self-harm risk", ("suicid", "kill myself", "end my life", "self harm", "self-harm",
                        "want to die", "hurt myself")),
    ("seizure", ("seizure", "convulsion", "having a fit", "had a fit", "having fits")),
    ("severe pain", ("severe pain", "worst pain", "excruciating", "unbearable pain")),
    ("fever", ("fever", "feverish", "high temperature", "burning up")),
    # Nausea is kept SEPARATE from vomiting. `ml/features.py` groups them into one
    # model feature, which is fine for a feature name — but these keywords are
    # surfaced to the clinician as evidence by the classifier, and reporting
    # "vomiting" for a patient who only said "nauseous" asserts a symptom they
    # did not report. E1 scores that as a hallucination, correctly.
    ("vomiting", ("vomit", "throwing up")),
    ("nausea", ("nausea", "nauseous")),
    ("dehydration", ("dehydrat", "can't keep fluids", "cant keep fluids", "not drinking")),
    # E1 finding: "my stomach hurts" and "my head hurts" are the commonest
    # plain-English phrasings of these two and neither was listed — the table had
    # only the compound-noun forms ("stomach pain", "headache").
    ("abdominal pain", ("abdominal pain", "stomach pain", "stomach ache", "stomach hurt",
                        "belly pain", "tummy pain", "belly hurt", "tummy hurt")),
    ("headache", ("headache", "migraine", "head hurt", "my head is hurting")),
    ("dizziness", ("dizzy", "dizziness", "lightheaded", "light-headed")),
    ("cough", ("cough",)),
    ("sore throat", ("sore throat", "throat hurts", "scratchy throat")),
    ("rash", ("rash", "itchy skin", "hives")),
    ("cold symptoms", ("runny nose", "blocked nose", "congestion", "sneezing", "common cold")),
    ("minor wound", ("minor cut", "small cut", "laceration", "graze", "scrape", "blister")),
    ("bruising", ("bruise", "bruising", "contusion")),
    ("sprain or strain", ("sprain", "twisted my ankle", "twisted my knee", "pulled muscle",
                          "pulled a muscle", "strained")),
    ("persistent or worsening", ("for days", "several days", "persistent", "worsening",
                                 "getting worse", "not improving")),
)


#: Longest LLM reply accepted. A normalised complaint is one or two short
#: sentences; anything longer means the model started explaining or diagnosing,
#: so the reply is discarded and the deterministic result stands.
_MAX_LLM_CHARS = 600

#: The LLM is given ONE job: restate the patient's words in plain English.
#: It is never asked to name symptoms, rate severity or diagnose — those stay
#: deterministic, so the model has no output channel through which to invent a
#: symptom. The last line is prompt-injection defence (OWASP LLM01).
# NOTE for Sham (Symptom-Intake owner) — changed 2026-09-16 by James, courseware audit.
# WHY: OWASP LLM01 mitigation #1 needs the SAME embedded-instruction guard in every
# LLM prompt, asserted by tests/agents/test_prompt_hygiene.py. Only the guard
# sentence (agents.base.EMBEDDED_INSTRUCTION_GUARD) and the SYSTEM_PROMPT alias were
# added; nothing else in this prompt or agent changed. Reword freely as long as the
# shared sentence stays in SYSTEM_PROMPT.
_LLM_SYSTEM = (
    "You are a clinical text normaliser for a triage system. Translate the patient's words into "
    "plain English and correct obvious misspellings. Do NOT diagnose. Do NOT add, infer or "
    "reword any symptom the patient did not state. "
    + EMBEDDED_INSTRUCTION_GUARD
)
# Public name every LLM-calling agent module exposes (tests/agents/test_prompt_hygiene.py).
SYSTEM_PROMPT = _LLM_SYSTEM


def _llm_prompt(raw_text: str) -> str:
    return (
        f'Patient\'s own words: "{raw_text}"\n\n'
        "Return JSON with a single key: english — the same complaint in plain English, one or "
        "two short sentences, containing nothing the patient did not say."
    )


def _fold(text: str) -> str:
    """Lowercase and strip accents, so "fièvre" and "fievre" match one entry."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


#: Surface forms that are whole words of their own (an open right edge would
#: read a longer word as the symptom). Empty today: "fitting" left the table
#: because "fitting room" is not a seizure either way.
_RIGHT_BOUNDED: frozenset[str] = frozenset()


def _contains(folded: str, phrase: str) -> bool:
    """Left-boundary match with an open right edge — see _SYMPTOM_VOCABULARY.
    A form that is denied in its clause ("no chest pain, just a cough") is not
    evidence of the symptom; the denial rule is the red-flag table's."""
    from .. import redflags

    pattern = r"(?<!\w)" + re.escape(phrase) + (r"(?!\w)" if phrase in _RIGHT_BOUNDED else "")
    return any(
        not redflags.is_denied(folded, m.start(), m.end())
        for m in re.finditer(pattern, folded)
    )


def _hint_score(folded: str, hints: tuple[str, ...]) -> int:
    """How many of `hints` appear. CJK entries have no word boundaries, so they
    are matched as plain substrings."""
    score = 0
    for hint in hints:
        if _CJK.search(hint):
            if hint in folded:
                score += 1
        elif _contains(folded, hint):
            score += 1
    return score


def _strip_disfluencies(text: str) -> str:
    """Remove voice-transcription noise: filler tokens and stutter-repeats."""
    for filler in _VOICE_FILLERS:
        text = re.sub(rf"(?<!\w){re.escape(filler)}(?!\w)[,\s]*", " ", text, flags=re.IGNORECASE)
    # "I I have" / "the the pain" -> collapse the immediate repeat.
    text = re.sub(r"(?<!\w)(\w+)(\s+\1)+(?!\w)", r"\1", text, flags=re.IGNORECASE)
    return text


#: Short answers that mean "yes" / "no" to a template question. Anything else is
#: folded as the patient's own words.
_YES_ANSWERS = frozenset({
    "yes", "yes.", "y", "yeah", "yep", "ya", "yah", "correct", "i do", "yes i do", "i have",
    "yes i have", "true", "ok yes", "yes, i do", "yes, i have",
})
_NO_ANSWERS = frozenset({
    "no", "no.", "n", "nope", "nah", "i don't", "i dont", "no i don't", "no i dont", "none",
    "not really", "no, i don't", "no, i dont", "i do not", "no i do not", "false",
})
_UNSURE_ANSWERS = frozenset({
    "not sure", "unsure", "i'm not sure", "im not sure", "don't know", "dont know",
    "i don't know", "i dont know", "no idea", "skip", "maybe", "n/a", "na", "-",
})
_MAX_ANSWER_CHARS = 300


def fold_clarifications(text: str, clarifications: list[dict]) -> str:
    """Append the interview answers to the normalised text as symptom clauses.

    A bare "yes" to a template question is turned into the statement the
    question asked about ("I also have fever, difficulty breathing"), because
    the downstream readers match SYMPTOM words. A bare "no" or "not sure" adds
    NOTHING to the text: a denied symptom is a zero feature exactly like an
    unmentioned one, and writing "no chest pain" into the text is what the
    red-flag regexes — which do not all read negation — would fire on. The
    denial still counts: the classifier reads the transcript and will not ask
    about that symptom again. Anything else — a free-text answer, or an answer
    to an onset / duration / severity question — is appended in the patient's
    own words, on the same footing as the complaint. Each answer is its own
    clause (". "), so a denial in one answer cannot reach into the next.

    Pure and deterministic: the same complaint plus the same transcript always
    yields the same text, which is what keeps a resumed turn replayable.
    """
    parts = [str(text or "").strip()]
    for item in clarifications or []:
        if not isinstance(item, dict):
            continue
        answer = re.sub(r"\s+", " ", str(item.get("answer") or "")).strip()[:_MAX_ANSWER_CHARS]
        if not answer:
            continue
        statement = re.sub(r"\s+", " ", str(item.get("statement") or "")).strip()
        lowered = answer.lower().rstrip("!").strip()
        if lowered in _NO_ANSWERS or lowered in _UNSURE_ANSWERS:
            continue
        if lowered in _YES_ANSWERS:
            if not statement:
                continue  # "yes" to an open question carries no symptom words
            clause = f"I also have {statement}"
        else:
            clause = answer
        parts.append(clause)
    folded = ". ".join(p.rstrip(".") for p in parts if p)
    return (folded + ".") if len(parts) > 1 and folded else folded


def _clean_text(raw: str, is_voice: bool) -> str:
    """Whitespace/punctuation hygiene + optional disfluency removal.

    Cleaning ONLY: no word is substituted, reordered or translated, so the output
    cannot assert a symptom the input did not contain.
    """
    text = unicodedata.normalize("NFKC", raw or "").replace("’", "'")
    text = re.sub(r"\s+", " ", text).strip()
    if is_voice:
        text = _strip_disfluencies(text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)      # " ," -> ","
    text = re.sub(r"([,.;:!?])(?=\w)", r"\1 ", text)  # ",word" -> ", word"
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        # Nothing survived. Fabricating a sentence here would be a hallucination
        # in the one case where we know least — leave it empty and let the
        # classifier fall back to raw_text.
        return ""
    if text[0].islower():
        text = text[0].upper() + text[1:]
    if text[-1] not in ".!?\u3002\uff01\uff1f":  # . ! ? and the CJK full stop / marks
        text += "."
    return text


def _detect_language(text: str, declared: str) -> str:
    """Three tiers: script -> marker words -> the declared language."""
    if _CJK.search(text):
        return "zh"
    if _TAMIL.search(text):
        return "ta"
    folded = _fold(text)
    best_lang, best_score = _DEFAULT_LANGUAGE, 0
    for lang, hints in _LANGUAGE_HINTS.items():
        score = _hint_score(folded, hints)
        if score > best_score:   # strict > keeps the dict's order as the tie-break
            best_lang, best_score = lang, score
    if best_score >= _MIN_HINTS_TO_OVERRIDE_DECLARED:
        return best_lang
    declared = (declared or "").strip().lower()[:2]
    if declared in _LANGUAGE_HINTS:
        return declared
    return best_lang if best_score else _DEFAULT_LANGUAGE


def _extract_keywords(text: str) -> list[str]:
    """Canonical keywords evidenced in `text`, most severe first, capped."""
    folded = _fold(text)
    found = [
        canonical
        for canonical, surface_forms in _SYMPTOM_VOCABULARY
        if any(_contains(folded, form) for form in surface_forms)
    ]
    return found[:MAX_KEYWORDS]


class SymptomIntakeAgent(ConsumesMessages, PipelineOrchestrator):
    """Normalises raw patient input into structured symptom text, AND orchestrates
    the pipeline that consumes it.

    TWO ROLES, DELIBERATELY. This agent is both:

      1. the first WORKER — `run()` normalises the patient's text, exactly as
         before, still bound by `CONTRACT` to its three fields; and
      2. the ORCHESTRATOR — `orchestrate()` (inherited from
         `PipelineOrchestrator`) drives all six workers in the fixed,
         safety-gated order and is the single entry point the API calls.

    The two do not bleed into each other. `run()` is unchanged and still writes
    only its declared lane, so `test_contracts.py` holds; the sequencing lives in
    `orchestrate()`, which mutates nothing itself and only calls workers. Keeping
    them separate is what lets one class hold both roles honestly.

    WHY IT IS ALSO THE ORCHESTRATOR. Previously a separate platform-owned
    `Supervisor` sequenced the workers. The team moved the role here so every
    agent is called through intake. `supervisor.py` remains as a deprecated
    alias so existing imports keep working — see its module docstring.

    THE COST, STATED PLAINLY. A safety-gated order that the orchestrator could
    freely reorder would not be safety-gated. So its discretion is bounded: the
    steps AFTER Safety follow a per-case plan (`planner.py`, Plan-and-Execute),
    but only a plan that passes the allowed-transition graph runs — Safety has
    already run, Reflection and Handoff are mandatory, and anything else falls
    back to the fixed sequence. And an agent that both produces input and decides who consumes it
    has no independent party between those two jobs — the isolation that catches
    an intake defect downstream is now weaker by exactly one hop. `CONTRACT`
    enforcement and the safety gate are unchanged and still mechanical.
    """

    SLUG = "intake"

    # [AI-Security][Agentic] FR-12 least-privilege allow-list + Spectrum of
    # Agency autonomy level. Adding a tool here is a security decision, not a
    # convenience — justify it in review.
    #
    # `route` and `aggregate` are the orchestration pseudo-tools, added when this
    # agent took the orchestrator role. They are not real external calls: the
    # engine delegates every piece of reasoning to a worker and never touches the
    # LLM, RAG, the clinic directory or the red-flag rules on its own behalf.
    TOOL_ALLOWLIST: ClassVar[list[str]] = ["llm.complete", *ORCHESTRATOR_TOOLS]
    AUTONOMY_LEVEL: int = 3  # L3: orchestrates other agents within a fixed pipeline
    PROMPT_PATTERN: str = "Structured JSON extraction prompt; explicit 'do not diagnose' instruction."

    # [Point 1] Machine-checked capability declaration — see agents/capability.py.
    # ORCHESTRATOR is declared, never derived: `classify()` cannot infer it, and
    # `enforce_capability` admits it only from the agent named in
    # capability.ORCHESTRATOR_SLUG. Both halves of this agent are described
    # below, because a declaration that mentioned only one would be dishonest.
    CAPABILITY = AgentCapability(
        reasoning=(
            "Reads free-text patient input in an unknown language and generates a normalised "
            "clinical sentence, a language code and a keyword set — an open-ended extraction, "
            "not a lookup. Orchestration itself involves no inference: the step order is fixed."
        ),
        action_space=(
            "any normalised clinical sentence from an open output space",
            "any subset of symptom keywords (<=6) it judges clinically salient",
            "any ISO 639-1 language code",
            "deterministic fallback extraction when the LLM is unavailable",
            "run the fixed, safety-gated worker sequence",
        ),
        memory=(
            "None of its own for extraction — each utterance is normalised independently. As "
            "orchestrator it reads prior-visit context via store.recall_session and applies the "
            "deterministic cross-visit rule, but holds no state between cases."
        ),
        tools=("llm.complete", *ORCHESTRATOR_TOOLS),
        uses_trained_model=False,
        classification=ORCHESTRATOR,
        justification=(
            "Both an agent and the workflow. Its extraction half is unambiguously agentic: no "
            "rule table could produce that output, the result is not a function of the input "
            "alone, and the space of correct answers is open. Its orchestration half decides "
            "nothing about the case — the order intake -> classifier -> safety -> routing -> "
            "hitl -> reflection is fixed in code and not chosen at runtime, deliberately, "
            "because an order an LLM could reorder would not be safety-gated. ORCHESTRATOR is "
            "the honest classification because AGENT would hide the sequencing role and "
            "POLICY_NODE would deny the extraction one."
        ),
    )

    # [Platform] Isolation lane — see AgentContract in base.py.
    # UNCHANGED by the orchestrator role. `run()` still writes only these three
    # fields; everything else on CaseState is written by the worker that owns it,
    # even though this agent is now the one calling that worker.
    CONTRACT = AgentContract(
        writes=frozenset({"normalised_symptoms", "detected_language", "intake_keywords"}),
        returns=frozenset({"source", "normalised_symptoms", "detected_language", "keywords"}),
    )

    # [A2A] This agent's communication interface — see agents/messaging.py.
    # Two halves, unioned: the worker's own handoff (`symptoms.normalised` out,
    # `case.opened` in) plus the intents the orchestration engine emits and
    # consumes. The orchestrator sets are imported from orchestration.py rather
    # than retyped, so a new message there cannot silently go undeclared here.
    COMMS = AgentComms(
        publishes=frozenset({"symptoms.normalised"}) | ORCHESTRATOR_PUBLISHES,
        subscribes=frozenset({"case.opened"}) | ORCHESTRATOR_SUBSCRIBES,
    )

    def __init__(self, **workers) -> None:
        # Builds the other six workers and points `self.intake` at this object.
        # The keyword pass-through is what lets `new_session()` hand a REQUEST
        # its own worker set while the expensive shared deps travel down with
        # them — see PipelineOrchestrator.__init__ for why that matters.
        PipelineOrchestrator.__init__(self, **workers)

    def emit(self, state: CaseState) -> AgentMessage:
        """Build this agent's outgoing A2A message from the post-run state.

        Already implemented — plumbing, not logic. It reads exactly the three
        fields `run()` is responsible for setting.
        """
        intent = "symptoms.normalised"
        enforce_comms(self, intent)
        return AgentMessage(
            sender=self.SLUG, recipient="classifier", intent=intent,
            payload={
                "normalised_symptoms": state.normalised_symptoms,
                "detected_language": state.detected_language,
                "keywords": state.intake_keywords,
            },
        )

    async def run(self, state: CaseState) -> dict:
        """Normalise `state.raw_text` into structured symptoms, then fold in the
        interview answers the request carried (`state.clarifications`).

        Reads:  state.raw_text, state.language, state.is_voice, state.clarifications
        Writes: state.normalised_symptoms  (one clear clinical sentence + folded answers)
                state.detected_language    (ISO 639-1 code)
                state.intake_keywords      (list[str], short symptom keywords)
        Returns: {"source", "normalised_symptoms", "detected_language", "keywords"}

        The answers are folded HERE, once, so every downstream reader of
        `normalised_symptoms` — the trained model, the keyword table, the red-flag
        regexes, the cross-visit rule — sees the same evidence without each of
        them learning about the interview. `raw_text` is never modified.
        """
        result = await self._normalise(state)
        folded = fold_clarifications(state.normalised_symptoms, state.clarifications)
        if folded != state.normalised_symptoms:
            state.normalised_symptoms = folded
            # Keywords are re-read from the folded text so an answer that names a
            # symptom ("yes, fever since last night") is in the keyword list too.
            state.intake_keywords = _extract_keywords(folded) or state.intake_keywords
            result["normalised_symptoms"] = folded
            result["keywords"] = state.intake_keywords
        return result

    async def _normalise(self, state: CaseState) -> dict:
        """The complaint alone — see `run()` for the folded-answers step.

        `source` is the provenance label surfaced in the audit trail: `"fallback"`
        when the deterministic pass was enough, `"llm"` when the model was needed
        and delivered.

        DETERMINISTIC FIRST, LLM ONLY TO FILL A GAP. `_fallback()` always runs and
        its result is the floor; the LLM is consulted only when that pass reads
        nothing, and even then the keywords are re-extracted deterministically
        from the model's English so it can never name a symptom the patient did
        not describe. Any LLM failure — network, bad JSON, empty extraction —
        degrades to the floor rather than raising, because an intake failure must
        not take down the triage pipeline.
        """
        # [A2A] Read the Supervisor's opening message before doing anything. Until
        # now this agent DECLARED a subscription to `case.opened` and never read
        # it, which made its half of the protocol decorative — it published but
        # never listened. `case.opened` carries the language and voice flags, so
        # taking them from the message rather than from shared state is what makes
        # the subscription mean something.
        language, is_voice = self._declared_inputs(state)

        # The deterministic path ALWAYS runs first and is the floor. It is local
        # computation (microseconds), and its keywords are literal matches against
        # the patient's own words, so they cannot be wrong in the inventing sense.
        result = self._fallback(state, language=language, is_voice=is_voice)

        # It found symptoms in ENGLISH input -> nothing for the LLM to add. Most
        # cases land here, so the common path never waits on the network.
        #
        # The language half of this condition is an E1 finding. The trigger was
        # once "keywords are empty" alone, and that was too greedy: Spanish
        # "Estoy vomitando" matches the English form "vomit" by cognate accident,
        # which short-circuited the LLM and returned an UNTRANSLATED sentence
        # with one lucky keyword. A cognate hit is not a reading of the sentence.
        # If the input is not English, the English-only vocabulary cannot be
        # trusted to have seen everything, however much it happened to match.
        if result["keywords"] and result["detected_language"] == _DEFAULT_LANGUAGE:
            return result

        # Zero keywords means the local English vocabulary could not read this
        # input. Three different causes — another language, a misspelling, or
        # unusual phrasing — all need the same help, which is why the trigger is
        # "found nothing" rather than "not English": a typo is still English and
        # a language check would skip right past it.
        english = await self._to_clinical_english(state)
        if not english:
            return result  # LLM unavailable or unusable — the floor stands

        # Keywords are STILL extracted deterministically, now from the English
        # text. The model never names a symptom itself, so it has no way to
        # invent one — E1 scores hallucination rate at exactly 0.0.
        keywords = _extract_keywords(english)
        if not keywords:
            return result

        # The English version also BECOMES the normalised sentence, because
        # `redflags.py` is an English-only regex table — an English sentence is
        # the only thing that lets a non-English emergency, or a misspelt one
        # ("cant breath"), match a deterministic red-flag rule at all.
        #
        # Safe to replace precisely BECAUSE we only reach this line when the
        # deterministic pass found nothing: there is no clinical content here to
        # lose. And nothing is lost anyway — `raw_text` is never modified, and
        # both the classifier and Safety-Override read
        # `normalised_symptoms + raw_text`, so the patient's exact words still
        # reach every downstream check.
        state.intake_keywords = keywords
        state.normalised_symptoms = english
        result["keywords"] = keywords
        result["normalised_symptoms"] = english
        result["source"] = "llm"
        return result

    async def _to_clinical_english(self, state: CaseState) -> str:
        """One bounded LLM call: the patient's own words, in clean English.

        Returns "" on ANY failure — unreachable provider, kill switch, malformed
        JSON, empty or over-long reply. An intake problem must never take down
        the triage pipeline, so no exception leaves this method.
        """
        # [AI-Security] FR-12 least-privilege enforcement point.
        enforce_tool_access(self, "llm.complete")
        try:
            reply = await llm.complete(_LLM_SYSTEM, _llm_prompt(state.raw_text), json_mode=True, task="intake.normalise")
            english = str(json.loads(reply).get("english") or "").strip()
        except Exception:  # noqa: BLE001 - any LLM fault must fall back to deterministic normalisation
            return ""
        return english if english and len(english) <= _MAX_LLM_CHARS else ""

    def _declared_inputs(self, state: CaseState) -> tuple[str, bool]:
        """[A2A] The language and voice flags for this case, message-first.

        Precedence is deliberate: the `case.opened` payload wins over `CaseState`
        when the Supervisor delivered an inbox, because the message is the
        explicit handoff and the shared state is an implicit side channel. When
        there is no bus — every unit test, and `run()` called standalone — this
        falls straight back to the state, so behaviour offline is unchanged.

        `require_subscription` inside `received_payload()` enforces that this
        agent declared `case.opened`, so reading is least-privileged in the same
        way publishing is.
        """
        if not getattr(self, "_consumed", False):
            return state.language, state.is_voice
        opened = self.received_payload("case.opened")
        if not opened:
            return state.language, state.is_voice
        return (
            str(opened.get("language") or state.language),
            bool(opened.get("is_voice", state.is_voice)),
        )

    def _fallback(
        self,
        state: CaseState,
        *,
        language: str | None = None,
        is_voice: bool | None = None,
    ) -> dict:
        """The deterministic, no-network path.

        MANDATORY, not optional: the entire test suite runs with `llm.complete`
        patched out (and production has a kill switch that does the same), so
        this is the path almost every case actually takes.

        Cleans rather than rewrites, and does not translate — see the module
        docstring for why, and E1 for how the two paths are scored separately.
        """
        # Message-supplied values when the Supervisor delivered an inbox,
        # otherwise the state. See `_declared_inputs`.
        voice = state.is_voice if is_voice is None else is_voice
        declared = state.language if language is None else language

        cleaned = _clean_text(state.raw_text, voice)
        detected = _detect_language(cleaned or state.raw_text, declared)
        keywords = _extract_keywords(cleaned)

        state.normalised_symptoms = cleaned
        state.detected_language = detected
        state.intake_keywords = keywords

        return {
            "source": "fallback",
            "normalised_symptoms": cleaned,
            "detected_language": detected,
            "keywords": keywords,
        }
