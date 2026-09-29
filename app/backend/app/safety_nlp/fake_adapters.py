"""Deterministic fake adapters for Safety NLP tests.

These are not clinical NLP. They exist so Phase 4 can build the adapter
pipeline and failure handling before any real model artifacts are installed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..redflags import RED_FLAG_RULES
from .contracts import (
    AdapterConfig,
    AssertionResult,
    CategoryCandidate,
    ContextResult,
    MentionSpan,
    OptionalModelAdapter,
    SafetyNlpAdapters,
    TranslationResult,
)

_CATEGORY_PHRASES: dict[str, tuple[str, ...]] = {
    "cardiac_chest_pain": ("chest pain", "chest pressure", "chest tightness"),
    "breathlessness": ("cannot breathe", "can't breathe", "cant breathe", "difficulty breathing"),
    "anaphylaxis": ("throat is closing", "tongue is swelling", "anaphylaxis"),
    "stroke_signs": ("slurred speech", "face droop", "face is drooping", "one sided weakness"),
    "severe_bleeding": ("vomiting blood", "bleeding", "blood is pooling"),
    "suicidal_ideation": ("kill myself", "end my life", "want to die"),
    "seizure": ("seizure", "convulsion"),
}

_KNOWN_CATEGORIES = {rule.name for rule in RED_FLAG_RULES}


@dataclass
class FakeTranslationAdapter(OptionalModelAdapter):
    translations: dict[tuple[str, str], str] = field(default_factory=dict)
    config: AdapterConfig = field(default_factory=lambda: AdapterConfig(
        model_name="fake-translation",
        model_revision="local-test",
    ))

    def translate(self, text: str, language: str) -> TranslationResult:
        status = self.availability_status(text)
        if status != "success":
            return TranslationResult(
                original_text=text,
                language=language,
                translated_text="",
                model_name=self.model_name,
                model_revision=self.model_revision,
                status=status,
            )
        translated = self.translations.get((language, text), text)
        return TranslationResult(
            original_text=text,
            language=language,
            translated_text=translated,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )


@dataclass
class FakeNerAdapter(OptionalModelAdapter):
    config: AdapterConfig = field(default_factory=lambda: AdapterConfig(
        model_name="fake-ner",
        model_revision="local-test",
    ))

    def extract(self, text: str, language: str) -> list[MentionSpan]:
        status = self.availability_status(text)
        if status != "success":
            return []
        mentions: list[MentionSpan] = []
        lowered = text.lower()
        for phrases in _CATEGORY_PHRASES.values():
            for phrase in phrases:
                match = re.search(re.escape(phrase), lowered)
                if match is None:
                    continue
                mentions.append(
                    MentionSpan(
                        mention_id=f"m{len(mentions) + 1}",
                        text=text[match.start():match.end()],
                        start=match.start(),
                        end=match.end(),
                        source_language=language,
                        model_name=self.model_name,
                        model_revision=self.model_revision,
                    )
                )
                break
        return mentions


@dataclass
class FakeAssertionAdapter(OptionalModelAdapter):
    overrides: dict[str, str] = field(default_factory=dict)
    config: AdapterConfig = field(default_factory=lambda: AdapterConfig(
        model_name="fake-assertion",
        model_revision="local-test",
    ))

    def classify(self, text: str, mention: MentionSpan) -> AssertionResult:
        status = self.availability_status(text)
        assertion = self.overrides.get(mention.mention_id)
        if status != "success":
            assertion = "unknown"
        if assertion is None:
            prefix = text[:mention.start].lower()
            window = prefix[-25:]
            if re.search(r"\b(no|not|never|without|denies|did not)\b", window):
                assertion = "negated"
            elif re.search(r"\b(if|would|might|could)\b", window):
                assertion = "conditional"
            elif re.search(r"\bmaybe|possibly|not sure\b", window):
                assertion = "possible"
            else:
                assertion = "present"
        return AssertionResult(
            mention_id=mention.mention_id,
            assertion=assertion,
            confidence=0.9 if assertion != "unknown" else 0.0,
            model_name=self.model_name,
            model_revision=self.model_revision,
            status=status,
        )


@dataclass
class FakeContextAdapter(OptionalModelAdapter):
    subjects: dict[str, str] = field(default_factory=dict)
    temporalities: dict[str, str] = field(default_factory=dict)
    config: AdapterConfig = field(default_factory=lambda: AdapterConfig(
        model_name="fake-context",
        model_revision="local-test",
    ))

    def classify(self, text: str, mention: MentionSpan) -> ContextResult:
        status = self.availability_status(text)
        lowered = text.lower()
        subject = self.subjects.get(mention.mention_id)
        if subject is None:
            before = lowered[:mention.start]
            if "my child" in before or "my daughter" in before or "my son" in before:
                subject = "care_subject"
            elif "brother" in before or "sister" in before or "someone" in before:
                subject = "other_person"
            else:
                subject = "patient"
        temporality = self.temporalities.get(mention.mention_id)
        if temporality is None:
            around = lowered[max(0, mention.start - 40):mention.end + 40]
            if "ten years ago" in around or "years ago" in around:
                temporality = "remote"
            elif "five minutes ago" in around or "minutes ago" in around:
                temporality = "recent"
            elif "not sure when" in around or "sometimes" in around:
                temporality = "unknown"
            else:
                temporality = "current"
        if status != "success":
            subject = "unknown"
            temporality = "unknown"
        return ContextResult(
            mention_id=mention.mention_id,
            subject=subject,
            temporality=temporality,
            conditional=" if " in f" {lowered} ",
            model_name=self.model_name,
            model_revision=self.model_revision,
            status=status,
        )


@dataclass
class FakeCategoryAdapter(OptionalModelAdapter):
    overrides: dict[str, str | None] = field(default_factory=dict)
    config: AdapterConfig = field(default_factory=lambda: AdapterConfig(
        model_name="fake-category",
        model_revision="local-test",
    ))

    def rank(self, text: str, mention: MentionSpan) -> list[CategoryCandidate]:
        status = self.availability_status(text)
        category = self.overrides.get(mention.mention_id)
        if status != "success":
            category = None
        if category is None and status == "success":
            mention_text = mention.text.lower()
            for candidate, phrases in _CATEGORY_PHRASES.items():
                if any(phrase in mention_text for phrase in phrases):
                    category = candidate
                    break
        if category not in _KNOWN_CATEGORIES:
            category = None
        return [
            CategoryCandidate(
                mention_id=mention.mention_id,
                category=category,
                confidence=0.92 if category else 0.0,
                model_name=self.model_name,
                model_revision=self.model_revision,
                status=status,
            )
        ]


def fake_adapters(**overrides) -> SafetyNlpAdapters:
    """Build a complete fake adapter bundle, with optional stage overrides."""
    return SafetyNlpAdapters(
        translation=overrides.get("translation") or FakeTranslationAdapter(),
        ner=overrides.get("ner") or FakeNerAdapter(),
        assertion=overrides.get("assertion") or FakeAssertionAdapter(),
        context=overrides.get("context") or FakeContextAdapter(),
        category=overrides.get("category") or FakeCategoryAdapter(),
    )
