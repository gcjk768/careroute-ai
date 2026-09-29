"""Safety-Override worker: deterministic, hard-coded red-flag rules.

These rules are intentionally NOT LLM-based. They are the last line of
defense and must be un-overridable by the (possibly wrong) classifier
output -- if a red flag fires, acuity can only be forced UP (more severe),
never down.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import acuity_rank, more_severe

POLICY_VERSION = "redflags-v1"


@dataclass
class RedFlagRule:
    name: str
    patterns: list[str]
    forced_acuity: str
    reason: str


# Ordered roughly by severity. First matching rule wins for the "primary"
# rule reported to the UI, but forced acuity is the most severe across ALL
# matching rules.
# Patterns are matched against FOLDED text (see `fold`): lowercase, NFKC, no
# invisible characters, straight apostrophes. They are written for the ways
# patients actually type an emergency, not only the textbook term: recall was
# 1.0 on the curated eval set while "he stopped breathing", "overdosed on
# paracetamol", "baby is floppy and blue", "choking", "pregnant and bleeding"
# and "I don't want to live anymore" fired nothing.
# Up to 80 characters containing no denial word. Negation is checked at a
# match's START, so a two-part pattern ("hit my head ... vomiting") would read
# "bumped my head, no confusion, no vomiting" as a report without this.
_NO_DENIAL_GAP = r"(?:(?!\b(?:no|not|without|denies|never)\b).){0,80}"

RED_FLAG_RULES: list[RedFlagRule] = [
    RedFlagRule(
        name="unresponsive",
        patterns=[
            r"\bunresponsive\b", r"\bunconscious\b", r"\bnot responding\b", r"\bnot rousable\b",
            r"\b(won'?t|will not|cannot|can'?t) wake\b", r"\bnot breathing\b",
            r"\bstopped breathing\b", r"\bno pulse\b",
        ],
        forced_acuity="P1_RESUSCITATION",
        reason="Unresponsive or not breathing: possible cardiac or respiratory arrest.",
    ),
    RedFlagRule(
        name="cardiac_chest_pain",
        patterns=[
            r"chest pain", r"chest tightness", r"chest pressure", r"crushing (chest|pain)",
            r"chest (hurts|hurting|aches?|discomfort)", r"tight(ness)? (in |across )?(my |the )?chest",
            r"pain (in|across|over) (my |the )?chest", r"pain (in|down) my (left )?arm",
            r"(left )?arm pain.{0,40}sweat", r"sweat.{0,40}(left )?arm pain",
            r"jaw pain.*chest", r"heart attack",
        ],
        forced_acuity="P1_RESUSCITATION",
        reason="Possible acute coronary syndrome (chest pain pattern).",
    ),
    RedFlagRule(
        name="breathlessness",
        patterns=[
            r"can'?t breathe?\b", r"can ?not breathe?\b", r"difficult\w* (in |with )?breathing",
            r"breathing (difficult|trouble|problem)", r"trouble breathing", r"short(ness)? of breath",
            r"severe(ly)? breathless", r"gasping for air", r"struggling to breathe",
            r"turning blue", r"lips? (are |is )?(turned |turning |going |went )?blue",
            r"floppy and blue", r"blue (lips|baby)",
        ],
        forced_acuity="P1_RESUSCITATION",
        reason="Severe respiratory distress reported.",
    ),
    RedFlagRule(
        name="choking",
        patterns=[r"\bchok(ing|ed)\b", r"airway (is )?(blocked|obstructed)"],
        forced_acuity="P1_RESUSCITATION",
        reason="Choking or airway obstruction reported.",
    ),
    RedFlagRule(
        name="poisoning_overdose",
        patterns=[
            r"\boverdos", r"took (too many|\d+) (pills|tablets|panadol|paracetamol|capsules)",
            r"swallowed (bleach|poison|detergent|chemical|petrol|kerosene)", r"\bpoison(ed|ing)\b",
        ],
        forced_acuity="P1_RESUSCITATION",
        reason="Possible overdose or poisoning.",
    ),
    RedFlagRule(
        name="anaphylaxis",
        patterns=[
            r"anaphylaxis", r"throat (is )?(closing|swelling)", r"tongue (is )?swelling",
            r"allergic reaction.*(swelling|breath)", r"epi[- ]?pen",
        ],
        forced_acuity="P1_RESUSCITATION",
        reason="Signs consistent with anaphylaxis.",
    ),
    RedFlagRule(
        name="stroke_signs",
        patterns=[
            r"face (is )?droop", r"slurr", r"sudden weakness", r"one side.*(weak|numb)",
            r"(weak|weakness|numb|numbness) on (one|the left|the right|my left|my right) side",
            r"numb(ness)?.{0,20}(left|right|one) side",
            r"(can'?t|cannot|can ?not) (move|feel|lift|raise) (my )?(arm|leg|face|hand)",
            r"sudden confusion", r"worst headache (of my life|ever)", r"thunderclap",
        ],
        forced_acuity="P1_RESUSCITATION",
        reason="Possible stroke (FAST) signs.",
    ),
    RedFlagRule(
        name="obstetric_bleeding",
        patterns=[r"pregnan\w*.{0,60}bleed", r"bleed\w*.{0,60}pregnan"],
        forced_acuity="P2_EMERGENT",
        reason="Bleeding in pregnancy.",
    ),
    RedFlagRule(
        name="severe_bleeding",
        patterns=[
            r"severe bleeding", r"heavy bleeding", r"profuse\w* bleed", r"won'?t stop bleeding",
            r"bleeding (that |which )?(won'?t|will not|does ?n'?t|doesn'?t) stop",
            r"bleeding (a lot|heavily|profusely)", r"blood (everywhere|pooling)",
            r"coughing (up )?blood", r"(vomit\w*|throw\w* up|throwing up|puk\w*|bring\w* up) (up )?blood",
            r"blood in (my )?vomit",
        ],
        forced_acuity="P2_EMERGENT",
        reason="Severe or uncontrolled bleeding reported.",
    ),
    RedFlagRule(
        name="suicidal_ideation",
        patterns=[
            r"suicid", r"kill myself", r"end my life", r"want to die", r"self.?harm",
            r"hurt(ing)? myself", r"don'?t want to (live|be alive|be here)", r"end(ing)? it all",
            r"take my (own )?life", r"better off dead",
        ],
        forced_acuity="P2_EMERGENT",
        reason="Expressions of suicidal ideation or self-harm risk.",
    ),
    # Live scenario test 2026-09-24: "my 2 year old has a fever of 40, very
    # drowsy and hard to wake up" came out P3 and "my 80 year old father fell,
    # hit his head and is now confused" P3 — neither text lit a single feature.
    # Reduced consciousness short of "unresponsive" is still an emergency.
    RedFlagRule(
        name="altered_consciousness",
        patterns=[
            r"\b(hard|difficult|harder|trouble|struggling) to (wake|rouse)\b", r"\bbarely (awake|conscious|responsive)\b",
            r"\b(very|extremely|unusually|abnormally) (drowsy|sleepy|floppy)\b",
            r"\b(now|newly|suddenly|became|become|becoming|is|seems|getting) (very |more )?confused\b",
            r"\bconfused (and|or) (drowsy|sleepy)\b",
        ],
        forced_acuity="P2_EMERGENT",
        reason="Reduced consciousness or new confusion.",
    ),
    RedFlagRule(
        name="head_injury",
        patterns=[
            r"\b(hit|hits|bang\w*|knock\w*|struck|bump\w*|injur\w*|hurt) (on )?(his|her|my|their|the|its) head\b"
            + _NO_DENIAL_GAP + r"\b(confus\w*|vomit\w*|drows\w*|sleepy|passed out|blacked out|knocked out|unconscious|fit|seizure)",
            r"\bhead (injury|trauma|knock)\b" + _NO_DENIAL_GAP
            + r"\b(confus\w*|vomit\w*|drows\w*|passed out|blacked out|unconscious)",
            r"\bfell\b.{0,40}\bhead\b.{0,20}\b(blood thinner|warfarin|anticoagula\w*)",
        ],
        forced_acuity="P2_EMERGENT",
        reason="Head injury with confusion, vomiting or drowsiness.",
    ),
    # 2026-09-26 interview soak (#018): "high fever with a stiff neck and a purple
    # rash" came out P3 — no rule covered meningitis/meningococcal sepsis at all.
    # A stiff neck alone (slept wrong) or a rash alone is not enough; fever plus
    # either is, and a non-blanching rash is on its own.
    RedFlagRule(
        name="meningitis_sepsis",
        patterns=[
            r"\b(fever|temperature|feverish)\b" + _NO_DENIAL_GAP + r"\b(stiff neck|neck (is )?stiff\w*)",
            r"\b(stiff neck|neck (is )?stiff\w*)" + _NO_DENIAL_GAP + r"\b(fever|temperature|feverish)\b",
            r"\bnon[- ]?blanch\w*", r"\brash\b.{0,30}\b(does ?n'?t|doesn'?t|does not|won'?t|not) (fade|blanch)",
            r"\b(fever|temperature|feverish)\b" + _NO_DENIAL_GAP + r"\b(purple|purplish|purpuric)\b.{0,20}\b(rash|spots|blotches)",
            r"\b(purple|purplish|purpuric)\b.{0,20}\b(rash|spots|blotches)" + _NO_DENIAL_GAP + r"\b(fever|temperature|feverish)\b",
        ],
        forced_acuity="P2_EMERGENT",
        reason="Possible meningitis or sepsis (fever with stiff neck or non-blanching rash).",
    ),
    RedFlagRule(
        name="seizure",
        patterns=[r"\bseizure", r"\bconvuls", r"\b(having|had|has|is|was) (a )?fit(s|ting)?\b"],
        forced_acuity="P2_EMERGENT",
        reason="Active or recent seizure activity.",
    ),
]

_COMPILED_RULES = [
    (rule, [re.compile(p, re.IGNORECASE) for p in rule.patterns]) for rule in RED_FLAG_RULES
]

_CURLY = str.maketrans({"\u2019": "'", "\u2018": "'"})


def fold(text: str, invisible_as: str = "") -> str:
    """The text the rules run on: NFKC (full-width, compatibility forms), no
    format characters (zero-width space, soft hyphen, bidi marks: "chest\u200bpain"
    defeated every regex while the guardrail folded exactly these for its own
    scan), straight apostrophes, single spaces, lowercase. A format character
    may hide a word break or sit inside a word, so the rules see both readings
    (`invisible_as` "" and " ")."""
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(invisible_as if unicodedata.category(ch) == "Cf" else ch for ch in t)
    return re.sub(r"\s+", " ", t.translate(_CURLY)).strip().lower()


# Negation. "I don't have chest pain" forced P1. A denial cue in the SAME
# clause, within three tokens before the phrase, with "and"/"but" ending the
# window ("no fever but chest pain" reports the chest pain) is a denial; "or"
# is not a boundary ("no fever or chills" denies both). "can't stop coughing"
# and "never had chest pain like this before" are reports. This is the one rule
# the classifier's keyword path and the ML feature extractor apply, so every
# reader of a sentence agrees; a phrase both denied and reported in one text
# counts as reported.
NEGATION_CUES = frozenset({"no", "not", "without", "denies", "denied", "deny", "never", "negative"})
NEGATION_WINDOW = 3
NEGATION_BOUNDARY = frozenset({"and", "but"})
INTENSIFIER_BEFORE = frozenset({"stop", "stopping", "stopped"})
INTENSIFIER_AFTER = ("like this", "this bad", "so bad", "before", "as bad")
_CLAUSE_DELIMITERS = ".,;:!?"
_TOKEN = re.compile(r"[a-z']+")


def is_denied(lower: str, start: int, end: int) -> bool:
    """True when the phrase at lower[start:end] is denied rather than reported."""
    clause_start = max(lower.rfind(d, 0, start) for d in _CLAUSE_DELIMITERS)
    tokens = _TOKEN.findall(lower[clause_start + 1:start])
    for index in range(len(tokens) - 1, -1, -1):
        if tokens[index] in NEGATION_BOUNDARY:
            tokens = tokens[index + 1:]
            break
    window = tokens[-NEGATION_WINDOW:]
    if not any(tok in NEGATION_CUES or tok.endswith("n't") for tok in window):
        return False
    if window[-1] in INTENSIFIER_BEFORE:
        return False
    if "never" in window:
        ends = [n for n in (lower.find(d, end) for d in _CLAUSE_DELIMITERS) if n != -1]
        after = lower[end:min(ends) if ends else len(lower)]
        if any(marker in after for marker in INTENSIFIER_AFTER):
            return False
    return True


def _reported(pattern: re.Pattern, folded: str) -> bool:
    return any(not is_denied(folded, m.start(), m.end()) for m in pattern.finditer(folded))


@dataclass
class SafetyOverrideResult:
    triggered: bool
    rule: str | None
    reason: str | None
    forced_acuity: str | None


def evaluate(text: str) -> SafetyOverrideResult:
    """Scan raw text for red flags. Returns the single most severe match."""
    matches = evaluate_all(text)
    if not matches:
        return SafetyOverrideResult(triggered=False, rule=None, reason=None, forced_acuity=None)

    return matches[0]


def evaluate_all(text: str) -> list[SafetyOverrideResult]:
    """Return every deterministic red-flag hit, most severe first.

    `evaluate()` remains the authoritative single-result API used for acuity
    override. This helper supports Phase 3 signal reporting without changing the
    existing escalation behavior.
    """
    readings = {fold(text), fold(text, invisible_as=" ")}
    matches: list[RedFlagRule] = []
    for rule, compiled in _COMPILED_RULES:
        if any(_reported(p, folded) for p in compiled for folded in readings):
            matches.append(rule)

    matches.sort(key=lambda rule: acuity_rank(rule.forced_acuity))
    return [
        SafetyOverrideResult(
            triggered=True,
            rule=rule.name,
            reason=rule.reason,
            forced_acuity=rule.forced_acuity,
        )
        for rule in matches
    ]


def apply_override(classifier_acuity: str, override: SafetyOverrideResult) -> str:
    """Safety-Override can only push acuity to a MORE severe value, never relax it."""
    if not override.triggered or override.forced_acuity is None:
        return classifier_acuity
    return more_severe(classifier_acuity, override.forced_acuity)
