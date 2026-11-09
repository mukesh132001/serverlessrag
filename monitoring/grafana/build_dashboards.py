#!/usr/bin/env python3
"""Build the Grafana dashboards as JSON (provisioned from monitoring/grafana/dashboards).

    python3 monitoring/grafana/build_dashboards.py

- serverlessrag-red.json  traffic, errors, latency (RED), pipeline stages, tokens, uptime probe
- serverlessrag-slo.json  SLIs, error budget remaining, burn rates, firing SLO alerts
"""
import json
import os

DS = {"type": "prometheus", "uid": "prometheus"}
REQ = "rag_request_duration_seconds"
API_ROUTES = 'route=~"/chat|/ingest"'

_next_id = 0


def _id():
    global _next_id
    _next_id += 1
    return _next_id


def target(expr, legend="", ref="A", instant=False):
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}
    if instant:
        t.update({"instant": True, "range": False})
    return t


def timeseries(title, targets, x, y, w=12, h=8, unit="short", description="", thresholds=None, stack=False, min_=None):
    panel = {
        "id": _id(), "type": "timeseries", "title": title, "description": description, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(e, l, chr(65 + i)) for i, (e, l) in enumerate(targets)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"lineWidth": 2, "fillOpacity": 10, "showPoints": "never",
                           "stacking": {"mode": "normal" if stack else "none"}},
            },
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
    }
    if min_ is not None:
        panel["fieldConfig"]["defaults"]["min"] = min_
    if thresholds:
        panel["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": thresholds}
        panel["fieldConfig"]["defaults"]["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    return panel


def stat(title, expr, x, y, w=4, h=4, unit="short", thresholds=None, description="", decimals=None):
    panel = {
        "id": _id(), "type": "stat", "title": title, "description": description, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(expr, "", "A", instant=True)],
        "fieldConfig": {"defaults": {
            "unit": unit,
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "blue", "value": None}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "none",
                    "textMode": "value"},
    }
    if decimals is not None:
        panel["fieldConfig"]["defaults"]["decimals"] = decimals
    return panel


def row(title, y):
    return {"id": _id(), "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def dashboard(uid, title, panels, variables=None, description=""):
    return {
        "uid": uid, "title": title, "description": description, "tags": ["serverlessrag"],
        "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True,
        "refresh": "30s", "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": variables or []},
        "annotations": {"list": [{
            "name": "SLO alerts", "datasource": DS, "enable": True, "iconColor": "red",
            "expr": 'ALERTS{alertname="SLOErrorBudgetBurn",alertstate="firing"}',
            "titleFormat": "{{slo}} burn {{burn_rate}}x", "tagKeys": "severity,slo",
        }]},
        "links": [{"title": "SLOs", "type": "link", "url": "/d/serverlessrag-slo"},
                  {"title": "RED", "type": "link", "url": "/d/serverlessrag-red"}],
        "panels": panels,
    }


def quantile(q, selector, by=""):
    by_clause = f"le, {by}" if by else "le"
    return f"histogram_quantile({q}, sum by ({by_clause}) (rate({REQ}_bucket{{{selector}}}[$__rate_interval])))"


