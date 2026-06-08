#!/usr/bin/env python3
"""
Optimize the fixed 5-minute Polymarket-style BTC Up/Down strategy.

This is different from the generic rolling-horizon backtest. It reconstructs
real clock-aligned 5m windows and asks: if the bot entered after N minutes, using
only information available at that time, how often would it match the final
UP/DOWN outcome shown in Polymarket's "Past" list?
"""

import argparse

from bot_core import fetch_klines, reconstruct_windows


def floats(raw):
    return [float(x.strip()) for x in str(raw).split(",") if x.strip()]


def ints(raw):
    return [int(x.strip()) for x in str(raw).split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description="Optimize fixed 5m Up/Down strategy")
    parser.add_argument("--hours", type=int, default=83)
    parser.add_argument("--min-signals", type=int, default=30)
    parser.add_argument("--strategies", default="follow_lead,reverse_lead,confirm_momentum,fade_momentum")
    parser.add_argument("--entries", default="1,2,3,4")
    parser.add_argument("--move-thresholds", default="0,0.01,0.02,0.03,0.05,0.08,0.10")
    parser.add_argument("--last-thresholds", default="0,0.01,0.02,0.03,0.05")
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    candles = fetch_klines("BTCUSDT", "1m", min(args.hours * 60, 5000))
    rows = []

    for strategy in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        for entry in ints(args.entries):
            for move_thr in floats(args.move_thresholds):
                for last_thr in floats(args.last_thresholds):
                    _, stats = reconstruct_windows(
                        candles,
                        window_min=5,
                        limit=40,
                        strategy=strategy,
                        entry_min=entry,
                        min_move_pct=move_thr,
                        min_last_candle_pct=last_thr,
                    )
                    if stats["predicted"] < args.min_signals:
                        continue
                    rows.append({
                        "strategy": strategy,
                        "entry": entry,
                        "move_thr": move_thr,
                        "last_thr": last_thr,
                        **stats,
                    })

    rows.sort(
        key=lambda r: (r["win_rate"], r["predicted"], -r["skipped"]),
        reverse=True,
    )

    print(f"5m optimizer over {len(candles)} 1m candles")
    print("=" * 112)
    print(
        f"{'strategy':<18} {'entry':>5} {'move>=':>8} {'last>=':>8} "
        f"{'signals':>7} {'win%':>6} {'UP':>13} {'DOWN':>13} {'actual UP%':>10}"
    )
    print("-" * 112)
    for r in rows[: args.top]:
        print(
            f"{r['strategy']:<18} {r['entry']:>4}m {r['move_thr']:>7.3f} "
            f"{r['last_thr']:>7.3f} {r['predicted']:>7} {r['win_rate']:>5.1f}% "
            f"{r['up_pred_win_rate']:>5.1f}%({r['up_pred']:>3}) "
            f"{r['down_pred_win_rate']:>5.1f}%({r['down_pred']:>3}) "
            f"{r['up_actual_rate']:>9.1f}%"
        )

    if rows:
        b = rows[0]
        print("\nSuggested 5m paper strategy:")
        print(f"  M5_STRATEGY={b['strategy']}")
        print(f"  M5_ENTRY_MIN={b['entry']}")
        print(f"  M5_MIN_MOVE_PCT={b['move_thr']}")
        print(f"  M5_MIN_LAST_CANDLE_PCT={b['last_thr']}")
        print("\nThis is a research candidate. Validate live in paper mode.")
    else:
        print("No settings met --min-signals. Lower the minimum or thresholds.")


if __name__ == "__main__":
    main()
