#!/usr/bin/env python3
"""Track the Hyperliquid Assistance Fund (AF) from public protocol data.

The AF is the system address 0xfefe...fe. Trading fees are credited to it
directly (no ledger entry) and it spends them buying HYPE on the HYPE/USDC
spot book (@107). The AQAv2 USDC reserve yield arrives as a ledger "send"
from 0x5000...0000 about every 30 days.

So, per period:
    HYPE bought        = sum of AF fills on @107
    USDC spent         = sum(px * sz) of those fills
    fee income to AF   = USDC spent + change in AF USDC balance - USDC ledger inflows
    reserve yield      = USDC sends from 0x5000...0000 (>= YIELD_MIN_USD)

Hypurrscan's cumulative fee series is stored only as an independent reference
(gross fees), to sanity-check the AF numbers. No personal account is used.

Files (data/):
  af_buys_hourly.csv   AF buys per UTC hour
  af_inflows.csv       ledger inflows/outflows worth recording (stablecoins, HYPE, >= $1k)
  af_snapshots.csv     AF balances each run + latest Hypurrscan cumulative fees
  hypurrscan_fees.csv  Hypurrscan cumulative fee history (reference)
  af_daily.csv         per Japan-time day (00:00-24:00 JST)
  af_summary.json      latest complete day, 7d / 30d totals, last yield payment
  af_state.json        cursors
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.hyperliquid.xyz/info"
HYPURRSCAN_FEES = "https://api.hypurrscan.io/fees"
AF = "0xfefefefefefefefefefefefefefefefefefefefe"
YIELD_SENDER = "0x5000000000000000000000000000000000000000"
YIELD_MIN_USD = 1000.0
HYPE_PAIR = "@107"                       # HYPE/USDC spot
WATCH_TOKENS = {"USDC", "USDH", "USDT0", "USDE", "HYPE"}
STABLES = {"USDC", "USDH", "USDT0", "USDE"}
HS_SCALE = 1e6                           # Hypurrscan fee units -> USD

HOUR_MS = 3600 * 1000
DAY_MS = 24 * HOUR_MS
JST = timezone(timedelta(hours=9))
FIRST_DAY_JST = "2026-10-30"             # first full JST day with fill history available
LEDGER_START_MS = int(datetime(2026, 8, 26, tzinfo=timezone.utc).timestamp() * 1000)  # AQAv2 accrual start
SNAP_TOLERANCE_MS = 90 * 60 * 1000       # balance snapshot must be within 90 min of a day boundary

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BUYS_CSV = os.path.join(DATA_DIR, "af_buys_hourly.csv")
INFLOWS_CSV = os.path.join(DATA_DIR, "af_inflows.csv")
SNAP_CSV = os.path.join(DATA_DIR, "af_snapshots.csv")
HS_CSV = os.path.join(DATA_DIR, "hypurrscan_fees.csv")
DAILY_CSV = os.path.join(DATA_DIR, "af_daily.csv")
SUMMARY_JSON = os.path.join(DATA_DIR, "af_summary.json")
STATE_JSON = os.path.join(DATA_DIR, "af_state.json")


# ---------------------------------------------------------------- helpers
def post(payload, retries=3):
    body = json.dumps(payload).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"API request failed: {payload.get('type')}: {last}")


def get_json(url, retries=2):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hype-funding-collector"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url}: {last}")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def jst_day_start_ms(day_str):
    d = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=JST)
    return int(d.timestamp() * 1000)


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, header, rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)


def fnum(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_state():
    if os.path.exists(STATE_JSON):
        with open(STATE_JSON) as f:
            return json.load(f)
    return {}


# ---------------------------------------------------------------- fills -> hourly buys
def fetch_fills(start_ms, end_ms):
    """All AF fills in [start_ms, end_ms], paging forward (2000 per page)."""
    out, cursor = {}, start_ms
    for _ in range(60):
        page = post({"type": "userFillsByTime", "user": AF, "startTime": cursor, "endTime": end_ms})
        if not page:
            break
        for f in page:
            out.setdefault(f["tid"], f)
        mx = max(int(f["time"]) for f in page)
        if len(page) < 2000:
            break
        cursor = mx if mx > cursor else cursor + 1
    return list(out.values())


def update_buys(state, now_ms):
    existing = {int(r["hour_ms"]): r for r in read_csv(BUYS_CSV)}
    first_ms = jst_day_start_ms(FIRST_DAY_JST)
    covered = state.get("fills_covered_until_ms")
    # re-fetch from the start of the last (possibly partial) hour
    start = first_ms if covered is None else max(first_ms, covered // HOUR_MS * HOUR_MS)
    fills = fetch_fills(start, now_ms)

    unexpected = []
    fresh = {}
    for f in fills:
        t = int(f["time"])
        if t < start:
            continue
        if f["coin"] != HYPE_PAIR or f["side"] != "B":
            unexpected.append({"time": iso(t), "coin": f["coin"], "side": f["side"], "sz": f["sz"], "px": f["px"]})
            continue
        h = t // HOUR_MS * HOUR_MS
        b = fresh.setdefault(h, {"n": 0, "hype": 0.0, "usdc": 0.0})
        b["n"] += 1
        b["hype"] += float(f["sz"])
        b["usdc"] += float(f["sz"]) * float(f["px"])

    for h in [h for h in existing if h >= start]:
        del existing[h]
    rows = []
    for h, r in existing.items():
        rows.append([h, r["hour_utc"], int(r["n_fills"]), float(r["hype"]), float(r["usdc"])])
    for h, b in fresh.items():
        rows.append([h, iso(h), b["n"], b["hype"], b["usdc"]])
    rows.sort(key=lambda r: r[0])
    write_csv(BUYS_CSV, ["hour_ms", "hour_utc", "n_fills", "hype", "usdc"],
              [[r[0], r[1], r[2], f"{r[3]:.6f}", f"{r[4]:.6f}"] for r in rows])
    state["fills_covered_until_ms"] = now_ms
    return {"fetched_fills": len(fills), "unexpected_fills": unexpected[:20], "n_unexpected": len(unexpected)}


# ---------------------------------------------------------------- ledger -> inflows
def categorize(token, amount, sender, direction):
    if direction == "out":
        return "outflow"
    if token == "USDC" and sender == YIELD_SENDER and amount >= YIELD_MIN_USD:
        return "aqa_yield"
    if token == "USDC":
        return "usdc_other"
    if token in STABLES:
        return "stable_other"
    if token == "HYPE":
        return "hype"
    return "other"


def update_inflows(state, now_ms):
    rows = read_csv(INFLOWS_CSV)
    seen = {(r["time_ms"], r["hash"], r["token"], r["amount"]) for r in rows}
    cursor = state.get("ledger_cursor_ms", LEDGER_START_MS)
    new = 0
    for _ in range(60):
        page = post({"type": "userNonFundingLedgerUpdates", "user": AF, "startTime": cursor, "endTime": now_ms})
        if not page:
            break
        for e in page:
            d = e.get("delta", {})
            token = d.get("token") or ("USDC" if "usdc" in d else None)
            amount = fnum(d.get("amount"), fnum(d.get("usdc"), 0.0))
            usdv = fnum(d.get("usdcValue"), amount if token in STABLES else 0.0)
            sender = (d.get("user") or "").lower()
            dest = (d.get("destination") or "").lower()
            direction = "out" if sender == AF else "in"
            if dest and dest != AF and sender != AF:
                continue
            if token not in WATCH_TOKENS and (usdv or 0) < YIELD_MIN_USD and direction == "in":
                continue
            key = (str(e["time"]), e.get("hash", ""), token or "", f"{amount}")
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "time_ms": str(e["time"]), "time_utc": iso(int(e["time"])), "type": d.get("type", ""),
                "direction": direction, "token": token or "", "amount": f"{amount}",
                "usdc_value": f"{usdv or 0}", "counterparty": dest if direction == "out" else sender,
                "category": categorize(token, amount or 0.0, sender, direction), "hash": e.get("hash", ""),
            })
            new += 1
        mx = max(int(e["time"]) for e in page)
        if len(page) < 2000:
            cursor = max(cursor, mx)
            break
        cursor = mx if mx > cursor else cursor + 1
    rows.sort(key=lambda r: int(r["time_ms"]))
    header = ["time_ms", "time_utc", "type", "direction", "token", "amount", "usdc_value", "counterparty", "category", "hash"]
    write_csv(INFLOWS_CSV, header, [[r[k] for k in header] for r in rows])
    state["ledger_cursor_ms"] = cursor
    return {"new_inflow_rows": new}


# ---------------------------------------------------------------- snapshots + reference fees
def update_snapshots(now_ms):
    s = post({"type": "spotClearinghouseState", "user": AF})
    bal = {b["coin"]: float(b["total"]) for b in s.get("balances", [])}

    hs_time = hs_total = hs_spot = None
    hs_err = None
    try:
        hist = get_json(HYPURRSCAN_FEES)
        pts = {}
        for r in read_csv(HS_CSV):
            pts[int(r["time_ms"])] = (float(r["total_fees_usd"]), float(r["spot_fees_usd"]))
        for p in hist:
            pts[int(p["time"]) * 1000] = (p["total_fees"] / HS_SCALE, p["total_spot_fees"] / HS_SCALE)
        keep_from = jst_day_start_ms("2026-09-01")
        write_csv(HS_CSV, ["time_ms", "time_utc", "total_fees_usd", "spot_fees_usd"],
                  [[t, iso(t), f"{v[0]:.2f}", f"{v[1]:.2f}"] for t, v in sorted(pts.items()) if t >= keep_from])
        last = max(hist, key=lambda p: p["time"])
        hs_time, hs_total, hs_spot = int(last["time"]) * 1000, last["total_fees"] / HS_SCALE, last["total_spot_fees"] / HS_SCALE
    except Exception as e:  # noqa: BLE001
        hs_err = str(e)

    header = ["time_ms", "time_utc", "af_usdc", "af_hype", "af_usdh", "af_usdt0", "af_usde",
              "hs_time_ms", "hs_total_fees_usd", "hs_spot_fees_usd"]
    new = not os.path.exists(SNAP_CSV)
    with open(SNAP_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow([now_ms, iso(now_ms), bal.get("USDC", 0.0), bal.get("HYPE", 0.0), bal.get("USDH", 0.0),
                    bal.get("USDT0", 0.0), bal.get("USDE", 0.0),
                    "" if hs_time is None else hs_time,
                    "" if hs_total is None else f"{hs_total:.2f}",
                    "" if hs_spot is None else f"{hs_spot:.2f}"])
    return {"af_usdc": bal.get("USDC"), "af_hype": bal.get("HYPE"), "hypurrscan_error": hs_err}


# ---------------------------------------------------------------- daily build
def nearest_snapshot(snaps, t):
    best = None
    for s in snaps:
        d = abs(s["t"] - t)
        if d <= SNAP_TOLERANCE_MS and (best is None or d < abs(best["t"] - t)):
            best = s
    return best


def interp(points, t, max_gap_ms=36 * HOUR_MS):
    """Linear interpolation of a cumulative series at t. points: sorted [(t, v)]."""
    lo = hi = None
    for p in points:
        if p[0] <= t:
            lo = p
        if p[0] >= t:
            hi = p
            break
    if lo is None or hi is None:
        return None, None
    if hi[0] == lo[0]:
        return lo[1], 0
    if hi[0] - lo[0] > max_gap_ms:
        return None, None
    v = lo[1] + (hi[1] - lo[1]) * (t - lo[0]) / (hi[0] - lo[0])
    return v, hi[0] - lo[0]


def build_daily(state, now_ms):
    buys = [(int(r["hour_ms"]), int(r["n_fills"]), float(r["hype"]), float(r["usdc"])) for r in read_csv(BUYS_CSV)]
    inflows = read_csv(INFLOWS_CSV)
    snaps = [{"t": int(r["time_ms"]), "usdc": float(r["af_usdc"]), "hype": float(r["af_hype"])} for r in read_csv(SNAP_CSV)]
    hs_pts = {int(r["time_ms"]): float(r["total_fees_usd"]) for r in read_csv(HS_CSV)}
    for r in read_csv(SNAP_CSV):
        if r.get("hs_time_ms") and r.get("hs_total_fees_usd"):
            hs_pts[int(r["hs_time_ms"])] = float(r["hs_total_fees_usd"])
    hs_points = sorted(hs_pts.items())
    covered = state.get("fills_covered_until_ms", 0)

    today_jst = datetime.fromtimestamp(now_ms / 1000, tz=JST).strftime("%Y-%m-%d")
    day = datetime.strptime(FIRST_DAY_JST, "%Y-%m-%d")
    out = []
    while day.strftime("%Y-%m-%d") <= today_jst:
        ds = day.strftime("%Y-%m-%d")
        start = jst_day_start_ms(ds)
        end = start + DAY_MS
        n = sum(b[1] for b in buys if start <= b[0] < end)
        hype = sum(b[2] for b in buys if start <= b[0] < end)
        usdc = sum(b[3] for b in buys if start <= b[0] < end)
        inr = [r for r in inflows if start <= int(r["time_ms"]) < end]
        yld = sum(float(r["amount"]) for r in inr if r["category"] == "aqa_yield")
        usdc_other = sum(float(r["amount"]) for r in inr if r["category"] == "usdc_other")
        usdc_out = sum(float(r["amount"]) for r in inr if r["category"] == "outflow" and r["token"] == "USDC")
        s0, s1 = nearest_snapshot(snaps, start), nearest_snapshot(snaps, end)
        if s0 and s1:
            dbal = s1["usdc"] - s0["usdc"]
            basis = "snapshot"
        else:
            dbal = 0.0
            basis = "no_snapshot"  # AF USDC balance assumed unchanged (it is normally only a few $k)
        fee_income = usdc + dbal - yld - usdc_other + usdc_out
        h0, g0 = interp(hs_points, start)
        h1, g1 = interp(hs_points, end)
        hs_fees = (h1 - h0) if (h0 is not None and h1 is not None) else None
        hs_basis = "" if hs_fees is None else ("hourly" if max(g0, g1) <= 3 * HOUR_MS else "daily_interp")
        complete = now_ms >= end and covered >= end
        out.append({
            "date_jst": ds, "complete": complete, "n_fills": n,
            "buy_hype": hype, "buy_usdc": usdc, "avg_px": (usdc / hype) if hype else None,
            "aqa_yield_usdc": yld, "usdc_other_in": usdc_other, "usdc_bal_change": dbal, "bal_basis": basis,
            "fee_income_usdc": fee_income,
            "hs_fees_usd": hs_fees, "hs_basis": hs_basis,
            "fee_to_hs_ratio": (fee_income / hs_fees) if hs_fees else None,
        })
        day += timedelta(days=1)

    header = ["date_jst", "complete", "n_fills", "buy_hype", "buy_usdc", "avg_px", "aqa_yield_usdc",
              "usdc_other_in", "usdc_bal_change", "bal_basis", "fee_income_usdc", "hs_fees_usd", "hs_basis",
              "fee_to_hs_ratio"]

    def fmt(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float):
            return f"{v:.4f}"
        return v
    write_csv(DAILY_CSV, header, [[fmt(r[k]) for k in header] for r in out])
    return out, inflows, snaps


def window(days, k):
    ds = [d for d in days if d["complete"]][-k:]
    if not ds:
        return None
    hs_vals = [d["hs_fees_usd"] for d in ds if d["hs_fees_usd"] is not None]
    w = {
        "days": len(ds), "from": ds[0]["date_jst"], "to": ds[-1]["date_jst"],
        "buy_hype": sum(d["buy_hype"] for d in ds), "buy_usdc": sum(d["buy_usdc"] for d in ds),
        "fee_income_usdc": sum(d["fee_income_usdc"] for d in ds),
        "aqa_yield_usdc": sum(d["aqa_yield_usdc"] for d in ds),
        "hs_fees_usd": sum(hs_vals) if len(hs_vals) == len(ds) else None,
    }
    w["avg_px"] = w["buy_usdc"] / w["buy_hype"] if w["buy_hype"] else None
    w["fee_income_annualized"] = w["fee_income_usdc"] / len(ds) * 365
    return w


def build_summary(days, inflows, snaps, now_ms, notes):
    complete = [d for d in days if d["complete"]]
    latest = complete[-1] if complete else None
    yields = [r for r in inflows if r["category"] == "aqa_yield"]
    last_yield = None
    if yields:
        y = yields[-1]
        t = int(y["time_ms"])
        last_yield = {"time_utc": y["time_utc"],
                      "date_jst": datetime.fromtimestamp(t / 1000, tz=JST).strftime("%Y-%m-%d"),
                      "amount_usdc": float(y["amount"]),
                      "next_estimate_jst": datetime.fromtimestamp((t + 30 * DAY_MS) / 1000, tz=JST).strftime("%Y-%m-%d"),
                      "annualized_usdc": float(y["amount"]) * 365 / 30}
    w7, w30 = window(days, 7), window(days, 30)
    warnings = []
    if notes.get("n_unexpected"):
        warnings.append(f"AF had {notes['n_unexpected']} fills other than HYPE buys on {HYPE_PAIR}")
    if latest and latest["fee_income_usdc"] < 0:
        warnings.append(f"negative fee income on {latest['date_jst']} (yield probably not fully spent within the day)")
    if latest and latest["fee_to_hs_ratio"] is not None and not (0.7 <= latest["fee_to_hs_ratio"] <= 1.15):
        warnings.append(f"AF fee income / Hypurrscan fees = {latest['fee_to_hs_ratio']:.2f} on {latest['date_jst']} (usual ~0.9)")
    summary = {
        "generated_utc": iso(now_ms),
        "af_address": AF,
        "latest_complete_day": latest,
        "today_partial": days[-1] if days and not days[-1]["complete"] else None,
        "last_7d": w7,
        "last_30d": w30,
        "run_rate_annualized_usdc": None if not w7 else {
            "fees_from_7d": w7["fee_income_annualized"],
            "yield_from_last_payment": last_yield["annualized_usdc"] if last_yield else None,
            "total": w7["fee_income_annualized"] + (last_yield["annualized_usdc"] if last_yield else 0.0),
        },
        "last_yield": last_yield,
        "yield_history": [{"time_utc": y["time_utc"], "amount_usdc": float(y["amount"])} for y in yields],
        "af_balance_now": None if not snaps else {"time_utc": iso(snaps[-1]["t"]), "hype": snaps[-1]["hype"], "usdc": snaps[-1]["usdc"]},
        "warnings": warnings,
        "notes": {
            "day": "Japan time 00:00-24:00 (UTC 15:00-15:00)",
            "fee_income_usdc": "USDC the AF received from trading fees = USDC spent on HYPE buys + change in AF USDC balance - USDC ledger inflows (yield etc.). This is the protocol's ~99% share; ~1% goes to HLP.",
            "aqa_yield_usdc": "AQAv2 USDC reserve yield, ledger send from 0x5000...0000",
            "hs_fees_usd": "Reference only: Hypurrscan cumulative total fees (gross, third party). AF fee income is usually ~0.9x of it.",
            "bal_basis": "no_snapshot = AF USDC balance change assumed 0 (days before balance snapshots started)",
        },
        "collector": notes,
    }
    with open(SUMMARY_JSON, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary


def main():
    now_ms = int(time.time() * 1000)
    state = load_state()
    notes = {}
    notes.update(update_buys(state, now_ms))
    notes.update(update_inflows(state, now_ms))
    notes.update(update_snapshots(now_ms))
    with open(STATE_JSON, "w") as f:
        json.dump(state, f, indent=2)
    days, inflows, snaps = build_daily(state, now_ms)
    s = build_summary(days, inflows, snaps, now_ms, notes)
    print(json.dumps({"latest": s["latest_complete_day"], "warnings": s["warnings"]}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
