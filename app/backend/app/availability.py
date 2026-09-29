"""[MLOps] Availability as the courseware defines it: YIELD and HARVEST.

Lecture 03 ("Logging and Monitoring") teaches three availability measures and
CareRoute computed none of them:

  uptime   (MTBF - MTTR) / MTBF — the fraction of wall-clock time the service
           is up. Prometheus measures the same quantity directly as
           `avg_over_time(up[30d])`, without having to estimate two means.
  yield    requests SERVED / requests RECEIVED. Not the same as uptime: a
           service that is up but shedding load has a yield below 1 and an
           uptime of 1.
  harvest  how COMPLETE each served answer was. The measure that matters for a
           system built to degrade rather than fail — which is exactly what
           CareRoute does: a missing embedder drops to lexical retrieval, a
           missing OneMap key drops to a local travel estimate, a missing model
           drops to the deterministic keyword table. Every one of those still
           returns a triage, and none of them returns the whole answer.

Fox & Brewer's point, and the reason this file exists: **a system trades
harvest to hold yield**. CareRoute makes that trade deliberately, in a dozen
`except` blocks, and until now it was invisible — every degraded answer was
counted as a success identical to a complete one.

WHAT "APPLICABLE" MEANS, and why the denominator is not just 5
--------------------------------------------------------------
A case with no location cannot be given a route. Counting that as a degraded
route would make harvest a measure of how many patients shared their location,
which says nothing about the service. Components whose *inputs* were absent are
therefore scored `None` (not applicable) and leave the denominator, so harvest
only ever answers "of what this case could have been given, how much was it?".
"""
from __future__ import annotations

#: The parts of a triage answer that degrade independently, in the order a
#: reader of the case record meets them.
COMPONENTS = ("assessment", "explanation", "citations", "clinic", "route")


def components(state) -> dict[str, bool | None]:
    """Per-component: True present, False degraded, None not applicable.

    `state` is a CaseState. Read-only — this never touches the case.
    """
    # The trained model answered, rather than the keyword fallback. The
    # provenance field is the honest signal: "shap" means real TreeExplainer
    # values off the served model, "llm" and "keyword" are the two fallbacks.
    # See CaseState.explanation_source.
    source = getattr(state, "explanation_source", "") or ""
    has_location = getattr(state, "latitude", None) is not None and \
        getattr(state, "longitude", None) is not None

    return {
        "assessment": source == "shap",
        "explanation": bool(getattr(state, "explanation", None)),
        "citations": bool(getattr(state, "citations", None)),
        "clinic": bool(getattr(state, "clinic", None)),
        # Without coordinates there is no route to compute, and the absence is
        # the patient's choice rather than a degradation of the service.
        "route": bool(getattr(state, "route_available", False)) if has_location else None,
    }


def harvest(state) -> tuple[float | None, dict[str, bool | None]]:
    """(harvest score over the APPLICABLE components, the component map).

    None when nothing was applicable — which cannot happen today, since four of
    the five components always are, but a mean over an empty set is undefined
    and reporting 0.0 for it would read as total degradation.
    """
    parts = components(state)
    applicable = [present for present in parts.values() if present is not None]
    if not applicable:
        return None, parts
    return sum(1 for present in applicable if present) / len(applicable), parts
