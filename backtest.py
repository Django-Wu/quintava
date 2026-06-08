#!/usr/bin/env python3
"""
Backtest the strategy on historical Binance data to measure directional
accuracy WITHOUT risking money. Answers: "does the bot guess UP/DOWN right?"

Usage:
  python backtest.py                       # last 48h, 1m candles, default
  python backtest.py --hours 168           # last 7 days
  python backtest.py --confidence 60       # only signals >= 60% confidence
  python backtest.py --horizons 1,3,5,15   # check several prediction horizons
  python backtest.py --cooldown 5          # avoid overlapping predictions
"""

import argparse

import numpy as np

from bot_core import fetch_klines, decide, compute_rsi_series, compute_macd_series


def backtest(candles, horizon_min, min_confidence, warmup=40, cooldown_min=None,
             rsi_s=None, macd_s=None, sig_s=None, hist_s=None, closes=None):
    """
    Walk forward through candles using precomputed indicator series (O(n)).
    At each step decide a signal from past-only data, then check the close
    `horizon_min` candles later. Returns dict with accuracy stats.
    """
    if closes is None:
        closes = np.array([c.close for c in candles], dtype=float)
    n = len(closes)
    total = wins = up_tot = up_win = down_tot = down_win = 0
    next_allowed_i = warmup
    cooldown = horizon_min if cooldown_min is None else max(0, int(cooldown_min))
    abs_moves = []
    win_moves = []
    loss_moves = []
    conf_buckets = {}  # confidence bucket -> [wins, total]

    for i in range(warmup, n - horizon_min):
        if i < next_allowed_i:
            continue
        rsi = rsi_s[i] if rsi_s is not None else None
        macd_line = macd_s[i] if macd_s is not None else None
        macd_sig = sig_s[i] if sig_s is not None else None
        hist = hist_s[i] if hist_s is not None else None
        slope = closes[i] - closes[i - 3] if i >= 3 else None
        res = decide(rsi, macd_line, macd_sig, hist, slope)
        if res.signal is None or res.confidence < min_confidence:
            continue

        entry = closes[i]
        exit_price = closes[i + horizon_min]
        move_pct = (exit_price - entry) / entry * 100
        correct = (
            (res.signal == "UP" and exit_price > entry) or
            (res.signal == "DOWN" and exit_price < entry)
        )

        total += 1
        wins += int(correct)
        next_allowed_i = i + cooldown
        abs_moves.append(abs(move_pct))
        if correct:
            win_moves.append(abs(move_pct))
        else:
            loss_moves.append(abs(move_pct))
        if res.signal == "UP":
            up_tot += 1
            up_win += int(correct)
        else:
            down_tot += 1
            down_win += int(correct)

        bucket = (res.confidence // 10) * 10
        b = conf_buckets.setdefault(bucket, [0, 0])
        b[0] += int(correct)
        b[1] += 1

    return {
        "horizon_min": horizon_min,
        "total": total,
        "wins": wins,
        "win_rate": round(wins / total * 100, 1) if total else 0.0,
        "up_total": up_tot,
        "up_win_rate": round(up_win / up_tot * 100, 1) if up_tot else 0.0,
        "down_total": down_tot,
        "down_win_rate": round(down_win / down_tot * 100, 1) if down_tot else 0.0,
        "avg_move_pct": round(float(np.mean(abs_moves)), 4) if abs_moves else 0.0,
        "avg_win_move_pct": round(float(np.mean(win_moves)), 4) if win_moves else 0.0,
        "avg_loss_move_pct": round(float(np.mean(loss_moves)), 4) if loss_moves else 0.0,
        "by_confidence": {
            k: {"count": v[1], "win_rate": round(v[0] / v[1] * 100, 1)}
            for k, v in sorted(conf_buckets.items())
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest BTC direction strategy")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--hours", type=int, default=48, help="history length in hours")
    parser.add_argument("--confidence", type=int, default=60, help="min confidence")
    parser.add_argument("--horizons", default="1,3,5,15", help="comma-separated minutes")
    parser.add_argument(
        "--cooldown",
        type=int,
        default=None,
        help="minutes to wait between predictions (defaults to each horizon)",
    )
    args = parser.parse_args()

    limit = min(args.hours * 60, 5000)
    print(f"📥 Fetching {limit} {args.interval} candles for {args.symbol}...")
    candles = fetch_klines(args.symbol, args.interval, limit)
    if len(candles) < 60:
        print("❌ Not enough data")
        return

    span_h = (candles[-1].open_time - candles[0].open_time) / 1000 / 3600
    print(f"✅ {len(candles)} candles (~{span_h:.1f}h), "
          f"${candles[0].close:,.0f} → ${candles[-1].close:,.0f}")
    print(f"⚙️  Min confidence: {args.confidence}%")
    print(f"🧊 Cooldown: {args.cooldown if args.cooldown is not None else 'same as horizon'} min")
    print("=" * 64)

    closes = np.array([c.close for c in candles], dtype=float)
    rsi_s = compute_rsi_series(closes)
    macd_s, sig_s, hist_s = compute_macd_series(closes)

    horizons = [int(h) for h in args.horizons.split(",")]
    print(f"\n{'Horizon':>8} | {'Signals':>7} | {'Win rate':>9} | "
          f"{'UP':>14} | {'DOWN':>14}")
    print("-" * 64)
    best = None
    for h in horizons:
        r = backtest(candles, h, args.confidence, cooldown_min=args.cooldown, closes=closes,
                     rsi_s=rsi_s, macd_s=macd_s, sig_s=sig_s, hist_s=hist_s)
        up = f"{r['up_win_rate']}% ({r['up_total']})"
        down = f"{r['down_win_rate']}% ({r['down_total']})"
        print(f"{str(h)+'m':>8} | {r['total']:>7} | {r['win_rate']:>8}% | "
              f"{up:>14} | {down:>14}")
        if r["total"] >= 10 and (best is None or r["win_rate"] > best["win_rate"]):
            best = r

    print("=" * 64)
    if best:
        print(f"\n🏆 Best horizon: {best['horizon_min']}m at {best['win_rate']}% "
              f"win rate ({best['total']} signals)")
        print("\n   Win rate by confidence (best horizon):")
        for bucket, info in best["by_confidence"].items():
            bar = "█" * int(info["win_rate"] / 5)
            print(f"     {bucket:>3}%+ : {info['win_rate']:>5}% {bar} ({info['count']})")
        print("\n   Note: 50% = coin flip. Above ~55% net of fees is promising;")
        print("   below 50% means the signal is worse than random for that horizon.")
    else:
        print("\n⚠️  Not enough signals fired. Lower --confidence or increase --hours.")


if __name__ == "__main__":
    main()
