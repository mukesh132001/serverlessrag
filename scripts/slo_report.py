#!/usr/bin/env python3
"""Pull your measured SLO numbers from Prometheus and draft the resume bullet.

  python3 scripts/slo_report.py --prometheus http://localhost:9090 --window 7d

Every number printed comes from Prometheus queries over --window, nothing is
estimated. If there is too little traffic in the window the script says so
instead of printing a number. Standard library only.
"""
import argparse
import json
import math
import sys
import urllib.parse
import urllib.request

REQ = "rag_request_duration_seconds"


def query(prom, expr):
    url = f"{prom.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    if data.get("status") != "success":
        raise RuntimeError(f"query failed: {expr}: {data}")
    result = data["data"]["result"]
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if math.isnan(value) or math.isinf(value) else value


def labelled(prom, expr, label):
    url = f"{prom.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    return {s["metric"].get(label, "?"): float(s["value"][1]) for s in data["data"]["result"]}


def fmt_ms(seconds):
    return "n/a" if seconds is None else f"{seconds * 1000:,.0f} ms"


def fmt_pct(ratio, digits=2):
    return "n/a" if ratio is None else f"{ratio * 100:.{digits}f}%"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prometheus", default="http://localhost:9090")
    p.add_argument("--window", default="7d", help="PromQL range, e.g. 1h, 24h, 7d, 30d")
    p.add_argument("--min-requests", type=int, default=200, help="refuse to report on less traffic than this")
    args = p.parse_args()
    prom, w = args.prometheus, args.window

    chat = 'route="/chat"'
    total = query(prom, f"sum(increase({REQ}_count{{{chat}}}[{w}]))")
    if total is None or total < args.min_requests:
        print(f"Only {0 if total is None else round(total)} /chat requests in the last {w}; need {args.min_requests}+ "
              f"for numbers worth quoting. Run scripts/load_test.py or widen --window.")
        sys.exit(1)

    errors = query(prom, f'sum(increase({REQ}_count{{{chat},status_class="5xx"}}[{w}]))') or 0.0
    ok_total = query(prom, f'sum(increase({REQ}_count{{{chat},status_class="2xx"}}[{w}]))') or 0.0
    under_4s = query(prom, f'sum(increase({REQ}_bucket{{{chat},status_class="2xx",le=~"4|4.0"}}[{w}]))') or 0.0
    q = lambda qq, sel=chat: query(prom, f"histogram_quantile({qq}, sum by (le) (increase({REQ}_bucket{{{sel}}}[{w}])))")
    p50, p95, p99 = q(0.50), q(0.95), q(0.99)
    stages = labelled(prom, f'histogram_quantile(0.95, sum by (le, stage) (increase(rag_stage_duration_seconds_bucket{{{chat}}}[{w}])))', "stage")
    tokens = labelled(prom, f"sum by (type) (increase(rag_llm_tokens_total[{w}]))", "type")
    uptime = query(prom, f'avg_over_time(probe_success{{job="blackbox-health"}}[{w}])')
    probe_p95 = query(prom, f'quantile_over_time(0.95, probe_duration_seconds{{job="blackbox-health"}}[{w}])')
    budget_avail = query(prom, 'slo:error_budget_remaining:ratio{slo="chat-availability"}')
    budget_lat = query(prom, 'slo:error_budget_remaining:ratio{slo="chat-latency"}')

    success = 1 - errors / total
    fast = under_4s / ok_total if ok_total else None

    print(f"ServerlessRAG /chat, last {w} (source: {prom})")
    print(f"  requests            {total:,.0f}  ({errors:,.0f} 5xx)")
    print(f"  success (non-5xx)   {fmt_pct(success, 3)}   SLO 99.5%  -> {'MET' if success >= 0.995 else 'MISSED'}")
    print(f"  latency p50/p95/p99 {fmt_ms(p50)} / {fmt_ms(p95)} / {fmt_ms(p99)}  (histogram estimate)")
    print(f"  under 4 s (2xx)     {fmt_pct(fast)}   SLO 95%    -> {'MET' if fast is not None and fast >= 0.95 else 'MISSED'}")
    if stages:
        print("  p95 by stage        " + ", ".join(f"{k} {fmt_ms(v)}" for k, v in sorted(stages.items())))
    if tokens:
        print("  tokens              " + ", ".join(f"{k} {v:,.0f}" for k, v in sorted(tokens.items())))
    print(f"  uptime probe        {fmt_pct(uptime, 3)} of probes passed, p95 probe time {fmt_ms(probe_p95)}")
    print(f"  30d budget left     availability {fmt_pct(budget_avail, 1)}, latency {fmt_pct(budget_lat, 1)}")

    target_ms = 4000
    if success < 0.995 or fast is None or fast < 0.95:
        print("\nNote: an SLO was missed in this window. Fix or re-measure before quoting these numbers.")
    print("\nDraft resume bullet (check every number against the lines above):")
    print(
        f"- Defined SLOs (99.5% success, p95 < {target_ms:,} ms) for a serverless RAG API on AWS Lambda; exported "
        f"OpenTelemetry metrics through an OTel Collector to Prometheus and built Grafana RED and error-budget dashboards "
        f"with multi-window burn-rate alerts and a blackbox_exporter uptime probe; over {w} measured "
        f"{fmt_pct(success, 2)} success and p95 {fmt_ms(p95)} across {total:,.0f} requests."
    )


if __name__ == "__main__":
    main()
