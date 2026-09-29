"""Generate monitoring/grafana/dashboards/careroute-triage.json.

The dashboard plots every metric app/metrics.py exports, the recording rules in
monitoring/alert.rules.yml, the backend's process runtime and the monitoring
stack itself. It is generated because ~75 hand-edited panels drift: a renamed
metric, a copied panel with the old legend, a gridPos overlap. Edit this file,
then run it:

    python monitoring/grafana/generate_dashboard.py [extra output path ...]

Pass the infra repo's copy as an extra path
(careroute_ai_infra/modules/monitoring_stack/config/careroute-triage.json) so
its config_drift CI job stays green. backend/tests/test_dashboard_metrics.py
fails if a panel reads a metric that does not exist, or a metric has no panel.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = [os.path.join(HERE, "dashboards", "careroute-triage.json"), *sys.argv[1:]]

DS = {"type": "prometheus", "uid": "${datasource}"}
BE = 'job="careroute-backend"'
RI = "$__rate_interval"

panels = []
_id = [0]
_y = [0]
_x = [0]
_rowh = [0]


def nid():
    _id[0] += 1
    return _id[0]


def row(title):
    newline()
    panels.append({
        "id": nid(), "type": "row", "title": title, "collapsed": False,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": _y[0]}, "panels": [],
    })
    _y[0] += 1


def newline():
    if _x[0]:
        _y[0] += _rowh[0]
    _x[0] = 0
    _rowh[0] = 0


def place(w, h):
    if _x[0] + w > 24:
        newline()
    pos = {"h": h, "w": w, "x": _x[0], "y": _y[0]}
    _x[0] += w
    _rowh[0] = max(_rowh[0], h)
    return pos


def tgt(expr, legend="", ref="A", instant=False, fmt=None):
    t = {"refId": ref, "datasource": DS, "expr": expr, "legendFormat": legend}
    if instant:
        t["instant"] = True
        t["range"] = False
    if fmt:
        t["format"] = fmt
    return t


def targets(*pairs, **kw):
    out = []
    for i, p in enumerate(pairs):
        expr, legend = p if isinstance(p, tuple) else (p, "")
        out.append(tgt(expr, legend, ref=chr(65 + i), **kw))
    return out


def panel(kind, title, desc, tg, w=12, h=8, unit=None, mn=None, mx=None,
          stack=False, thresholds=None, options=None, custom=None, extra=None):
    defaults = {}
    if unit:
        defaults["unit"] = unit
    if mn is not None:
        defaults["min"] = mn
    if mx is not None:
        defaults["max"] = mx
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
        defaults["color"] = {"mode": "thresholds"}
    c = dict(custom or {})
    if kind == "timeseries":
        c.setdefault("drawStyle", "line")
        c.setdefault("fillOpacity", 25 if stack else 10)
        c.setdefault("showPoints", "never")
        if stack:
            c["stacking"] = {"mode": "normal", "group": "A"}
    if c:
        defaults["custom"] = c
    p = {
        "id": nid(), "type": kind, "title": title, "description": desc,
        "datasource": DS, "gridPos": place(w, h),
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "targets": tg,
    }
    if options:
        p["options"] = options
    if extra:
        p.update(extra)
    panels.append(p)


def ts(title, desc, *pairs, **kw):
    panel("timeseries", title, desc, targets(*pairs), **kw)


def stat(title, desc, expr, unit=None, thresholds=None, legend="", text_mode="value",
         instant=True, w=4, h=4, decimals=None, mn=None, mx=None, no_value=None):
    opts = {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "textMode": text_mode, "colorMode": "background" if thresholds else "value",
        "graphMode": "none", "justifyMode": "center",
    }
    panel("stat", title, desc, [tgt(expr, legend, instant=instant)], w=w, h=h, unit=unit,
          thresholds=thresholds, options=opts, mn=mn, mx=mx,
          extra={"fieldConfig": {"defaults": {
              k: v for k, v in {
                  "unit": unit, "decimals": decimals, "min": mn, "max": mx, "noValue": no_value,
                  "thresholds": {"mode": "absolute", "steps": thresholds} if thresholds else None,
                  "color": {"mode": "thresholds"} if thresholds else {"mode": "fixed", "fixedColor": "blue"},
              }.items() if v is not None}, "overrides": []}})


def pie(title, desc, expr, legend, w=8, h=8):
    panel("piechart", title, desc, [tgt(expr, legend, instant=True)], w=w, h=h,
          options={
              "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
              "pieType": "donut", "legend": {"displayMode": "table", "placement": "right",
                                             "values": ["value", "percent"]},
          })


def heat(title, desc, expr, w=12, h=8, unit=None):
    panel("heatmap", title, desc, [tgt(expr, "{{le}}", fmt="heatmap")], w=w, h=h,
          options={
              "calculate": False, "yAxis": {"unit": unit or "short"},
              "cellGap": 1, "color": {"mode": "scheme", "scheme": "Oranges", "steps": 64},
              "tooltip": {"mode": "single", "yHistogram": True},
          })


def bars(title, desc, expr, legend, unit=None, w=12, h=8):
    panel("bargauge", title, desc, [tgt(expr, legend, instant=True)], w=w, h=h, unit=unit,
          options={
              "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
              "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
          })


def table(title, desc, expr, w=24, h=7):
    panel("table", title, desc, [tgt(expr, "", instant=True, fmt="table")], w=w, h=h,
          extra={"transformations": [{"id": "organize", "options": {
              "excludeByName": {"Time": True, "Value": True, "__name__": True}}}]})


GREEN, AMBER, RED = "green", "orange", "red"
AG = 'agent=~"$agent"'
MD = 'model=~"$model"'

# ── Overview ────────────────────────────────────────────────────────────────
row("Overview")
stat("Backend tasks up", "Backend tasks Prometheus is scraping and reaching. 0 = the triage API is down or unregistered.",
     f"sum(up{{{BE}}}) or vector(0)", thresholds=[{"color": RED, "value": None}, {"color": GREEN, "value": 1}])
stat("Triage requests / min", "All triage requests received, every outcome.",
     f"sum(rate(careroute_triage_requests_total[{RI}])) * 60", unit="reqpm", instant=False, decimals=1)
stat("Yield (5m)", "Served / received, from the careroute:yield:ratio5m recording rule. Alert below 95%. "
     "Absent with no traffic: a ratio over zero requests is undefined, not 0%.",
     "min(careroute:yield:ratio5m)", unit="percentunit", decimals=1, no_value="no traffic",
     thresholds=[{"color": RED, "value": None}, {"color": AMBER, "value": 0.9}, {"color": GREEN, "value": 0.95}])
stat("Harvest (5m)", "Mean completeness of served answers. Alert below 60% while yield is healthy. "
     "Only services that serve answers report it (agent containers do not).",
     "min(careroute:harvest:mean5m)", unit="percentunit", decimals=1, no_value="no traffic",
     thresholds=[{"color": RED, "value": None}, {"color": AMBER, "value": 0.6}, {"color": GREEN, "value": 0.8}])
stat("Escalation share (range)", "Cases escalated to a clinician over the selected range, as a share of triage requests. Alert above 60%.",
     "sum(increase(careroute_escalations_total[$__range])) / clamp_min(sum(increase(careroute_triage_requests_total[$__range])), 1)",
     unit="percentunit", decimals=1,
     thresholds=[{"color": GREEN, "value": None}, {"color": AMBER, "value": 0.4}, {"color": RED, "value": 0.6}])
stat("Alerts firing", "Prometheus alerts currently firing (ALERTS series). Details in the Alerts row.",
     'count(ALERTS{alertstate="firing"}) or vector(0)',
     thresholds=[{"color": GREEN, "value": None}, {"color": RED, "value": 1}])
stat("LLM spend (range)", "Estimated LLM spend over the selected range, priced models only.",
     "sum(increase(careroute_llm_cost_usd_total[$__range])) or vector(0)", unit="currencyUSD", decimals=3)
stat("LLM cost / triage (30m)", "Same formula as the LLMCostPerTriageHigh alert: warns at $0.096 (80% of the $0.12 efficiency gate).",
     "sum(rate(careroute_llm_cost_usd_total[30m])) / clamp_min(sum(rate(careroute_triage_requests_total[30m])), 0.001)",
     unit="currencyUSD", decimals=4,
     thresholds=[{"color": GREEN, "value": None}, {"color": AMBER, "value": 0.096}, {"color": RED, "value": 0.12}])
stat("LLM p95 latency", "95th percentile wall-clock of LLM calls, all models and outcomes. Alert above 8 s.",
     f"histogram_quantile(0.95, sum(rate(careroute_llm_latency_seconds_bucket[{RI}])) by (le))",
     unit="s", instant=False, decimals=2,
     thresholds=[{"color": GREEN, "value": None}, {"color": AMBER, "value": 5}, {"color": RED, "value": 8}])
stat("LLM cache hit ratio (range)", "Share of LLM lookups answered by the exact-match cache instead of a paid provider call.",
     'sum(increase(careroute_llm_cache_total{outcome="hit"}[$__range])) / clamp_min(sum(increase(careroute_llm_cache_total[$__range])), 1)',
     unit="percentunit", decimals=1)
stat("Deployed model", "Severity-model version the backend reports (careroute_model_info).",
     "max(careroute_model_info) by (version)", legend="{{version}}", text_mode="name")
stat("Uptime (range)", "Fraction of scrapes that reached a backend task over the selected range.",
     f"avg(avg_over_time(up{{{BE}}}[$__range]))", unit="percentunit", decimals=2,
     thresholds=[{"color": RED, "value": None}, {"color": AMBER, "value": 0.99}, {"color": GREEN, "value": 0.999}])

# ── Traffic & outcomes ──────────────────────────────────────────────────────
row("Traffic & outcomes")
ts("Triage request rate (by outcome)", "Requests per second by outcome, plus the total.",
   (f"sum(rate(careroute_triage_requests_total[{RI}])) by (outcome)", "{{outcome}}"),
   (f"sum(rate(careroute_triage_requests_total[{RI}]))", "total"), w=16, unit="reqps")
pie("Outcome mix (range)", "How the selected range's requests ended.",
    "sum(increase(careroute_triage_requests_total[$__range])) by (outcome)", "{{outcome}}")
ts("Human-escalation rate (share of requests)", "Escalations / triage requests over 15m, the EscalationRateAnomaly formula.",
   ("sum(rate(careroute_escalations_total[15m])) / clamp_min(sum(rate(careroute_triage_requests_total[15m])), 0.001)", "escalation share"),
   unit="percentunit", mn=0, mx=1)
ts("Escalations / min", "Cases handed to a clinician per minute.",
   (f"sum(rate(careroute_escalations_total[{RI}])) * 60", "escalations"), unit="short")

# ── Availability ────────────────────────────────────────────────────────────
row("Availability — yield, harvest, uptime")
ts("Availability — yield (served / received)", "careroute:yield:ratio5m. Received includes failed runs and requests shed before work ran.",
   ("careroute:yield:ratio5m", "yield"), unit="percentunit", mn=0, mx=1, w=8)
ts("Availability — harvest (how complete the answers were)", "careroute:harvest:mean5m. Yield can sit at 100% while this falls.",
   ("careroute:harvest:mean5m", "harvest"), unit="percentunit", mn=0, mx=1, w=8)
ts("Uptime (30d recording rule)", "careroute:availability:ratio30d. On AWS Prometheus keeps 6 h of data, so this is the uptime of whatever is retained.",
   ("careroute:availability:ratio30d", "{{instance}}"), unit="percentunit", mn=0, mx=1, w=8)
ts("Which part of the answer is missing", "Degraded answer components per second. not_applicable is excluded: a case with no coordinates was never owed a route.",
   (f'sum(rate(careroute_answer_components_total{{state="degraded"}}[{RI}])) by (component)', "{{component}}"), w=12, stack=True)
ts("Answer components by state", "Every component, present / degraded / not_applicable.",
   (f"sum(rate(careroute_answer_components_total[{RI}])) by (component, state)", "{{component}} · {{state}}"), w=12)
heat("Harvest distribution", "How complete each served answer was (careroute_answer_harvest buckets).",
     f"sum(increase(careroute_answer_harvest_bucket[{RI}])) by (le)", w=24, unit="percentunit")

# ── Severity model ──────────────────────────────────────────────────────────
row("Severity model (MLOps)")
ts("Model inference latency (p50 / p95 / p99)", "Severity-model inference including SHAP. Alert: p95 above 1.5 s.",
   (f"histogram_quantile(0.50, sum(rate(careroute_model_predict_seconds_bucket[{RI}])) by (le))", "p50"),
   (f"histogram_quantile(0.95, sum(rate(careroute_model_predict_seconds_bucket[{RI}])) by (le))", "p95"),
   (f"histogram_quantile(0.99, sum(rate(careroute_model_predict_seconds_bucket[{RI}])) by (le))", "p99"), unit="s", w=12)
ts("Predictions by acuity", "Served predictions per second by acuity class. A shift in the mix is drift before labels exist.",
   (f"sum(rate(careroute_model_predictions_total[{RI}])) by (acuity)", "{{acuity}}"), w=12, stack=True)
pie("Acuity mix (range)", "Share of each acuity class over the selected range.",
    "sum(increase(careroute_model_predictions_total[$__range])) by (acuity)", "{{acuity}}")
ts("Model confidence (median / mean)", "Calibrated confidence of served predictions. A slide down is serving skew.",
   (f"histogram_quantile(0.5, sum(rate(careroute_model_confidence_bucket[{RI}])) by (le))", "median"),
   (f"sum(rate(careroute_model_confidence_sum[{RI}])) / clamp_min(sum(rate(careroute_model_confidence_count[{RI}])), 0.001)", "mean"),
   unit="percentunit", mn=0, mx=1, w=8)
ts("Low-confidence share (< 0.6)", "Share of predictions below the 0.6 escalation threshold.",
   (f'sum(rate(careroute_model_confidence_bucket{{le="0.6"}}[{RI}])) / clamp_min(sum(rate(careroute_model_confidence_count[{RI}])), 0.001)', "below 0.6"),
   unit="percentunit", mn=0, mx=1, w=8)
heat("Confidence distribution", "careroute_model_confidence buckets over time.",
     f"sum(increase(careroute_model_confidence_bucket[{RI}])) by (le)", w=12, unit="percentunit")
ts("Clinician decisions (HITL) by agreement", "Escalation decisions: did the clinician's final acuity agree with the model?",
   (f"sum(rate(careroute_hitl_decisions_total[{RI}])) by (agreement)", "{{agreement}}"), w=6, stack=True)
stat("Clinician agreement (range)", "Live accuracy on human-reviewed cases: agreed / (agreed + overridden).",
     'sum(increase(careroute_hitl_decisions_total{agreement="agreed"}[$__range])) / clamp_min(sum(increase(careroute_hitl_decisions_total{agreement=~"agreed|overridden"}[$__range])), 1)',
     unit="percentunit", decimals=1, w=6, h=8,
     thresholds=[{"color": RED, "value": None}, {"color": AMBER, "value": 0.7}, {"color": GREEN, "value": 0.85}])
stat("HITL escalations past SLA (open)", "Pending escalations older than CAREROUTE_HITL_SLA_MINUTES — evaluated when the queue is read.",
     "sum(careroute_hitl_open_overdue)", legend="open overdue", w=8, decimals=0,
     thresholds=[{"color": GREEN, "value": None}, {"color": RED, "value": 1}])
stat("HITL SLA breaches (range)", "Escalations that breached the review SLA (each counted once).",
     "sum(increase(careroute_hitl_sla_breached_total[$__range]))", legend="breached", w=8, decimals=0)
stat("Retrain queue (clinician disagreements, range)", "Clinician overrides queued as labelled retraining examples (app/memory/retrain_queue.py).",
     "sum(increase(careroute_retrain_queue_total[$__range]))", legend="queued", w=8, decimals=0)

# ── Agents ──────────────────────────────────────────────────────────────────
row("Agents")
ts("Per-agent step duration p95", "95th percentile step time per agent. Alert: above 2 s.",
   (f"histogram_quantile(0.95, sum(rate(careroute_agent_duration_seconds_bucket{{{AG}}}[{RI}])) by (le, agent))", "{{agent}}"),
   unit="s", w=12)
ts("Per-agent step duration p50", "Median step time per agent.",
   (f"histogram_quantile(0.50, sum(rate(careroute_agent_duration_seconds_bucket{{{AG}}}[{RI}])) by (le, agent))", "{{agent}}"),
   unit="s", w=12)
ts("Agent step rate by source", "Where each agent's answer came from: llm / model / fallback / deterministic.",
   (f"sum(rate(careroute_agent_duration_seconds_count{{{AG}}}[{RI}])) by (agent, source)", "{{agent}} · {{source}}"),
   w=12, stack=True, unit="ops")
ts("Fallback share per agent", "Share of each agent's steps that ended on the deterministic fallback — an LLM or model that is quietly not answering.",
   (f'sum(rate(careroute_agent_duration_seconds_count{{{AG},source="fallback"}}[{RI}])) by (agent) / clamp_min(sum(rate(careroute_agent_duration_seconds_count{{{AG}}}[{RI}])) by (agent), 0.001)', "{{agent}}"),
   w=12, unit="percentunit", mn=0, mx=1)
ts("Agent container calls by outcome", "Gateway -> agent-container calls (ok / error / rejected / breaker_open), one count per call. Only non-zero when AGENT_TRANSPORT=http — with the in-process transport the call never crosses the network and this metric stays flat. A non-ok outcome means the case was degraded and escalated to a clinician, not silently dropped.",
   (f"sum(rate(careroute_agent_calls_total{{{AG}}}[{RI}])) by (agent, outcome)", "{{agent}} · {{outcome}}"),
   w=12, stack=True, unit="ops")
ts("Agent call failure ratio", "Non-ok share of gateway -> agent-container calls, per agent. Alert-adjacent to the AgentDown signal: a rising ratio here means the container is unhealthy or the breaker is open before it fully stops answering.",
   (f'sum(rate(careroute_agent_calls_total{{{AG},outcome!="ok"}}[{RI}])) by (agent) / clamp_min(sum(rate(careroute_agent_calls_total{{{AG}}}[{RI}])) by (agent), 0.001)', "{{agent}}"),
   w=12, unit="percentunit", mn=0, mx=1)
bars("Mean step time per agent (range)", "Average step duration per agent over the selected range.",
     f"sum(increase(careroute_agent_duration_seconds_sum{{{AG}}}[$__range])) by (agent) / clamp_min(sum(increase(careroute_agent_duration_seconds_count{{{AG}}}[$__range])) by (agent), 1)",
     "{{agent}}", unit="s", w=8)
ts("Tool calls by tool and outcome", "Model-chosen tool calls through the registry gateway.",
   (f"sum(rate(careroute_tool_calls_total{{{AG}}}[{RI}])) by (tool, outcome)", "{{tool}} · {{outcome}}"), w=8, stack=True, unit="ops")
ts("Tool calls not OK", "Refused, invalid-argument, error and unknown-tool calls, by agent.",
   (f'sum(rate(careroute_tool_calls_total{{{AG},outcome!="ok"}}[{RI}])) by (agent, outcome)', "{{agent}} · {{outcome}}"), w=8, unit="ops")
ts("Orchestration plans by shape", "Per-case plans chosen by agents/planner.py (full / emergency / red_flag / clarify / low_confidence / fallback). A rising fallback share means proposed plans are being rejected by the transition graph.",
   (f"sum(rate(careroute_plan_total[{RI}])) by (shape)", "{{shape}}"), w=24, stack=True, unit="ops")

# ── LLM ─────────────────────────────────────────────────────────────────────
row("LLM — cost, tokens, latency, routing")
ts("LLM spend rate (USD / hour) by model", "Estimated spend, extrapolated to an hourly rate.",
   (f"sum(rate(careroute_llm_cost_usd_total{{{MD}}}[{RI}])) by (model) * 3600", "{{model}}"), unit="currencyUSD", w=8, stack=True)
bars("LLM spend by model (range)", "Estimated spend per model over the selected range.",
     f"sum(increase(careroute_llm_cost_usd_total{{{MD}}}[$__range])) by (model)", "{{model}}", unit="currencyUSD", w=8)
ts("LLM cost per triage", "Spend / triage requests over 30m. Warn line: $0.096.",
   ("sum(rate(careroute_llm_cost_usd_total[30m])) / clamp_min(sum(rate(careroute_triage_requests_total[30m])), 0.001)", "cost / triage"),
   unit="currencyUSD", w=8, thresholds=[{"color": GREEN, "value": None}, {"color": AMBER, "value": 0.096}, {"color": RED, "value": 0.12}],
   custom={"thresholdsStyle": {"mode": "line"}})
ts("Tokens / min by model and direction", "Prompt and completion tokens. Alert: above 2000 tokens/s over 15m.",
   (f"sum(rate(careroute_llm_tokens_total{{{MD}}}[{RI}])) by (model, direction) * 60", "{{model}} · {{direction}}"), w=12, stack=True)
ts("Tokens per triage", "Tokens spent per triage request, by direction.",
   (f"sum(rate(careroute_llm_tokens_total[{RI}])) by (direction) / ignoring(direction) group_left clamp_min(sum(rate(careroute_triage_requests_total[{RI}])), 0.001)", "{{direction}}"), w=12)
ts("LLM latency by model (p50 / p95 / p99, OK calls)", "Successful calls only, so a timeout cannot move the healthy percentile.",
   (f'histogram_quantile(0.50, sum(rate(careroute_llm_latency_seconds_bucket{{{MD},outcome="ok"}}[{RI}])) by (le, model))', "p50 {{model}}"),
   (f'histogram_quantile(0.95, sum(rate(careroute_llm_latency_seconds_bucket{{{MD},outcome="ok"}}[{RI}])) by (le, model))', "p95 {{model}}"),
   (f'histogram_quantile(0.99, sum(rate(careroute_llm_latency_seconds_bucket{{{MD},outcome="ok"}}[{RI}])) by (le, model))', "p99 {{model}}"),
   unit="s", w=12)
ts("LLM calls by outcome", "ok / error / timeout per second, by model.",
   (f"sum(rate(careroute_llm_latency_seconds_count{{{MD}}}[{RI}])) by (model, outcome)", "{{model}} · {{outcome}}"), w=6, stack=True, unit="ops")
ts("LLM provider failure rate", "Non-OK share of LLM calls. Alert above 25% over 15m.",
   (f'sum(rate(careroute_llm_latency_seconds_count{{{MD},outcome!="ok"}}[{RI}])) by (model) / clamp_min(sum(rate(careroute_llm_latency_seconds_count{{{MD}}}[{RI}])) by (model), 0.001)', "{{model}}"),
   w=6, unit="percentunit", mn=0, mx=1)
heat("LLM latency distribution", "careroute_llm_latency_seconds buckets, all models.",
     f"sum(increase(careroute_llm_latency_seconds_bucket{{{MD}}}[{RI}])) by (le)", w=12, unit="s")
ts("Router decisions by task and tier", "Which model tier the LLM router picked for each named task.",
   (f"sum(rate(careroute_llm_router_decisions_total[{RI}])) by (task, tier)", "{{task}} · {{tier}}"), w=12, stack=True, unit="ops")
ts("Response-cache hit ratio by task", "Hits / lookups per task. Every hit is a provider call not paid for.",
   (f'sum(rate(careroute_llm_cache_total{{outcome="hit"}}[{RI}])) by (task) / clamp_min(sum(rate(careroute_llm_cache_total[{RI}])) by (task), 0.001)', "{{task}}"),
   w=12, unit="percentunit", mn=0, mx=1)
ts("LLM calls by model and routing reason", "Model each LLM call went to and why: base tier, or the difficulty signal (low_confidence / thin_evidence / rerun) that escalated it; override = the safety adjudicator pinned its own model.",
   (f"sum(rate(careroute_llm_route_total[{RI}])) by (model, reason)", "{{model}} · {{reason}}"), w=12, stack=True, unit="ops")
ts("Per-call LLM guardrail events", "Every LLM call is screened: input PII redaction + injection screen, output LLM05 screen. blocked = the caller fell back to its deterministic path; flagged = counted and audited only.",
   (f"sum(rate(careroute_llm_guard_total[{RI}])) by (task, stage, action)", "{{task}} · {{stage}} · {{action}}"), w=12, stack=True, unit="ops")

# ── Safety & security ───────────────────────────────────────────────────────
row("Safety & security")
ts("Guardrail blocks / output flags / rate-limited", "Input blocks, suppressed LLM outputs and 429s per second. Alert: blocks above 0.2/s.",
   (f"sum(rate(careroute_guardrail_blocks_total[{RI}]))", "input blocks"),
   (f"sum(rate(careroute_output_flags_total[{RI}]))", "output flags"),
   (f"sum(rate(careroute_rate_limited_total[{RI}]))", "rate-limited (429)"), w=12, unit="ops")
ts("Guardrail block share", "Input blocks as a share of triage requests.",
   (f"sum(rate(careroute_guardrail_blocks_total[{RI}])) / clamp_min(sum(rate(careroute_triage_requests_total[{RI}])), 0.001)", "blocked share"),
   w=12, unit="percentunit", mn=0, mx=1)
ts("Untrusted content dropped (indirect injection)", "Retrieved or remembered text dropped by the untrusted-content screen, by channel.",
   (f"sum(rate(careroute_untrusted_content_drops_total[{RI}])) by (channel)", "{{channel}}"), w=8, stack=True, unit="ops")
ts("Abuse monitor events", "Quarantines started and requests refused while quarantined.",
   (f"sum(rate(careroute_abuse_events_total[{RI}])) by (kind)", "{{kind}}"), w=8, unit="ops")
bars("Security events (range)", "Totals over the selected range.",
     'label_replace(sum(increase(careroute_guardrail_blocks_total[$__range])), "event", "input blocks", "", "")'
     ' or label_replace(sum(increase(careroute_output_flags_total[$__range])), "event", "output flags", "", "")'
     ' or label_replace(sum(increase(careroute_rate_limited_total[$__range])), "event", "rate-limited", "", "")'
     ' or label_replace(sum(increase(careroute_untrusted_content_drops_total[$__range])), "event", "untrusted drops", "", "")'
     ' or label_replace(sum(increase(careroute_abuse_events_total[$__range])), "event", "abuse events", "", "")',
     "{{event}}", w=8)

# ── Care routing ────────────────────────────────────────────────────────────
row("Care routing")
ts("Routing fallbacks by reason", "Safe fallbacks in care routing (e.g. straight-line distance when OneMap is unavailable).",
   (f"sum(rate(careroute_routing_fallbacks_total[{RI}])) by (reason)", "{{reason}}"), w=8, stack=True, unit="ops")
ts("Hours-directory freshness", "Freshness of the clinic opening-hours data seen during routing.",
   (f"sum(rate(careroute_routing_data_freshness_total[{RI}])) by (status)", "{{status}}"), w=8, stack=True, unit="ops")
pie("Fallback reasons (range)", "Why routing fell back, over the selected range.",
    "sum(increase(careroute_routing_fallbacks_total[$__range])) by (reason)", "{{reason}}")

# ── Backend runtime ─────────────────────────────────────────────────────────
row("Backend runtime (per task)")
ts("CPU (cores used)", "Process CPU per backend task. The task has 0.5 vCPU on the demo stack.",
   (f"rate(process_cpu_seconds_total{{{BE}}}[{RI}])", "{{instance}}"), w=8, unit="short")
ts("Resident memory", "RSS per backend task. The demo task has 1 GiB; the model warmup OOMs below that.",
   (f"process_resident_memory_bytes{{{BE}}}", "RSS {{instance}}"),
   (f"process_virtual_memory_bytes{{{BE}}}", "virtual {{instance}}"), w=8, unit="bytes")
ts("Open file descriptors", "Open FDs against the limit. SSE streams and outbound HTTP each hold one.",
   (f"process_open_fds{{{BE}}}", "open {{instance}}"),
   (f"process_max_fds{{{BE}}}", "max {{instance}}"), w=8)
ts("Python GC collections / s", "Garbage-collector runs per generation.",
   (f"sum(rate(python_gc_collections_total{{{BE}}}[{RI}])) by (generation)", "gen {{generation}}"), w=8)
ts("Python GC objects / s", "Objects freed per generation, and objects the collector found but could not free (a reference-cycle leak).",
   (f"sum(rate(python_gc_objects_collected_total{{{BE}}}[{RI}])) by (generation)", "collected gen {{generation}}"),
   (f"sum(rate(python_gc_objects_uncollectable_total{{{BE}}}[{RI}])) by (generation)", "uncollectable gen {{generation}}"), w=8)
stat("Process uptime", "Time since the oldest running backend process started. Resets on every deploy, Spot reclaim or crash.",
     f"time() - min(process_start_time_seconds{{{BE}}})", unit="s", w=4, h=8)
stat("Restarts (range)", "Backend process starts seen over the selected range (deploys, Spot reclaims, crashes).",
     f"sum(changes(process_start_time_seconds{{{BE}}}[$__range])) or vector(0)", w=4, h=8,
     thresholds=[{"color": GREEN, "value": None}, {"color": AMBER, "value": 1}, {"color": RED, "value": 3}])

# ── Alerts ──────────────────────────────────────────────────────────────────
row("Alerts")
table("Alerts firing now", "Every Prometheus alert in the firing state, with its labels.",
      'ALERTS{alertstate="firing"}', w=16)
ts("Alerts by state", "Pending and firing alerts over time.",
   ("sum(ALERTS) by (alertstate)", "{{alertstate}}"), w=8, stack=True)

# ── Monitoring stack health ─────────────────────────────────────────────────
row("Monitoring stack health")
ts("Scrape targets up", "1 = Prometheus reached the target on its last scrape.",
   ("up", "{{job}} {{instance}}"), w=8, mn=0, mx=1)
ts("Scrape duration", "How long each scrape took.",
   ("scrape_duration_seconds", "{{job}} {{instance}}"), w=8, unit="s")
ts("Samples per scrape", "Series returned per scrape — a jump means a new high-cardinality label.",
   ("scrape_samples_scraped", "{{job}} {{instance}}"), w=8)
ts("Prometheus head series", "Active series in the TSDB head (drives Prometheus memory).",
   ("prometheus_tsdb_head_series", "series"), w=6)
ts("Samples ingested / s", "Rate of samples appended to the TSDB.",
   (f"rate(prometheus_tsdb_head_samples_appended_total[{RI}])", "samples/s"), w=6)
ts("Rule group evaluation time", "Last evaluation duration per rule group. Should stay far below the 15 s interval.",
   ("prometheus_rule_group_last_duration_seconds", "{{rule_group}}"), w=6, unit="s")
ts("Rule evaluation failures", "Failed rule evaluations per second, by group.",
   (f"sum(rate(prometheus_rule_evaluation_failures_total[{RI}])) by (rule_group)", "{{rule_group}}"), w=6, unit="ops")
ts("Prometheus → Alertmanager notifications", "Alerts sent, failed and dropped on the way to Alertmanager.",
   (f"sum(rate(prometheus_notifications_sent_total[{RI}]))", "sent"),
   (f"sum(rate(prometheus_notifications_errors_total[{RI}]))", "errors"),
   (f"sum(rate(prometheus_notifications_dropped_total[{RI}]))", "dropped"), w=8, unit="ops")
ts("Alertmanager alerts by state", "Alerts Alertmanager is holding: active, suppressed, unprocessed.",
   ('sum(alertmanager_alerts{job="alertmanager"}) by (state)', "{{state}}"), w=8, stack=True)
ts("Alertmanager notifications", "Notifications attempted and failed, by integration. None are configured yet (null receivers).",
   (f'sum(rate(alertmanager_notifications_total{{job="alertmanager"}}[{RI}])) by (integration)', "sent {{integration}}"),
   (f'sum(rate(alertmanager_notifications_failed_total{{job="alertmanager"}}[{RI}])) by (integration)', "failed {{integration}}"), w=8, unit="ops")
ts("Monitoring task memory", "RSS of Prometheus and Alertmanager.",
   ('process_resident_memory_bytes{job=~"prometheus|alertmanager"}', "{{job}}"), w=12, unit="bytes")
ts("Monitoring task CPU", "CPU cores used by Prometheus and Alertmanager.",
   (f'rate(process_cpu_seconds_total{{job=~"prometheus|alertmanager"}}[{RI}])', "{{job}}"), w=12)
newline()


def var(name, label, query):
    return {
        "name": name, "label": label, "type": "query", "datasource": DS,
        "query": {"query": query, "refId": f"{name}-var"}, "definition": query,
        "refresh": 2, "includeAll": True, "multi": True, "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1, "hide": 0,
    }


dashboard = {
    "annotations": {"list": [{
        "builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
        "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
        "name": "Annotations & Alerts", "type": "dashboard",
    }]},
    "description": "Every metric the CareRoute backend exports, its recording rules, the backend process runtime and the monitoring stack itself. Generated; edit the generator, not the JSON, if you have it.",
    "editable": True,
    "graphTooltip": 1,
    "schemaVersion": 39,
    "tags": ["careroute", "mlops", "triage"],
    "title": "CareRoute AI — Triage Observability",
    "uid": "careroute-triage",
    "time": {"from": "now-6h", "to": "now"},
    # 1m, not 30s: ~70 queries per refresh against a 0.25 vCPU Prometheus task.
    "refresh": "1m",
    "templating": {"list": [
        {"name": "datasource", "label": "Data source", "type": "datasource",
         "query": "prometheus", "current": {}, "hide": 0},
        var("agent", "Agent", "label_values(careroute_agent_duration_seconds_count, agent)"),
        var("model", "LLM model", "label_values(careroute_llm_latency_seconds_count, model)"),
    ]},
    "panels": panels,
}

text = json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n"
for path in OUTPUTS:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
print(len([p for p in panels if p["type"] != "row"]), "panels,", len(text), "bytes")