def red_dashboard():
    route = 'route=~"$route"'
    p = []
    y = 0
    p.append(row("Overview", y)); y += 1
    p += [
        stat("Requests / s", f"sum(rate({REQ}_count{{{API_ROUTES}}}[5m]))", 0, y, unit="reqps", decimals=2),
        stat("5xx error rate (5m)",
             f'sum(rate({REQ}_count{{{API_ROUTES},status_class="5xx"}}[5m])) / sum(rate({REQ}_count{{{API_ROUTES}}}[5m]))',
             4, y, unit="percentunit", decimals=2,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.005}, {"color": "red", "value": 0.02}]),
        stat("/chat p95 (5m)", f'histogram_quantile(0.95, sum by (le) (rate({REQ}_bucket{{route="/chat"}}[5m])))',
             8, y, unit="s", decimals=2,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 3}, {"color": "red", "value": 4}]),
        stat("/chat p50 (5m)", f'histogram_quantile(0.50, sum by (le) (rate({REQ}_bucket{{route="/chat"}}[5m])))',
             12, y, unit="s", decimals=2),
        stat("Uptime probe", 'min(probe_success{job="blackbox-health"})', 16, y, decimals=0,
             thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
             description="blackbox_exporter GET /health from outside the request path"),
        stat("Cold starts (1h)", "sum(increase(rag_cold_starts_total[1h]))", 20, y, decimals=0),
    ]
    y += 4

    p.append(row("Traffic", y)); y += 1
    p += [
        timeseries("Requests / s by route", [(f"sum by (route) (rate({REQ}_count{{{route}}}[$__rate_interval]))", "{{route}}")],
                   0, y, unit="reqps"),
        timeseries("Requests / s by status", [(f"sum by (status_code) (rate({REQ}_count{{{route}}}[$__rate_interval]))", "{{status_code}}")],
                   12, y, unit="reqps", stack=True),
    ]
    y += 8

    p.append(row("Errors", y)); y += 1
    p += [
        timeseries("5xx error ratio by route",
                   [(f'sum by (route) (rate({REQ}_count{{{route},status_class="5xx"}}[$__rate_interval])) / sum by (route) (rate({REQ}_count{{{route}}}[$__rate_interval]))', "{{route}}")],
                   0, y, unit="percentunit", min_=0,
                   thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.005}],
                   description="Dashed line: 0.5% = the availability SLO's error budget"),
        timeseries("4xx (client errors) / s",
                   [(f'sum by (route, status_code) (rate({REQ}_count{{{route},status_class="4xx"}}[$__rate_interval]))', "{{route}} {{status_code}}")],
                   12, y, unit="reqps",
                   description="Validation failures and unknown topics. Not counted against the SLO."),
    ]
    y += 8

    p.append(row("Latency", y)); y += 1
    p += [
        timeseries("/chat latency percentiles",
                   [(quantile(0.50, 'route="/chat"'), "p50"), (quantile(0.95, 'route="/chat"'), "p95"),
                    (quantile(0.99, 'route="/chat"'), "p99")],
                   0, y, unit="s", min_=0,
                   thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 4}],
                   description="Dashed line: 4 s = the p95 latency SLO threshold"),
        timeseries("p95 latency by route", [(quantile(0.95, route, "route"), "{{route}}")], 12, y, unit="s", min_=0),
        timeseries("/chat p95 by pipeline stage",
                   [("histogram_quantile(0.95, sum by (le, stage) (rate(rag_stage_duration_seconds_bucket{route=\"/chat\"}[$__rate_interval])))", "{{stage}}")],
                   0, y + 8, unit="s", min_=0,
                   description="Where the time goes: Titan embedding, pgvector HNSW retrieval, gpt-oss generation"),
        {
            "id": _id(), "type": "heatmap", "title": "/chat latency distribution", "datasource": DS,
            "gridPos": {"x": 12, "y": y + 8, "w": 12, "h": 8},
            "targets": [{"datasource": DS, "refId": "A", "format": "heatmap", "legendFormat": "{{le}}",
                         "expr": f'sum by (le) (increase({REQ}_bucket{{route="/chat"}}[$__rate_interval]))'}],
            "options": {"calculate": False, "yAxis": {"unit": "s"}, "cellGap": 1,
                        "color": {"mode": "scheme", "scheme": "Oranges", "steps": 64}},
        },
    ]
    y += 16

    p.append(row("LLM and runtime", y)); y += 1
    p += [
        timeseries("LLM tokens / min", [("sum by (type) (rate(rag_llm_tokens_total[$__rate_interval])) * 60", "{{type}}")],
                   0, y, unit="short", stack=True),
        timeseries("Cold starts / 5m", [("sum by (job) (increase(rag_cold_starts_total[5m]))", "{{job}}")], 12, y),
    ]
    y += 8

    p.append(row("External uptime probe (blackbox_exporter)", y)); y += 1
    p += [
        timeseries("Probe success", [('probe_success{job="blackbox-health"}', "{{instance}}")], 0, y, min_=0),
        timeseries("Probe duration by phase", [('sum by (phase) (probe_http_duration_seconds{job="blackbox-health"})', "{{phase}}")],
                   12, y, unit="s", stack=True),
    ]

    variables = [{
        "name": "route", "label": "Route", "type": "query", "datasource": DS,
        "query": {"query": f"label_values({REQ}_count, route)", "refId": "route"},
        "definition": f"label_values({REQ}_count, route)",
        "includeAll": True, "multi": True, "allValue": ".*", "refresh": 2,
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
    }]
    return dashboard("serverlessrag-red", "ServerlessRAG / RED (rate, errors, duration)", p, variables,
                     "OpenTelemetry metrics from the ingest and chat Lambdas, via the OTel Collector")


