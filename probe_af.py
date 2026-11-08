#!/usr/bin/env python3
"""One-off probe #2: AF fill continuity (HYPE received outside fills), open orders,
and an independent fee series (Hypurrscan) for cross-checking.

Public protocol address only. Writes results to probe/ for inspection.
"""
import csv
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

API = "https://api.hyperliquid.xyz/info"
AF = "0xfefefefefefefefefefefefefefefefefefefefe"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe")
os.makedirs(OUT, exist_ok=True)


def post(payload):
    req = urllib.request.Request(API, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "hype-funding-probe"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


now = int(time.time() * 1000)
report = {"generated_utc": iso(now)}

# full fills (non-aggregated) since Oct 25, paged
start = int(datetime(2026, 10, 25, tzinfo=timezone.utc).timestamp() * 1000)
allf, cursor, pages = [], start, 0
while pages < 12:
    page = post({"type": "userFillsByTime", "user": AF, "startTime": cursor, "endTime": now})
    pages += 1
    if not page:
        break
    allf.extend(page)
    mx = max(f["time"] for f in page)
    if len(page) < 2000 or mx <= cursor:
        break
    cursor = mx  # same-ms fills: keep cursor at mx, dedupe below
seen, fills = set(), []
for f in allf:
    if f["tid"] in seen:
        continue
    seen.add(f["tid"])
    fills.append(f)
fills.sort(key=lambda f: (f["time"], float(f["startPosition"])))
report["fills"] = {"pages": pages, "n": len(fills),
                   "first": iso(fills[0]["time"]) if fills else None,
                   "last": iso(fills[-1]["time"]) if fills else None,
                   "coins": sorted({f["coin"] for f in fills}),
                   "sides": sorted({f["side"] for f in fills})}
with open(os.path.join(OUT, "fills.csv"), "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["time_ms", "time_utc", "coin", "side", "px", "sz", "startPosition", "crossed", "fee", "feeToken", "tid"])
    for f in fills:
        w.writerow([f["time"], iso(f["time"]), f["coin"], f["side"], f["px"], f["sz"], f["startPosition"],
                    f["crossed"], f["fee"], f["feeToken"], f["tid"]])

# HYPE received outside of fills: gaps in startPosition between consecutive HYPE buys
gaps, gap_by_day = [], {}
hype = [f for f in fills if f["coin"] == "@107"]
for a, b in zip(hype, hype[1:]):
    exp = float(a["startPosition"]) + float(a["sz"])
    g = float(b["startPosition"]) - exp
    if abs(g) > 1e-6:
        gaps.append({"from": iso(a["time"]), "to": iso(b["time"]), "gap": g})
        d = iso(b["time"])[:10]
        gap_by_day[d] = gap_by_day.get(d, 0.0) + g
report["hype_gaps"] = {"n": len(gaps), "sum": sum(x["gap"] for x in gaps),
                       "by_day": dict(sorted(gap_by_day.items())),
                       "largest": sorted(gaps, key=lambda x: -abs(x["gap"]))[:15]}

# open orders / current balances
try:
    oo = post({"type": "openOrders", "user": AF})
    report["open_orders"] = {"n": len(oo), "sample": oo[:5],
                             "usdc_locked_est": sum(float(o["sz"]) * float(o["limitPx"]) for o in oo if o.get("side") == "B")}
except Exception as e:  # noqa: BLE001
    report["open_orders"] = f"ERROR {e}"
try:
    s = post({"type": "spotClearinghouseState", "user": AF})
    report["spot_now"] = {b["coin"]: b for b in s["balances"] if b["coin"] in ("USDC", "HYPE", "USDH", "USDT0", "USDE")}
except Exception as e:  # noqa: BLE001
    report["spot_now"] = f"ERROR {e}"

# Independent fee series (Hypurrscan)
for name, url in [("hypurrscan_fees", "https://api.hypurrscan.io/fees")]:
    try:
        j = get(url)
        if isinstance(j, list):
            report[name] = {"n": len(j), "head": j[:2], "tail": j[-30:]}
        else:
            report[name] = {"type": type(j).__name__, "keys": list(j)[:20] if isinstance(j, dict) else None,
                            "raw": json.dumps(j)[:2000]}
    except Exception as e:  # noqa: BLE001
        report[name] = f"ERROR {e}"

with open(os.path.join(OUT, "report2.json"), "w") as fh:
    json.dump(report, fh, indent=1, ensure_ascii=False)
print("done", report["fills"])
