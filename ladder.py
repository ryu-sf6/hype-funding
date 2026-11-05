#!/usr/bin/env python3
"""Price-level alert for HYPE.

Runs after collect.py on GitHub Actions. When the mark price crosses one of the
$10 levels (20, 30, ..., 100), opens a GitHub issue so the repository owner gets
a push/email notification. The issue also shows the target net exposure from a
simple linear ladder:  target = (90 - price) * 100  (floored at 0).

Only market price and the ladder formula are used here. No account data.
"""
import json
import os
import subprocess
import sys

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SUMMARY_JSON = os.path.join(DATA_DIR, "summary.json")
STATE_JSON = os.path.join(DATA_DIR, "alert_state.json")

LEVELS = [20, 30, 40, 50, 60, 70, 80, 90, 100]
LADDER_TOP = 90       # net target reaches 0 at this price
LADDER_SLOPE = 100    # HYPE per $1


def ladder_target(price):
    return max(0, round((LADDER_TOP - price) * LADDER_SLOPE))


def bucket(price):
    """Index of the highest level at or below price (-1 if below all levels)."""
    b = -1
    for i, lv in enumerate(LEVELS):
        if price >= lv:
            b = i
    return b


def main():
    with open(SUMMARY_JSON) as f:
        s = json.load(f)
    price = float(s["current"]["mark_px"])
    now = s["generated_utc"]

    state = {"bucket": None, "price": None}
    if os.path.exists(STATE_JSON):
        with open(STATE_JSON) as f:
            state = json.load(f)

    cur_b = bucket(price)
    prev_b = state.get("bucket")
    prev_price = state.get("price")

    crossed = []
    if prev_b is not None and cur_b != prev_b:
        lo, hi = sorted([prev_b, cur_b])
        # levels strictly between the two buckets' positions, inclusive of the new one
        if cur_b > prev_b:
            crossed = [LEVELS[i] for i in range(prev_b + 1, cur_b + 1)]
            direction = "上抜け"
        else:
            crossed = [LEVELS[i] for i in range(cur_b + 1, prev_b + 1)]
            direction = "下抜け"

    if crossed:
        target = ladder_target(price)
        levels_txt = " / ".join(f"${lv}" for lv in crossed)
        title = f"HYPE {direction} {levels_txt}: ${price:.2f}"
        body = (
            f"時刻: {now}\n"
            f"マーク価格: ${price:.2f}（前回チェック: ${prev_price:.2f}）\n"
            f"またいだ水準: {levels_txt}（{direction}）\n\n"
            f"ラダー目標（(90 − 価格) × 100）: ネット {target:,} HYPE\n"
            f"※ 価格 ${price:.0f} 付近での目標。実際の保有枚数との差は朝の日報で確認。\n\n"
            f"FR（現在）: {s['current']['funding_hourly']*100:+.5f}%/h"
            f"（年率 {s['current']['funding_annualized']*100:+.1f}%）"
        )
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPOSITORY")
        if token and repo:
            subprocess.run(
                ["gh", "issue", "create", "-R", repo, "-t", title, "-b", body, "-l", "price-alert"],
                check=False,
            )
        print("ALERT:", title)
    else:
        print(f"no level crossed (price ${price:.2f}, bucket {cur_b}, prev {prev_b})")

    with open(STATE_JSON, "w") as f:
        json.dump({"bucket": cur_b, "price": price, "updated_utc": now,
                   "ladder_target": ladder_target(price)}, f, indent=2)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
