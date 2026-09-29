"""Aggregate category-ranker benchmarks for Safety NLP shadow mode."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .contracts import CategoryCandidate, MentionSpan


class CategoryRanker(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def model_revision(self) -> str: ...

    def rank(self, text: str, mention: MentionSpan) -> list[CategoryCandidate]: ...


@dataclass(frozen=True)
class CategoryRankerCandidate:
    name: str
    adapter: CategoryRanker


@dataclass(frozen=True)
class CategoryBenchmarkBucket:
    candidate: str
    language: str
    rows: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    recall_at_k: dict[int, float] = field(default_factory=dict)
    threshold: float = 0.6
    positives: int = 0
    negatives: int = 0
    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0
    abstentions: int = 0
    threshold_recall: float = 0.0
    specificity: float = 0.0
    abstention_rate: float = 0.0
    calibration_error: float = 0.0
    model_name: str | None = None
    model_revision: str | None = None


@dataclass(frozen=True)
class CategoryDisagreementBucket:
    primary_candidate: str
    comparator_candidate: str
    language: str
    rows: int = 0
    disagreements: int = 0
    trigger_disagreements: int = 0
    category_disagreements: int = 0
    review_telemetry_count: int = 0

    @property
    def disagreement_rate(self) -> float:
        return _rate(self.disagreements, self.rows)


@dataclass(frozen=True)
class CategoryBenchmarkReport:
    schema_version: int
    candidates: tuple[str, ...]
    k_values: tuple[int, ...]
    buckets: tuple[CategoryBenchmarkBucket, ...]
    disagreements: tuple[CategoryDisagreementBucket, ...] = ()
    notes: tuple[str, ...] = (
        "Synthetic benchmark labels are not clinically validated.",
        "Summaries are aggregate-only and do not contain patient text, mention text, evidence spans, prompts, responses, case IDs or session IDs.",
    )


def benchmark_category_rankers(
    cases: Iterable[Mapping],
    candidates: Iterable[CategoryRankerCandidate],
    *,
    k_values: Iterable[int] = (1, 3),
    decision_threshold: float = 0.6,
) -> CategoryBenchmarkReport:
    if not 0.0 <= decision_threshold <= 1.0:
        raise ValueError("decision_threshold must be between 0 and 1")
    k_tuple = tuple(sorted(set(k_values)))
    candidate_tuple = tuple(candidates)
    case_tuple = tuple(cases)
    buckets: dict[tuple[str, str], _MutableCategoryBucket] = {}
    decisions: dict[tuple[int, str], _CategoryDecision] = {}

    for candidate in candidate_tuple:
        for row_index, row in enumerate(case_tuple):
            language = str(row.get("language", "unknown"))
            text = str(row.get("referenceTranslation") or row.get("text") or "")
            mention = _benchmark_mention(row, text)
            if mention is None:
                continue
            key = (candidate.name, language)
            bucket = buckets.setdefault(
                key,
                _MutableCategoryBucket(
                    candidate=candidate.name,
                    language=language,
                    k_values=k_tuple,
                    threshold=decision_threshold,
                ),
            )
            ranked = candidate.adapter.rank(text, mention)
            bucket.add_result(
                gold_category=str(row.get("category")),
                gold_trigger=bool(row.get("goldTrigger")),
                ranked=ranked,
                model_name=candidate.adapter.model_name,
                model_revision=candidate.adapter.model_revision,
            )
            decisions[(row_index, candidate.name)] = _decision_from_ranked(
                language=language,
                ranked=ranked,
                threshold=decision_threshold,
            )

    return CategoryBenchmarkReport(
        schema_version=1,
        candidates=tuple(candidate.name for candidate in candidate_tuple),
        k_values=k_tuple,
        buckets=tuple(
            buckets[key].freeze()
            for key in sorted(buckets, key=lambda item: (item[0], item[1]))
        ),
        disagreements=_disagreement_buckets(case_tuple, candidate_tuple, decisions),
    )


@dataclass
class _MutableCategoryBucket:
    candidate: str
    language: str
    k_values: tuple[int, ...]
    threshold: float
    rows: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    hits_at_k: dict[int, int] = field(default_factory=dict)
    positives: int = 0
    negatives: int = 0
    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0
    abstentions: int = 0
    calibration_error_total: float = 0.0
    model_name: str | None = None
    model_revision: str | None = None

    def __post_init__(self) -> None:
        self.hits_at_k = dict.fromkeys(self.k_values, 0)

    def add_result(
        self,
        *,
        gold_category: str,
        gold_trigger: bool,
        ranked: list[CategoryCandidate],
        model_name: str,
        model_revision: str,
    ) -> None:
        self.rows += 1
        self.model_name = model_name
        self.model_revision = model_revision
        status = ranked[0].status if ranked else "invalid_output"
        self.status_counts[status] = self.status_counts.get(status, 0) + 1
        categories = [candidate.category for candidate in ranked if candidate.category]
        for k in self.k_values:
            if gold_category in categories[:k]:
                self.hits_at_k[k] += 1
        if gold_trigger:
            self.positives += 1
        else:
            self.negatives += 1
        top = ranked[0] if ranked else None
        top_confidence = float(top.confidence) if top else 0.0
        accepted = bool(
            top
            and top.status == "success"
            and top.category == gold_category
            and top.confidence >= self.threshold
        )
        abstained = not top or top.status != "success" or top.category is None or top.confidence < self.threshold
        if abstained:
            self.abstentions += 1
        if gold_trigger and accepted:
            self.true_positives += 1
        elif gold_trigger:
            self.false_negatives += 1
        elif accepted:
            self.false_positives += 1
        else:
            self.true_negatives += 1
        self.calibration_error_total += abs(top_confidence - (1.0 if gold_trigger and accepted else 0.0))

    def freeze(self) -> CategoryBenchmarkBucket:
        return CategoryBenchmarkBucket(
            candidate=self.candidate,
            language=self.language,
            rows=self.rows,
            status_counts=dict(self.status_counts),
            recall_at_k={k: _rate(self.hits_at_k[k], self.rows) for k in self.k_values},
            threshold=self.threshold,
            positives=self.positives,
            negatives=self.negatives,
            true_positives=self.true_positives,
            false_positives=self.false_positives,
            true_negatives=self.true_negatives,
            false_negatives=self.false_negatives,
            abstentions=self.abstentions,
            threshold_recall=_rate(self.true_positives, self.positives),
            specificity=_rate(self.true_negatives, self.negatives),
            abstention_rate=_rate(self.abstentions, self.rows),
            calibration_error=_rate_float(self.calibration_error_total, self.rows),
            model_name=self.model_name,
            model_revision=self.model_revision,
        )


@dataclass(frozen=True)
class _CategoryDecision:
    language: str
    triggered: bool
    category: str | None


@dataclass
class _MutableDisagreementBucket:
    primary_candidate: str
    comparator_candidate: str
    language: str
    rows: int = 0
    disagreements: int = 0
    trigger_disagreements: int = 0
    category_disagreements: int = 0

    def add(self, primary: _CategoryDecision, comparator: _CategoryDecision) -> None:
        self.rows += 1
        trigger_disagreement = primary.triggered != comparator.triggered
        category_disagreement = primary.category != comparator.category
        if trigger_disagreement:
            self.trigger_disagreements += 1
        if category_disagreement:
            self.category_disagreements += 1
        if trigger_disagreement or category_disagreement:
            self.disagreements += 1

    def freeze(self) -> CategoryDisagreementBucket:
        return CategoryDisagreementBucket(
            primary_candidate=self.primary_candidate,
            comparator_candidate=self.comparator_candidate,
            language=self.language,
            rows=self.rows,
            disagreements=self.disagreements,
            trigger_disagreements=self.trigger_disagreements,
            category_disagreements=self.category_disagreements,
            review_telemetry_count=self.disagreements,
        )


def _benchmark_mention(row: Mapping, text: str) -> MentionSpan | None:
    mentions = row.get("mentions")
    if not isinstance(mentions, list) or not mentions:
        return None
    category = str(row.get("category"))
    mention_row = next(
        (
            mention for mention in mentions
            if isinstance(mention, Mapping) and str(mention.get("category", category)) == category
        ),
        mentions[0],
    )
    mention_text = str(mention_row.get("text") or category.replace("_", " "))
    start = text.casefold().find(mention_text.casefold())
    if start < 0:
        start = 0
        mention_text = text[:min(len(text), max(1, len(mention_text)))]
    end = start + len(mention_text)
    return MentionSpan(
        mention_id="benchmark-mention",
        text=mention_text,
        start=start,
        end=end,
        entity_type=str(mention_row.get("entityType") or "Sign_symptom"),
        source_language=str(row.get("language") or "unknown"),
    )


def _decision_from_ranked(
    *,
    language: str,
    ranked: list[CategoryCandidate],
    threshold: float,
) -> _CategoryDecision:
    top = ranked[0] if ranked else None
    triggered = bool(top and top.status == "success" and top.category is not None and top.confidence >= threshold)
    return _CategoryDecision(
        language=language,
        triggered=triggered,
        category=top.category if triggered else None,
    )


def _disagreement_buckets(
    cases: tuple[Mapping, ...],
    candidates: tuple[CategoryRankerCandidate, ...],
    decisions: dict[tuple[int, str], _CategoryDecision],
) -> tuple[CategoryDisagreementBucket, ...]:
    if len(candidates) < 2:
        return ()
    primary = candidates[0].name
    buckets: dict[tuple[str, str], _MutableDisagreementBucket] = {}
    for row_index, row in enumerate(cases):
        primary_decision = decisions.get((row_index, primary))
        if primary_decision is None:
            continue
        language = str(row.get("language", primary_decision.language))
        for comparator in candidates[1:]:
            comparator_decision = decisions.get((row_index, comparator.name))
            if comparator_decision is None:
                continue
            key = (comparator.name, language)
            bucket = buckets.setdefault(
                key,
                _MutableDisagreementBucket(
                    primary_candidate=primary,
                    comparator_candidate=comparator.name,
                    language=language,
                ),
            )
            bucket.add(primary_decision, comparator_decision)
    return tuple(
        buckets[key].freeze()
        for key in sorted(buckets, key=lambda item: (item[0], item[1]))
    )


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _rate_float(numerator: float, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator
