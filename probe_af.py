#!/usr/bin/env python3
"""One-off probe: inspect the Assistance Fund (AF) system address data shapes.

Public protocol address only. Writes results to probe/ for inspection.
"""
import json
import os
import time
import urllib.request
from collections import Counter, defaultdict
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


def save(name, obj):
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


now = int(time.time() * 1000)
report = {"generated_utc": iso(now)}

for label, payload in [
    ("spot_state", {"type": "spotClearinghouseState", "user": AF}),
    ("perp_state", {"type": "clearinghouseState", "user": AF}),
]:
    try:
        save(f"{label}.json", post(payload))
        report[label] = "ok"
    except Exception as e:  # noqa: BLE001
        report[label] = f"ERROR {e}"

# spot meta: token / pair names
try:
    meta = post({"type": "spotMeta"})
    tokens = {t["index"]: t["name"] for t in meta["tokens"]}
    pairs = {}
    for u in meta["universe"]:
        pairs[u["name"]] = [tokens.get(i) for i in u["tokens"]]
    save("spot_pairs_subset.json", {k: v for k, v in pairs.items() if "HYPE" in (v or []) or k in ("@107",)})
    report["spotMeta"] = "ok"
except Exception as e:  # noqa: BLE001
    pairs = {}
    report["spotMeta"] = f"ERROR {e}"


def summarize_fills(fills):
    s = {"n": len(fills)}
    if not fills:
        return s
    ts = [f["time"] for f in fills]
    s["first"], s["last"] = iso(min(ts)), iso(max(ts))
    by = defaultdict(lambda: {"n": 0, "sz": 0.0, "ntl": 0.0, "fee": 0.0, "feeTokens": Counter(), "dirs": Counter()})
    for f in fills:
        k = (f["coin"], f["side"])
        b = by[k]
        b["n"] += 1
        b["sz"] += float(f["sz"])
        b["ntl"] += float(f["sz"]) * float(f["px"])
        b["fee"] += float(f.get("fee", 0) or 0)
        b["feeTokens"][f.get("feeToken")] += 1
        b["dirs"][f.get("dir")] += 1
    s["by_coin_side"] = {f"{c}|{sd}|{pairs.get(c)}": {**v, "feeTokens": dict(v["feeTokens"]), "dirs": dict(v["dirs"])}
                         for (c, sd), v in by.items()}
    # per UTC day
    days = defaultdict(lambda: {"n": 0, "sz": 0.0, "ntl": 0.0})
    for f in fills:
        d = iso(f["time"])[:10]
        days[d]["n"] += 1
        days[d]["sz"] += float(f["sz"])
        days[d]["ntl"] += float(f["sz"]) * float(f["px"])
    s["by_day"] = dict(sorted(days.items()))
    s["sample"] = fills[:5]
    return s


try:
    fills = post({"type": "userFills", "user": AF})
    report["userFills"] = summarize_fills(fills)
except Exception as e:  # noqa: BLE001
    report["userFills"] = f"ERROR {e}"

# How far back can userFillsByTime go? page forward from Oct 1
try:
    start = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
    allf, pages, cursor = [], 0, start
    while pages < 8:
        page = post({"type": "userFillsByTime", "user": AF, "startTime": cursor, "endTime": now,
                     "aggregateByTime": True})
        pages += 1
        if not page:
            break
        allf.extend(page)
        mx = max(f["time"] for f in page)
        if len(page) < 2000 or mx <= cursor:
            break
        cursor = mx + 1
    # dedupe
    seen, uniq = set(), []
    for f in allf:
        k = (f.get("tid"), f.get("time"), f.get("oid"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(f)
    report["userFillsByTime_from_Oct1"] = {"pages": pages, **summarize_fills(uniq)}
except Exception as e:  # noqa: BLE001
    report["userFillsByTime_from_Oct1"] = f"ERROR {e}"

# Ledger updates (deposits, transfers, etc.)
try:
    start = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
    led = post({"type": "userNonFundingLedgerUpdates", "user": AF, "startTime": start, "endTime": now})
    types = Counter(x["delta"].get("type") for x in led)
    big = []
    for x in led:
        d = x["delta"]
        big.append({"time": iso(x["time"]), **{k: v for k, v in d.items()}})
    report["ledger"] = {"n": len(led), "types": dict(types)}
    save("ledger.json", big[-400:])
except Exception as e:  # noqa: BLE001
    report["ledger"] = f"ERROR {e}"

# DefiLlama for cross-check
llama = {}
for slug in ["hyperliquid", "hyperliquid-perps", "hyperliquid-spot-orderbook"]:
    for dt in ["dailyFees", "dailyRevenue", "dailyHoldersRevenue"]:
        key = f"{slug}:{dt}"
        try:
            j = get(f"https://api.llama.fi/summary/fees/{slug}?dataType={dt}")
            chart = j.get("totalDataChart") or []
            llama[key] = [[iso(int(t) * 1000)[:10], v] for t, v in chart[-45:]]
            llama[key + "_meta"] = {k: j.get(k) for k in ("name", "total24h", "total48hto24h", "total7d", "total30d", "methodology")}
        except Exception as e:  # noqa: BLE001
            llama[key] = f"ERROR {e}"
save("defillama.json", llama)

save("report.json", report)
print(json.dumps({k: (v if isinstance(v, str) else "…") for k, v in report.items()}, ensure_ascii=False))
