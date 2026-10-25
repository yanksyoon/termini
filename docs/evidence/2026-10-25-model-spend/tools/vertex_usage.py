#!/usr/bin/env python3
"""Per-day Vertex AI spend for one project, from the provider's own usage metrics.

Cloud Monitoring's `publisher/online_serving/token_count` is the billable token
count Vertex itself records (not an estimate from a local tokenizer), and
`model_invocation_count` is the call count, labelled by method, response code and
input/output token bucket. Together they answer "what did the calls cost and how
were they shaped" without a billing export.

Usage:
    python3 vertex_usage.py <project> [--days N]

Requires an authenticated `gcloud` (the access token is minted from it) with
`monitoring.timeSeries.list` on the project.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

# List prices, USD per 1M tokens (Vertex AI, global endpoint, Oct 2026).
PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}


def access_token() -> str:
    return subprocess.run(["gcloud", "auth", "print-access-token"],
                          capture_output=True, text=True, check=True).stdout.strip()


def series(project: str, token: str, metric: str, start: str, end: str,
           group_by: str | None = None) -> list:
    params = {
        "filter": f'metric.type="{metric}"',
        "interval.startTime": start,
        "interval.endTime": end,
        "aggregation.alignmentPeriod": "86400s",
        "aggregation.perSeriesAligner": "ALIGN_SUM",
    }
    if group_by:
        params["aggregation.crossSeriesReducer"] = "REDUCE_SUM"
        params["aggregation.groupByFields"] = group_by
    url = (f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries?"
           + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode()).get("timeSeries", [])
    except urllib.error.HTTPError as exc:  # a metric with no data 404s
        print(f"  ({metric}: HTTP {exc.code} — no data)")
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--price-model", default="gemini-2.5-flash", choices=sorted(PRICES))
    args = ap.parse_args()

    end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    start = end - dt.timedelta(days=args.days)
    token = access_token()

    tokens: dict[str, dict] = {}
    for s in series(args.project, token,
                    "aiplatform.googleapis.com/publisher/online_serving/token_count",
                    start.isoformat(), end.isoformat(), "metric.labels.type"):
        kind = s["metric"]["labels"]["type"]
        for pt in s["points"]:
            day = pt["interval"]["endTime"][:10]
            tokens.setdefault(day, {"input": 0, "output": 0})[kind] = int(
                pt["value"].get("int64Value", 0))

    calls = collections.Counter()
    for s in series(args.project, token,
                    "aiplatform.googleapis.com/publisher/online_serving/model_invocation_count",
                    start.isoformat(), end.isoformat()):
        for pt in s["points"]:
            calls[pt["interval"]["endTime"][:10]] += int(pt["value"].get("int64Value", 0))

    status = collections.Counter()
    for s in series(args.project, token,
                    "aiplatform.googleapis.com/publisher/online_serving/model_invocation_count",
                    start.isoformat(), end.isoformat(), "metric.labels.response_code"):
        code = s["metric"]["labels"].get("response_code", "?")
        status[code] += sum(int(pt["value"].get("int64Value", 0)) for pt in s["points"])

    pin, pout = PRICES[args.price_model]
    print(f"\n{args.project}  ({args.days}d to {end.date()})  priced as {args.price_model}\n")
    print(f"{'day':<12}{'input':>14}{'output':>12}{'calls':>8}{'cost USD':>11}")
    total = 0.0
    for day in sorted(tokens):
        v = tokens[day]
        cost = v["input"] / 1e6 * pin + v["output"] / 1e6 * pout
        total += cost
        print(f"{day:<12}{v['input']:>14,}{v['output']:>12,}{calls[day]:>8,}{cost:>11.2f}")
    print(f"{'TOTAL':<12}{sum(v['input'] for v in tokens.values()):>14,}"
          f"{sum(v['output'] for v in tokens.values()):>12,}{sum(calls.values()):>8,}{total:>11.2f}")
    print(f"\nresponse codes: {dict(status)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
