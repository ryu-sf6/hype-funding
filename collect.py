#!/usr/bin/env python3
"""Collect HYPE perpetual funding data from the public Hyperliquid API.

Runs hourly on GitHub Actions. Stores:
  data/funding_HYPE.csv   hourly funding rates (one row per settlement hour)
  data/mark_HYPE.csv      mark price snapshots (one row per run)
  data/summary.json       current rate, cumulative rates over 24h / 3d / 7d, APR

Market data only. No account, address, or key is used.
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

API = "https://api.hyperliquid.xyz/info"
COIN = "HYPE"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FUNDING_CSV = os.path.join(DATA_DIR, f"funding_{COIN}.csv")
MARK_CSV = os.path.join(DATA_DIR, f"mark_{COIN}.csv")
SUMMARY_JSON = os.path.join(DATA_DIR, "summary.json")
HOUR_MS = 3600 * 1000
LOOKBACK_MS = 9 * 24 * HOUR_MS  # fetch a bit more than 7 days each run
KEEP_MS = 400 * 24 * HOUR_MS    # keep roughly 400 days of history in the CSV


def post(payload, retries=3):
    body = json.dumps(payload).encode()
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                API, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"API request failed: {payload.get('type')}: {last_err}")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_funding():
    rows = {}
    if os.path.exists(FUNDING_CSV):
        with open(FUNDING_CSV, newline="") as f:
            for r in csv.DictReader(f):
                rows[int(r["time_ms"])] = {
                    "time_ms": int(r["time_ms"]),
                    "funding_rate": float(r["funding_rate"]),
                    "premium": float(r["premium"]) if r.get("premium") not in (None, "") else None,
                }
    return rows


def save_funding(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    cutoff = int(time.time() * 1000) - KEEP_MS
    with open(FUNDING_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_ms", "time_utc", "funding_rate", "premium"])
        for t in sorted(rows):
            if t < cutoff:
                continue
            r = rows[t]
            w.writerow([t, iso(t), f"{r['funding_rate']:.10f}",
                        "" if r["premium"] is None else f"{r['premium']:.10f}"])


def fetch_funding_history(now_ms):
    data = post({"type": "fundingHistory", "coin": COIN, "startTime": now_ms - LOOKBACK_MS})
    out = {}
    for item in data:
        t = int(item["time"])
        out[t] = {
            "time_ms": t,
            "funding_rate": float(item["fundingRate"]),
            "premium": float(item["premium"]) if item.get("premium") is not None else None,
        }
    return out


def fetch_current():
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset["name"] == COIN:
            return {
                "funding": float(ctx["funding"]),
                "mark_px": float(ctx["markPx"]),
                "oracle_px": float(ctx["oraclePx"]),
                "open_interest": float(ctx["openInterest"]),
            }
    raise RuntimeError(f"{COIN} not found in metaAndAssetCtxs")


def append_mark(now_ms, cur):
    os.makedirs(DATA_DIR, exist_ok=True)
    new = not os.path.exists(MARK_CSV)
    with open(MARK_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_ms", "time_utc", "mark_px", "oracle_px", "funding_now", "open_interest"])
        w.writerow([now_ms, iso(now_ms), cur["mark_px"], cur["oracle_px"],
                    f"{cur['funding']:.10f}", cur["open_interest"]])


def window_stats(rows, now_ms, hours):
    start = now_ms - hours * HOUR_MS
    rates = [r["funding_rate"] for t, r in rows.items() if start < t <= now_ms]
    if not rates:
        return {"hours_expected": hours, "hours_available": 0, "sum": None,
                "mean_hourly": None, "annualized_from_mean": None,
                "hours_negative": 0, "min": None, "max": None}
    s = sum(rates)
    mean = s / len(rates)
    return {
        "hours_expected": hours,
        "hours_available": len(rates),
        "sum": s,                           # cumulative funding rate over the window (fraction)
        "mean_hourly": mean,                # average hourly rate (fraction)
        "annualized_from_mean": mean * 24 * 365,
        "hours_negative": sum(1 for x in rates if x < 0),
        "min": min(rates),
        "max": max(rates),
    }


def main():
    now_ms = int(time.time() * 1000)
    rows = load_funding()
    rows.update(fetch_funding_history(now_ms))
    save_funding(rows)

    cur = fetch_current()
    append_mark(now_ms, cur)

    latest_t = max(rows) if rows else None
    summary = {
        "coin": COIN,
        "generated_utc": iso(now_ms),
        "current": {
            "funding_hourly": cur["funding"],
            "funding_annualized": cur["funding"] * 24 * 365,
            "mark_px": cur["mark_px"],
            "oracle_px": cur["oracle_px"],
            "open_interest": cur["open_interest"],
        },
        "last_settled": None if latest_t is None else {
            "time_utc": iso(latest_t),
            "funding_rate": rows[latest_t]["funding_rate"],
        },
        "windows": {
            "24h": window_stats(rows, now_ms, 24),
            "3d": window_stats(rows, now_ms, 72),
            "7d": window_stats(rows, now_ms, 168),
            "30d": window_stats(rows, now_ms, 720),
        },
        "note": "funding_rate is the hourly rate as a fraction (0.0000125 = 0.00125%/h). "
                "Positive: longs pay shorts. sum over a window = cumulative rate; "
                "USD received by a short of N coins ~= sum over hours of rate*N*mark_px.",
    }
    with open(SUMMARY_JSON, "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary["current"]), "rows:", len(rows))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