def slo_dashboard():
    sel = 'slo="$slo"'
    p = []
    y = 0
    p.append(row("$slo", y)); y += 1
    p += [
        stat("Objective", f"slo:objective:ratio{{{sel}}}", 0, y, unit="percentunit", decimals=1),
        stat("SLI, last 30 days", f"1 - slo:sli_error:ratio_rate30d{{{sel}}}", 4, y, unit="percentunit", decimals=3,
             thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 0.95}]),
        stat("Error budget remaining", f"slo:error_budget_remaining:ratio{{{sel}}}", 8, y, unit="percentunit", decimals=1,
             thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.25}, {"color": "green", "value": 0.5}],
             description="1 - (30-day error ratio / allowed error ratio). Below 0 the SLO is breached."),
        stat("Burn rate 1h", f"slo:burn_rate:1h{{{sel}}}", 12, y, decimals=2,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 6}, {"color": "red", "value": 14.4}]),
        stat("Burn rate 6h", f"slo:burn_rate:6h{{{sel}}}", 16, y, decimals=2,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 3}, {"color": "red", "value": 6}]),
        stat("Burn rate 3d", f"slo:burn_rate:3d{{{sel}}}", 20, y, decimals=2,
             thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    ]
    y += 4
    p += [
        timeseries("Burn rate", [(f"slo:burn_rate:{w}{{{sel}}}", w) for w in ["5m", "1h", "6h", "1d"]],
                   0, y, min_=0,
                   thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 6}, {"color": "red", "value": 14.4}],
                   description="1 = spending the budget exactly over 30 days. Pages at 14.4x (1h & 5m) and 6x (6h & 30m)."),
        timeseries("Error budget remaining (30-day window)", [(f"slo:error_budget_remaining:ratio{{{sel}}}", "remaining")],
                   12, y, unit="percentunit",
                   thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 0}]),
        timeseries("Error ratio vs. budget", [(f"slo:sli_error:ratio_rate1h{{{sel}}}", "1h error ratio"),
                                             (f"slo:error_budget:ratio{{{sel}}}", "allowed")],
                   0, y + 8, unit="percentunit", min_=0),
        {
            "id": _id(), "type": "table", "title": "Firing / pending SLO alerts", "datasource": DS,
            "gridPos": {"x": 12, "y": y + 8, "w": 12, "h": 8},
            "targets": [{"datasource": DS, "refId": "A", "format": "table", "instant": True, "range": False,
                         "expr": 'ALERTS{alertname="SLOErrorBudgetBurn"}'}],
            "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True, "__name__": True, "alertname": True}}}],
        },
    ]
    variables = [{
        "name": "slo", "label": "SLO", "type": "query", "datasource": DS,
        "query": {"query": "label_values(slo:objective:ratio, slo)", "refId": "slo"},
        "definition": "label_values(slo:objective:ratio, slo)",
        "includeAll": True, "multi": True, "refresh": 1,
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
    }]
    # Repeat the whole row for each SLO.
    p[0]["repeat"] = "slo"
    return dashboard("serverlessrag-slo", "ServerlessRAG / SLOs and error budgets", p, variables,
                     "chat-availability: 99.5% non-5xx. chat-latency: 95% of successful requests < 4 s.")


def main():
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboards")
    os.makedirs(out_dir, exist_ok=True)
    for board in (red_dashboard(), slo_dashboard()):
        path = os.path.join(out_dir, f"{board['uid']}.json")
        with open(path, "w") as f:
            json.dump(board, f, indent=2)
            f.write("\n")
        print(f"wrote {path} ({len(board['panels'])} panels)")


if __name__ == "__main__":
    main()
