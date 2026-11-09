#!/usr/bin/env python3
"""Generate Prometheus recording and alerting rules for the ServerlessRAG SLOs.

Edit the SLOS list, then run:
    python3 monitoring/prometheus/generate_slo_rules.py

Writes monitoring/prometheus/rules/slo.yml. Alerts follow the multi-window,
multi-burn-rate pattern from the Google SRE Workbook (chapter 5, "Alerting on
SLOs"): an alert fires only when both a long and a short window are burning
the error budget faster than the given rate, so it pages quickly on big
outages, catches slow leaks, and resets soon after recovery.
"""
import os

import yaml

SLO_PERIOD = "30d"

# Each SLO is a ratio of bad events to all events, built from the request
# duration histogram exported by common/telemetry.py.
COUNT = "rag_request_duration_seconds_count"
BUCKET = "rag_request_duration_seconds_bucket"

SLOS = [
    {
        "name": "chat-availability",
        "description": "99.5% of POST /chat requests return a non-5xx response",
        "objective": 0.995,
        "bad": f'sum(rate({COUNT}{{route="/chat",status_class="5xx"}}[{{w}}]))',
        "total": f'sum(rate({COUNT}{{route="/chat"}}[{{w}}]))',
    },
    {
        "name": "chat-latency",
        # p95 < 4 s, expressed as "95% of successful requests faster than 4 s" so it
        # has an error budget. 4 s must be a histogram bucket boundary
        # (common/telemetry.py LATENCY_BUCKETS_S). Prometheus 3 writes le="4.0".
        "description": "95% of successful POST /chat requests complete in under 4 s (p95 < 4 s)",
        "objective": 0.95,
        "threshold_seconds": 4,
        "bad": (
            f'(sum(rate({COUNT}{{route="/chat",status_class="2xx"}}[{{w}}]))'
            f' - sum(rate({BUCKET}{{route="/chat",status_class="2xx",le=~"4|4.0"}}[{{w}}])))'
        ),
        "total": f'sum(rate({COUNT}{{route="/chat",status_class="2xx"}}[{{w}}]))',
    },
]

# (severity, burn rate, long window, short window, for)
# Burn rate 1 spends exactly the whole budget in the SLO period.
BURN_ALERTS = [
    ("page", 14.4, "1h", "5m", "2m"),    # 2% of a 30-day budget in 1 hour
    ("page", 6, "6h", "30m", "15m"),     # 5% in 6 hours
    ("ticket", 3, "1d", "2h", "1h"),     # 10% in 1 day
    ("ticket", 1, "3d", "6h", "3h"),     # 10% in 3 days
]

WINDOWS = ["5m", "30m", "1h", "2h", "6h", "1d", "3d", SLO_PERIOD]


def ratio_record(slo, window):
    return {
        "record": f"slo:sli_error:ratio_rate{window}",
        "expr": f'({slo["bad"].replace("{w}", window)})\n/\n({slo["total"].replace("{w}", window)})',
        "labels": {"slo": slo["name"]},
    }


def build():
    groups = []
    for slo in SLOS:
        name = slo["name"]
        budget = round(1 - slo["objective"], 6)
        sel = f'{{slo="{name}"}}'
        records = [ratio_record(slo, w) for w in WINDOWS]
        records += [
            {"record": "slo:objective:ratio", "expr": str(slo["objective"]), "labels": {"slo": name}},
            {"record": "slo:error_budget:ratio", "expr": str(budget), "labels": {"slo": name}},
            # Fraction of the 30-day error budget still unspent (1 = untouched, <0 = blown).
            {
                "record": "slo:error_budget_remaining:ratio",
                "expr": f"1 - (slo:sli_error:ratio_rate{SLO_PERIOD}{sel} / {budget})",
                "labels": {"slo": name},
            },
            # Current burn rates, for the dashboard.
            *[
                {
                    "record": f"slo:burn_rate:{w}",
                    "expr": f"slo:sli_error:ratio_rate{w}{sel} / {budget}",
                    "labels": {"slo": name},
                }
                for w in ["5m", "1h", "6h", "1d", "3d"]
            ],
        ]
        groups.append({"name": f"slo-{name}-recording", "interval": "30s", "rules": records})

        alerts = []
        for severity, rate, long_w, short_w, for_ in BURN_ALERTS:
            threshold = round(rate * budget, 6)
            alerts.append({
                "alert": "SLOErrorBudgetBurn",
                "expr": (
                    f"slo:sli_error:ratio_rate{long_w}{sel} > {threshold}\n"
                    f"and\nslo:sli_error:ratio_rate{short_w}{sel} > {threshold}"
                ),
                "for": for_,
                "labels": {
                    "severity": severity,
                    "slo": name,
                    "burn_rate": str(rate),
                    "long_window": long_w,
                    "short_window": short_w,
                },
                "annotations": {
                    "summary": f"{name}: error budget burning {rate}x too fast ({long_w}/{short_w} windows)",
                    "description": (
                        f"SLO: {slo['description']}. "
                        f"Error ratio over {long_w} is {{{{ $value | humanizePercentage }}}} "
                        f"(threshold {threshold * 100:.3g}% = {rate}x the {budget * 100:.3g}% budget). "
                        f"At this rate the 30-day budget is gone in {30 / rate:.3g} days."
                    ),
                    "dashboard": "/d/serverlessrag-slo",
                },
            })
        groups.append({"name": f"slo-{name}-alerts", "rules": alerts})
    return {"groups": groups}


def _literal_presenter(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None)


def main():
    yaml.add_representer(str, _literal_presenter)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules", "slo.yml")
    header = "# GENERATED by monitoring/prometheus/generate_slo_rules.py; edit that file, not this one.\n"
    with open(out, "w") as f:
        f.write(header)
        yaml.dump(build(), f, sort_keys=False, width=1000)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
