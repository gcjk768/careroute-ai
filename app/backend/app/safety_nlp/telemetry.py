"""Privacy-safe aggregate telemetry for Safety NLP signals."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ..agents.safety import SafetySignal

UNCERTAIN_ASSERTIONS = frozenset({"possible", "unknown", "conditional"})
UNCERTAIN_CONTEXTS = frozenset({"unknown"})


@dataclass(frozen=True)
class SafetyNlpTelemetrySummary:
    total_signals: int = 0
    triggered_signals: int = 0
    abstentions: int = 0
    uncertainty_count: int = 0
    disagreement_count: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    channel_counts: dict[str, int] = field(default_factory=dict)
    stage_counts: dict[str, int] = field(default_factory=dict)
    stage_latency_ms: dict[str, int] = field(default_factory=dict)
    stage_availability_counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "totalSignals": self.total_signals,
            "triggeredSignals": self.triggered_signals,
            "abstentions": self.abstentions,
            "uncertaintyCount": self.uncertainty_count,
            "disagreementCount": self.disagreement_count,
            "statusCounts": dict(self.status_counts),
            "channelCounts": dict(self.channel_counts),
            "stageCounts": dict(self.stage_counts),
            "stageLatencyMs": dict(self.stage_latency_ms),
            "stageAvailabilityCounts": {
                stage: dict(counts)
                for stage, counts in self.stage_availability_counts.items()
            },
        }


def summarize_safety_signals(signals: Iterable[SafetySignal]) -> SafetyNlpTelemetrySummary:
    """Summarize signals without retaining text/evidence fields."""
    signal_tuple = tuple(signals)
    status_counts: dict[str, int] = {}
    channel_counts: dict[str, int] = {}
    stage_counts: dict[str, int] = {}
    stage_latency_ms: dict[str, int] = {}
    stage_availability_counts: dict[str, dict[str, int]] = {}
    uncertainty_count = 0
    abstentions = 0

    for signal in signal_tuple:
        _increment(status_counts, signal.status)
        _increment(channel_counts, signal.channel)
        _increment(stage_counts, signal.stage)
        stage_latency_ms[signal.stage] = stage_latency_ms.get(signal.stage, 0) + int(signal.latency_ms)
        availability = stage_availability_counts.setdefault(signal.stage, {})
        _increment(availability, signal.status)
        if _is_uncertain(signal):
            uncertainty_count += 1
        if _is_abstention(signal):
            abstentions += 1

    return SafetyNlpTelemetrySummary(
        total_signals=len(signal_tuple),
        triggered_signals=sum(1 for signal in signal_tuple if signal.triggered),
        abstentions=abstentions,
        uncertainty_count=uncertainty_count,
        disagreement_count=_disagreement_count(signal_tuple),
        status_counts=status_counts,
        channel_counts=channel_counts,
        stage_counts=stage_counts,
        stage_latency_ms=stage_latency_ms,
        stage_availability_counts=stage_availability_counts,
    )


def sanitized_signal_record(signal: SafetySignal) -> dict:
    """Return a bus/audit-safe signal record with no evidence or patient text."""
    return {
        "channel": signal.channel,
        "triggered": signal.triggered,
        "category": signal.category,
        "assertion": signal.assertion,
        "temporality": signal.temporality,
        "subject": signal.subject,
        "confidence": float(signal.confidence),
        "sourceLanguage": signal.source_language,
        "stage": signal.stage,
        "modelName": signal.model_name,
        "modelRevision": signal.model_revision,
        "latencyMs": signal.latency_ms,
        "status": signal.status,
        "policyVersion": signal.policy_version,
    }


def _is_uncertain(signal: SafetySignal) -> bool:
    return (
        signal.status != "success"
        or signal.assertion in UNCERTAIN_ASSERTIONS
        or signal.subject in UNCERTAIN_CONTEXTS
        or signal.temporality in UNCERTAIN_CONTEXTS
    )


def _is_abstention(signal: SafetySignal) -> bool:
    return signal.status != "success" or signal.category is None or not signal.triggered


def _disagreement_count(signals: tuple[SafetySignal, ...]) -> int:
    successful = [signal for signal in signals if signal.status == "success"]
    if len(successful) < 2:
        return 0
    outcomes = {
        (signal.channel, signal.triggered, signal.category)
        for signal in successful
    }
    triggered_categories = {
        signal.category
        for signal in successful
        if signal.triggered and signal.category is not None
    }
    trigger_votes = {signal.triggered for signal in successful}
    return int(len(trigger_votes) > 1 or len(triggered_categories) > 1 or len(outcomes) > 1)


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1

