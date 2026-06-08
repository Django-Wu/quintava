#!/usr/bin/env python3
"""
Engine: orchestrates live Binance data, the strategy, and the paper trader.
Maintains rolling candle history, fires signals on each closed candle, records
and resolves paper predictions, and emits state updates via `on_update`.
"""

import time
from collections import deque
from datetime import datetime, timezone

import numpy as np

from bot_core import (
    BinanceLiveClient,
    PaperTrader,
    fetch_klines,
    generate_signal,
    compute_rsi_series,
    compute_macd_series,
    reconstruct_windows,
    predict_fixed_window,
    implied_prob,
)


MAX_CANDLES = 300
M5_MEMORY_LOOKBACK = 500
M5_MEMORY_MIN_SIGNALS = 30
M5_MEMORY_MIN_WIN_RATE = 60.0
M5_MEMORY_CACHE_SEC = 10


class Engine:
    def __init__(self, symbol="BTCUSDT", interval="1m", horizon_min=5,
                 min_confidence=60, cooldown_min=None, stake=5.0,
                 starting_balance=100.0, db_path="paper_trades.db",
                 m5_strategy="follow_lead", m5_entry_min=1,
                 m5_min_move_pct=0.05, m5_min_last_candle_pct=0.0,
                 m5_min_volatility_pct=0.0):
        self.symbol = symbol
        self.interval = interval
        self.min_confidence = min_confidence
        self.window_min = 5  # matches Polymarket "BTC Up or Down 5m"
        self.m5_strategy = m5_strategy
        self.m5_entry_min = m5_entry_min
        self.m5_min_move_pct = m5_min_move_pct
        self.m5_min_last_candle_pct = m5_min_last_candle_pct
        self.m5_min_volatility_pct = m5_min_volatility_pct
        self.m5_memory_lookback = M5_MEMORY_LOOKBACK
        self.m5_memory_min_signals = M5_MEMORY_MIN_SIGNALS
        self.m5_memory_min_win_rate = M5_MEMORY_MIN_WIN_RATE
        self._m5_memory_gate_cache = (0.0, None)
        self.paused = False

        self.candles = deque(maxlen=MAX_CANDLES)  # list of Candle
        self.last_price = None
        self.last_price_ts = None
        self.last_signal = None  # dict

        self.paper = PaperTrader(
            db_path,
            horizon_min=horizon_min,
            min_confidence=min_confidence,
            cooldown_min=cooldown_min,
            stake=stake,
            starting_balance=starting_balance,
        )
        self.client = None
        self.loop = None
        self.on_update = None  # callback(dict)
        self._running = False

    # -- lifecycle --------------------------------------------------------- #
    def set_loop(self, loop):
        self.loop = loop

    def start(self):
        if self._running:
            return
        self._running = True
        # warm up with history so indicators are ready immediately
        try:
            hist = fetch_klines(self.symbol, self.interval, MAX_CANDLES)
            self.candles.extend(hist)
            if hist:
                self.last_price = hist[-1].close
                self.last_price_ts = hist[-1].open_time
            print(f"[engine] warmed up with {len(hist)} candles")
        except Exception as e:
            print(f"[engine] warmup failed: {e}")

        self.client = BinanceLiveClient(
            self.symbol, self.interval,
            on_tick=self._on_tick,
            on_candle_closed=self._on_candle_closed,
        )
        self.client.start()

    def stop(self):
        self._running = False
        if self.client:
            self.client.stop()

    # -- data callbacks ---------------------------------------------------- #
    def _on_tick(self, price, ts_ms):
        self.last_price = price
        self.last_price_ts = ts_ms
        # resolve matured paper predictions against the live price
        resolved = self.paper.resolve_due(price, ts_ms)
        self._emit("tick", {
            "price": price,
            "ts": ts_ms,
            "resolved": len(resolved),
            "stats": self.paper.stats() if resolved else None,
            "window5m": self._current_5m_window(),
        })

    def _on_candle_closed(self, candle):
        self.candles.append(candle)
        self._record_completed_5m_windows()
        if self.paused:
            return
        closes = [c.close for c in self.candles]
        res = generate_signal(closes)

        signal_payload = {
            "signal": res.signal,
            "confidence": res.confidence,
            "rsi": None if res.rsi is None else round(res.rsi, 1),
            "macd_hist": None if res.macd_hist is None else round(res.macd_hist, 2),
            "reasons": res.reasons,
            "price": candle.close,
            "ts": candle.open_time,
            "recorded": False,
        }

        if res.signal and res.confidence >= self.min_confidence:
            rid = self.paper.record(
                res.signal, res.confidence, candle.close,
                res.reasons, ts_ms=candle.open_time,
            )
            signal_payload["recorded"] = rid is not None
            self.last_signal = signal_payload

        self._emit("candle", {
            "candle": self._candle_dict(candle),
            "indicators": signal_payload,
            "stats": self.paper.stats(),
            "market5m": self.market5m(),
        })

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _candle_dict(c):
        return {
            "time": c.open_time // 1000,  # lightweight-charts uses seconds
            "open": c.open, "high": c.high, "low": c.low, "close": c.close,
        }

    def candle_history(self):
        return [self._candle_dict(c) for c in self.candles]

    def _m5_memory_gate(self, stats=None):
        """Decide whether live 5m entries are allowed based on persisted memory."""
        now = time.time()
        cached_at, cached = self._m5_memory_gate_cache
        if stats is None and cached and now - cached_at < M5_MEMORY_CACHE_SEC:
            return cached

        stats = stats or self.paper.m5_stats(limit=self.m5_memory_lookback)
        predicted = stats.get("predicted", 0)
        win_rate = stats.get("win_rate", 0.0)
        if predicted < self.m5_memory_min_signals:
            gate = {
                "allow_entries": True,
                "status": "learning",
                "label": "Память обучается",
                "reason": (
                    f"нужно {self.m5_memory_min_signals} live входов, "
                    f"сейчас {predicted}"
                ),
                "min_signals": self.m5_memory_min_signals,
                "min_win_rate": self.m5_memory_min_win_rate,
                "lookback": self.m5_memory_lookback,
                "stats": stats,
            }
        elif win_rate >= self.m5_memory_min_win_rate:
            gate = {
                "allow_entries": True,
                "status": "trusted",
                "label": "Память доверяет",
                "reason": (
                    f"live точность {win_rate}% на {predicted} входах"
                ),
                "min_signals": self.m5_memory_min_signals,
                "min_win_rate": self.m5_memory_min_win_rate,
                "lookback": self.m5_memory_lookback,
                "stats": stats,
            }
        else:
            gate = {
                "allow_entries": False,
                "status": "blocked",
                "label": "Пауза по памяти",
                "reason": (
                    f"live точность {win_rate}% ниже порога "
                    f"{self.m5_memory_min_win_rate}%"
                ),
                "min_signals": self.m5_memory_min_signals,
                "min_win_rate": self.m5_memory_min_win_rate,
                "lookback": self.m5_memory_lookback,
                "stats": stats,
            }

        self._m5_memory_gate_cache = (now, gate)
        return gate

    def _current_5m_window(self):
        """The in-progress clock-aligned Up/Down window (mirrors Polymarket 5m)."""
        if not self.candles:
            return None
        span = self.window_min * 60 * 1000
        candles = list(self.candles)
        last = candles[-1]
        ws = last.open_time - (last.open_time % span)

        first_i = next(
            (i for i, c in enumerate(candles)
             if c.open_time - (c.open_time % span) == ws),
            len(candles) - 1,
        )
        open_price = candles[first_i].open
        cur_price = self.last_price or last.close
        idxs = [
            i for i, c in enumerate(candles)
            if c.open_time - (c.open_time % span) == ws
        ]

        if first_i >= 40:
            pred, conf, reasons, meta = predict_fixed_window(
                candles,
                idxs,
                strategy=self.m5_strategy,
                entry_min=self.m5_entry_min,
                min_move_pct=self.m5_min_move_pct,
                min_last_candle_pct=self.m5_min_last_candle_pct,
                min_volatility_pct=self.m5_min_volatility_pct,
                window_min=self.window_min,
            )
        else:
            pred, conf, reasons, meta = None, 0, [], {}

        raw_pred, raw_conf = pred, conf
        memory_gate = self._m5_memory_gate()
        if pred is not None and not memory_gate["allow_entries"]:
            pred, conf = None, 0
            reasons = reasons + [memory_gate["reason"]]

        stake = self.paper.stake
        odds_price = meta.get("odds_price") if pred is not None else None
        payout_mult = meta.get("payout_mult") if pred is not None else None
        payout = round(stake * payout_mult, 2) if payout_mult else None
        profit = round(stake * (payout_mult - 1.0), 2) if payout_mult else None

        leading = "UP" if cur_price > open_price else "DOWN"

        # Live mark-to-market: shares were bought at entry odds; their value now
        # moves every tick with the current price, like an open Polymarket position.
        live_price = shares = position_value = unrealized_pnl = None
        if pred is not None and odds_price:
            now_ms = time.time() * 1000
            minutes_left_now = max(0.0, (ws + span - now_ms) / 60000.0)
            cur_move = abs((cur_price - open_price) / open_price * 100)
            p_now = implied_prob(
                cur_move, minutes_left_now, meta.get("volatility", 0.02),
                predicted_is_leader=(pred == leading),
            )
            shares = stake / odds_price
            live_price = round(p_now, 4)
            position_value = round(shares * p_now, 2)
            unrealized_pnl = round(position_value - stake, 2)
        return {
            "start_ts": ws,
            "end_ts": ws + span,
            "open_price": round(open_price, 2),
            "current_price": round(cur_price, 2),
            "prediction": pred,
            "confidence": conf,
            "raw_prediction": raw_pred,
            "raw_confidence": raw_conf,
            "reasons": reasons,
            "memory_gate": memory_gate,
            "strategy": self.m5_strategy,
            "entry_min": self.m5_entry_min,
            "min_move_pct": self.m5_min_move_pct,
            "min_last_candle_pct": self.m5_min_last_candle_pct,
            "min_volatility_pct": self.m5_min_volatility_pct,
            "leading": leading,
            "winning": (pred is not None and pred == leading),
            "move_pct": round((cur_price - open_price) / open_price * 100, 4),
            "stake": round(stake, 2),
            "odds_price": odds_price,
            "payout_mult": payout_mult,
            "payout": payout,
            "profit": profit,
            "live_price": live_price,
            "shares": round(shares, 4) if shares else None,
            "position_value": position_value,
            "unrealized_pnl": unrealized_pnl,
        }

    def _record_completed_5m_windows(self):
        """Store newly completed 5m windows so live stats survive restarts."""
        if len(self.candles) < 45:
            return
        history, _ = reconstruct_windows(
            list(self.candles),
            self.window_min,
            limit=3,
            strategy=self.m5_strategy,
            entry_min=self.m5_entry_min,
            min_move_pct=self.m5_min_move_pct,
            min_last_candle_pct=self.m5_min_last_candle_pct,
            min_volatility_pct=self.m5_min_volatility_pct,
        )
        for window in history:
            self.paper.record_m5_window(window)

    def market5m(self):
        """Full snapshot of the 5-minute Up/Down market: current + past + stats."""
        history, stats = reconstruct_windows(
            list(self.candles),
            self.window_min,
            limit=40,
            strategy=self.m5_strategy,
            entry_min=self.m5_entry_min,
            min_move_pct=self.m5_min_move_pct,
            min_last_candle_pct=self.m5_min_last_candle_pct,
            min_volatility_pct=self.m5_min_volatility_pct,
        )
        stake = self.paper.stake
        pnl_units = stats.get("pnl_units", 0.0)
        predicted = stats.get("predicted", 0)
        stats = {
            **stats,
            "stake": round(stake, 2),
            "staked": round(predicted * stake, 2),
            "pnl": round(pnl_units * stake, 2),
            "roi": round((pnl_units / predicted * 100), 2) if predicted else 0.0,
        }
        live_stats = self.paper.m5_stats(limit=self.m5_memory_lookback)
        live_stats = {
            **live_stats,
            "stake": round(stake, 2),
            "pnl": round(live_stats.get("pnl_units", 0.0) * stake, 2),
        }
        memory_gate = self._m5_memory_gate(live_stats)
        return {
            "window_min": self.window_min,
            "strategy": self.m5_strategy,
            "entry_min": self.m5_entry_min,
            "min_move_pct": self.m5_min_move_pct,
            "min_last_candle_pct": self.m5_min_last_candle_pct,
            "min_volatility_pct": self.m5_min_volatility_pct,
            "current": self._current_5m_window(),
            "history": history[::-1],  # newest first, like the site's "Past"
            "stats": stats,
            "memory_gate": memory_gate,
            "live_stats": live_stats,
            "live_recent": self.paper.recent_m5_windows(25),
        }

    def _current_indicators(self):
        closes = [c.close for c in self.candles]
        if len(closes) < 35:
            return {"rsi": None, "macd_hist": None, "signal": None, "confidence": 0}
        res = generate_signal(closes)
        return {
            "rsi": None if res.rsi is None else round(res.rsi, 1),
            "macd_hist": None if res.macd_hist is None else round(res.macd_hist, 2),
            "signal": res.signal,
            "confidence": res.confidence,
            "reasons": res.reasons,
        }

    def snapshot(self):
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "price": self.last_price,
            "price_ts": self.last_price_ts,
            "paused": self.paused,
            "min_confidence": self.min_confidence,
            "horizon_min": self.paper.horizon_min,
            "cooldown_min": self.paper.cooldown_min,
            "stake": self.paper.stake,
            "indicators": self._current_indicators(),
            "last_signal": self.last_signal,
            "stats": self.paper.stats(),
            "recent": self.paper.recent(25),
            "candles": self.candle_history(),
            "market5m": self.market5m(),
        }

    def _emit(self, kind, data):
        if self.on_update:
            self.on_update({"type": kind, "data": data})

    # -- controls ---------------------------------------------------------- #
    def set_min_confidence(self, value):
        self.min_confidence = max(0, min(100, value))
        self.paper.min_confidence = self.min_confidence

    def set_horizon(self, minutes):
        self.paper.horizon_min = max(1, minutes)
        if self.paper.cooldown_min <= 0:
            return
        self.paper.cooldown_min = self.paper.horizon_min

    def set_cooldown(self, minutes):
        self.paper.cooldown_min = max(0, minutes)

    def set_stake(self, amount):
        self.paper.stake = max(0.1, float(amount))

    def reset_paper(self):
        self.paper.reset()
        self.last_signal = None

    def auto_tune(self, hours=72, min_signals=15):
        """Grid-search confidence/horizon/cooldown on recent history and apply
        the best combination automatically. Returns the chosen settings plus a
        few runner-up candidates so the UI can explain the decision."""
        from backtest import backtest

        limit = min(hours * 60, 5000)
        candles = fetch_klines(self.symbol, self.interval, limit)
        closes = np.array([c.close for c in candles], dtype=float)
        rsi_s = compute_rsi_series(closes)
        macd_s, sig_s, hist_s = compute_macd_series(closes)

        confidences = [30, 35, 40, 45, 50, 55, 60]
        horizons = [1, 3, 5, 10, 15]
        cooldowns = [1, 3, 5]

        def search(min_sig):
            found = []
            for confidence in confidences:
                for horizon in horizons:
                    for cooldown in cooldowns:
                        r = backtest(
                            candles, horizon, confidence, cooldown_min=cooldown,
                            closes=closes, rsi_s=rsi_s, macd_s=macd_s,
                            sig_s=sig_s, hist_s=hist_s,
                        )
                        if r["total"] < min_sig:
                            continue
                        found.append({
                            "confidence": confidence,
                            "horizon": horizon,
                            "cooldown": cooldown,
                            "total": r["total"],
                            "win_rate": r["win_rate"],
                            "up_win_rate": r["up_win_rate"],
                            "down_win_rate": r["down_win_rate"],
                            "edge": round(r["avg_win_move_pct"] - r["avg_loss_move_pct"], 4),
                        })
            found.sort(
                key=lambda x: (x["win_rate"], x["total"], x["edge"]),
                reverse=True,
            )
            return found

        relaxed = False
        rows = search(min_signals)
        if not rows:
            relaxed = True
            rows = search(max(5, min_signals // 2))

        base = {
            "hours": hours,
            "candles": len(candles),
            "min_signals": min_signals,
            "relaxed": relaxed,
            "candidates": rows[:5],
        }
        if not rows:
            return {**base, "applied": False,
                    "reason": "недостаточно сигналов на истории"}

        best = rows[0]
        self.set_min_confidence(best["confidence"])
        self.set_horizon(best["horizon"])
        self.set_cooldown(best["cooldown"])
        return {
            **base,
            "applied": True,
            "best": best,
            "settings": {
                "min_confidence": self.min_confidence,
                "horizon_min": self.paper.horizon_min,
                "cooldown_min": self.paper.cooldown_min,
                "stake": self.paper.stake,
            },
        }

    def auto_tune_5m(self, hours=83, min_signals=30):
        """Optimize the fixed 5-minute Up/Down strategy and apply the best
        configuration to the live dashboard."""
        limit = min(hours * 60, 5000)
        candles = fetch_klines(self.symbol, self.interval, limit)

        strategies = [
            "follow_lead",
            "confirm_momentum",
        ]
        entries = [3, 4]
        move_thresholds = [0.08, 0.10, 0.12, 0.15]
        last_thresholds = [0, 0.02, 0.05]
        volatility_thresholds = [0, 0.02, 0.04]
        split_i = max(60, int(len(candles) * 0.65))
        train_candles = candles[:split_i]
        test_candles = candles[max(0, split_i - 40):]

        def evaluate(candle_set, cfg):
            _, stats = reconstruct_windows(
                candle_set,
                window_min=self.window_min,
                limit=40,
                strategy=cfg["strategy"],
                entry_min=cfg["entry_min"],
                min_move_pct=cfg["min_move_pct"],
                min_last_candle_pct=cfg["min_last_candle_pct"],
                min_volatility_pct=cfg["min_volatility_pct"],
            )
            return stats

        def search(min_sig):
            rows = []
            for strategy in strategies:
                for entry in entries:
                    for move_thr in move_thresholds:
                        for last_thr in last_thresholds:
                            for vol_thr in volatility_thresholds:
                                cfg = {
                                    "strategy": strategy,
                                    "entry_min": entry,
                                    "min_move_pct": move_thr,
                                    "min_last_candle_pct": last_thr,
                                    "min_volatility_pct": vol_thr,
                                }
                                train = evaluate(train_candles, cfg)
                                if train["predicted"] < min_sig:
                                    continue
                                test = evaluate(test_candles, cfg)
                                if test["predicted"] < max(8, min_sig // 3):
                                    continue
                                rows.append({
                                    **cfg,
                                    "predicted": test["predicted"],
                                    "skipped": test["skipped"],
                                    "win_rate": test["win_rate"],
                                    "wins": test["wins"],
                                    "losses": test["losses"],
                                    "up_pred": test["up_pred"],
                                    "up_pred_win_rate": test["up_pred_win_rate"],
                                    "down_pred": test["down_pred"],
                                    "down_pred_win_rate": test["down_pred_win_rate"],
                                    "up_actual_rate": test["up_actual_rate"],
                                    "train_predicted": train["predicted"],
                                    "train_win_rate": train["win_rate"],
                                    "test_predicted": test["predicted"],
                                    "test_win_rate": test["win_rate"],
                                })
            rows.sort(
                key=lambda r: (
                    r["test_win_rate"],
                    r["test_predicted"],
                    r["train_win_rate"],
                    r["up_pred"] > 0 and r["down_pred"] > 0,
                ),
                reverse=True,
            )
            return rows

        relaxed = False
        rows = search(min_signals)
        if not rows:
            relaxed = True
            rows = search(max(8, min_signals // 2))

        base = {
            "hours": hours,
            "candles": len(candles),
            "train_candles": len(train_candles),
            "test_candles": len(test_candles),
            "min_signals": min_signals,
            "relaxed": relaxed,
            "validation": "walk_forward",
            "candidates": rows[:5],
        }
        if not rows:
            return {**base, "applied": False,
                    "reason": "недостаточно 5м сигналов на истории"}

        best = rows[0]
        self.m5_strategy = best["strategy"]
        self.m5_entry_min = best["entry_min"]
        self.m5_min_move_pct = best["min_move_pct"]
        self.m5_min_last_candle_pct = best["min_last_candle_pct"]
        self.m5_min_volatility_pct = best["min_volatility_pct"]
        market = self.market5m()
        self._emit("candle", {
            "candle": self._candle_dict(list(self.candles)[-1]) if self.candles else None,
            "indicators": self._current_indicators(),
            "stats": self.paper.stats(),
            "market5m": market,
        })
        return {
            **base,
            "applied": True,
            "best": best,
            "market5m": market,
            "settings": {
                "m5_strategy": self.m5_strategy,
                "m5_entry_min": self.m5_entry_min,
                "m5_min_move_pct": self.m5_min_move_pct,
                "m5_min_last_candle_pct": self.m5_min_last_candle_pct,
                "m5_min_volatility_pct": self.m5_min_volatility_pct,
            },
        }

    def run_backtest(self, hours, confidence, horizons, cooldown=None):
        from backtest import backtest
        limit = min(hours * 60, 5000)
        candles = fetch_klines(self.symbol, self.interval, limit)
        closes = np.array([c.close for c in candles], dtype=float)
        rsi_s = compute_rsi_series(closes)
        macd_s, sig_s, hist_s = compute_macd_series(closes)
        results = []
        for h in [int(x) for x in str(horizons).split(",")]:
            r = backtest(candles, h, confidence, cooldown_min=cooldown, closes=closes,
                         rsi_s=rsi_s, macd_s=macd_s, sig_s=sig_s, hist_s=hist_s)
            results.append(r)
        return {
            "hours": hours,
            "confidence": confidence,
            "cooldown": cooldown,
            "candles": len(candles),
            "price_from": round(float(closes[0]), 2) if len(closes) else None,
            "price_to": round(float(closes[-1]), 2) if len(closes) else None,
            "results": results,
        }
