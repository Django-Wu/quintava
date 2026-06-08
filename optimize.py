#!/usr/bin/env python3
"""
Grid-search strategy settings on Binance history.

This is the main tool for improving the bot without risking money: it searches
confidence thresholds, prediction horizons and cooldowns, then ranks settings
by win rate while enforcing a minimum number of signals.

Usage:
  python optimize.py
  python optimize.py --hours 83 --min-signals 20
"""

import argparse

import numpy as np

from backtest import backtest
from bot_core import fetch_klines, compute_rsi_series, compute_macd_series


def parse_ints(raw):
    return [int(x.strip()) for x in str(raw).split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description="Optimize BTC bot parameters")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--hours", type=int, default=83)
    parser.add_argument("--confidences", default="30,35,40,45,50,55,60")
    parser.add_argument("--horizons", default="1,3,5,10,15")
    parser.add_argument("--cooldowns", default="1,3,5,10,15")
    parser.add_argument("--min-signals", type=int, default=20)
    parser.add_argument("--top", type=int, default=12)
    args = parser.parse_args()

    limit = min(args.hours * 60, 5000)
    print(f"Fetching {limit} candles for {args.symbol}...")
    candles = fetch_klines(args.symbol, args.interval, limit)
    closes = np.array([c.close for c in candles], dtype=float)
    rsi_s = compute_rsi_series(closes)
    macd_s, sig_s, hist_s = compute_macd_series(closes)

    rows = []
    for confidence in parse_ints(args.confidences):
        for horizon in parse_ints(args.horizons):
            for cooldown in parse_ints(args.cooldowns):
                result = backtest(
                    candles,
                    horizon,
                    confidence,
                    cooldown_min=cooldown,
                    closes=closes,
                    rsi_s=rsi_s,
                    macd_s=macd_s,
                    sig_s=sig_s,
                    hist_s=hist_s,
                )
                if result["total"] < args.min_signals:
                    continue
                rows.append({
                    "confidence": confidence,
                    "horizon": horizon,
                    "cooldown": cooldown,
                    **result,
                })

    rows.sort(
        key=lambda r: (
            r["win_rate"],
            r["total"],
            r["avg_win_move_pct"] - r["avg_loss_move_pct"],
        ),
        reverse=True,
    )

    print(
        f"\nTop {min(args.top, len(rows))} settings "
        f"(min {args.min_signals} signals, {len(candles)} candles)"
    )
    print("=" * 92)
    print(
        f"{'conf':>5} {'horizon':>7} {'cooldn':>7} {'signals':>7} "
        f"{'win%':>6} {'UP':>13} {'DOWN':>13} {'avg move':>9}"
    )
    print("-" * 92)
    for row in rows[: args.top]:
        print(
            f"{row['confidence']:>5}% {row['horizon']:>6}m {row['cooldown']:>6}m "
            f"{row['total']:>7} {row['win_rate']:>5.1f}% "
            f"{row['up_win_rate']:>5.1f}%({row['up_total']:>3}) "
            f"{row['down_win_rate']:>5.1f}%({row['down_total']:>3}) "
            f"{row['avg_move_pct']:>8.4f}%"
        )

    if not rows:
        print("No settings reached the minimum signal count. Lower --min-signals.")
    else:
        best = rows[0]
        print("\nSuggested paper-trading settings:")
        print(f"  MIN_CONFIDENCE={best['confidence']}")
        print(f"  HORIZON_MIN={best['horizon']}")
        print(f"  COOLDOWN_MIN={best['cooldown']}")
        print("\nTreat this as a candidate, not proof. Validate in live paper mode next.")


if __name__ == "__main__":
    main()
