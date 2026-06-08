#!/usr/bin/env python3
"""
Core trading engine: technical indicators, signal strategy, Binance data
access (historical REST + live WebSocket), and a SQLite-backed paper trader
that measures whether the bot's UP/DOWN predictions actually come true.

This module has no web/server dependencies so it can be reused by the
backtester (backtest.py), the dashboard server (server.py) and the live bot.
"""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import erf, sqrt

import numpy as np
import requests
import websocket

BINANCE_REST = "https://api.binance.com/api/v3/klines"
BINANCE_WS = "wss://stream.binance.com:9443/ws"

# Polymarket-style payout model.
# A bet buys shares at a price (implied probability). Each winning share pays $1.
#   profit on win = stake * (1 / price - 1)
#   loss          = -stake
# The "spread" makes the price you pay slightly worse than fair, like a real book.
ODDS_SPREAD = 0.02
ODDS_MIN_PRICE = 0.50
ODDS_MAX_PRICE = 0.97


def _normal_cdf(x):
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def implied_prob(abs_move_pct, minutes_left, volatility_pct, predicted_is_leader=True):
    """
    Probability the predicted side wins given how far price is from target and
    how much time is left. Random-walk model: less time + bigger lead => more
    decided. Used both for entry odds and for live mark-to-market.
    """
    minutes_left = max(0.05, float(minutes_left))
    # Convert mean-absolute 1m move to a std estimate (E|X| = sigma*0.7979 for
    # a normal), then scale by remaining time (random-walk: std ~ sqrt(t)).
    sigma = max(0.01, float(volatility_pct) / 0.7979) * sqrt(minutes_left)
    z = abs(abs_move_pct) / sigma
    prob_leader = _normal_cdf(z)  # probability the current leader holds
    prob = prob_leader if predicted_is_leader else (1.0 - prob_leader)
    return min(0.99, max(0.01, prob))


def estimate_entry_odds(abs_move_pct, minutes_left, volatility_pct,
                        predicted_is_leader=True, spread=ODDS_SPREAD):
    """
    Estimate the market price (implied probability) of the predicted side at
    entry time, mirroring how Polymarket odds form: the further price has moved
    from target and the less time is left, the more 'decided' the window is, so
    the leading side is expensive (price near 1) and pays little.

    This is a transparent ESTIMATE, not a live Polymarket quote.
    """
    prob = implied_prob(abs_move_pct, minutes_left, volatility_pct, predicted_is_leader)
    price = min(ODDS_MAX_PRICE, max(ODDS_MIN_PRICE, prob + spread))
    return round(price, 4)


def confidence_to_odds(confidence, spread=ODDS_SPREAD):
    """Map a 0-100 confidence to an entry price for directional horizon bets."""
    prob = min(0.95, max(0.05, confidence / 100.0))
    price = min(ODDS_MAX_PRICE, max(ODDS_MIN_PRICE, prob + spread))
    return round(price, 4)


def odds_payout_multiplier(price):
    """How much $1 staked returns in total on a win (stake included)."""
    price = min(0.99, max(0.01, float(price)))
    return round(1.0 / price, 4)


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def compute_rsi(closes, period: int = 14):
    """Wilder's RSI. `closes` is a list/array of close prices (oldest first)."""
    if len(closes) < period + 1:
        return None
    closes = np.asarray(closes, dtype=float)
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _ema_series(values, period: int):
    values = np.asarray(values, dtype=float)
    k = 2.0 / (period + 1.0)
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1.0 - k)
    return out


def compute_macd(closes, fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram) for the latest point."""
    if len(closes) < slow + signal:
        return None, None, None
    closes = np.asarray(closes, dtype=float)
    macd_line = _ema_series(closes, fast) - _ema_series(closes, slow)
    signal_line = _ema_series(macd_line, signal)
    hist = macd_line[-1] - signal_line[-1]
    return float(macd_line[-1]), float(signal_line[-1]), float(hist)


def compute_rsi_series(closes, period: int = 14):
    """Full RSI series (NaN where undefined). O(n) single pass — for backtests."""
    closes = np.asarray(closes, dtype=float)
    out = np.full(len(closes), np.nan)
    if len(closes) < period + 1:
        return out
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    def rsi_val(g, l):
        if l == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[period] = rsi_val(avg_gain, avg_loss)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = rsi_val(avg_gain, avg_loss)
    return out


def compute_macd_series(closes, fast=12, slow=26, signal=9):
    """Full MACD/signal/hist series in O(n). Returns (macd, signal, hist) arrays."""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < 2:
        nan = np.full(len(closes), np.nan)
        return nan, nan, nan
    macd_line = _ema_series(closes, fast) - _ema_series(closes, slow)
    signal_line = _ema_series(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
@dataclass
class SignalResult:
    signal: str | None  # "UP", "DOWN" or None
    confidence: int
    rsi: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    reasons: list = field(default_factory=list)


def decide(rsi, macd_line, macd_sig, hist, slope) -> SignalResult:
    """
    Core decision logic given already-computed indicator values. Combines RSI
    (mean-reversion), MACD (momentum confirmation) and a short momentum slope
    into a directional signal with 0-100 confidence. Shared by live + backtest.
    Pass None for any indicator that is not yet available.
    """
    signal = None
    confidence = 0
    reasons = []

    if rsi is not None and not (isinstance(rsi, float) and rsi != rsi):  # not NaN
        if rsi < 30:
            signal, confidence = "UP", confidence + 30
            reasons.append(f"RSI {rsi:.0f} oversold")
        elif rsi > 70:
            signal, confidence = "DOWN", confidence + 30
            reasons.append(f"RSI {rsi:.0f} overbought")

    if hist is not None and not (isinstance(hist, float) and hist != hist):
        if hist > 0 and macd_line > macd_sig:
            if signal == "UP":
                confidence += 35
                reasons.append("MACD bullish (confirms)")
            elif signal is None:
                signal, confidence = "UP", 30
                reasons.append("MACD bullish")
        elif hist < 0 and macd_line < macd_sig:
            if signal == "DOWN":
                confidence += 35
                reasons.append("MACD bearish (confirms)")
            elif signal is None:
                signal, confidence = "DOWN", 30
                reasons.append("MACD bearish")

    if slope is not None and signal is not None:
        if signal == "UP" and slope > 0:
            confidence += 15
            reasons.append("upward momentum")
        elif signal == "DOWN" and slope < 0:
            confidence += 15
            reasons.append("downward momentum")

    confidence = min(confidence, 100)
    return SignalResult(signal, confidence, rsi, macd_line, macd_sig, hist, reasons)


def generate_signal(closes) -> SignalResult:
    """Convenience wrapper: compute indicators from a close history, then decide."""
    rsi = compute_rsi(closes)
    macd_line, macd_sig, hist = compute_macd(closes)
    slope = None
    if len(closes) >= 4:
        slope = float(closes[-1]) - float(closes[-4])
    return decide(rsi, macd_line, macd_sig, hist, slope)


# --------------------------------------------------------------------------- #
# Binance data access
# --------------------------------------------------------------------------- #
@dataclass
class Candle:
    open_time: int  # ms epoch (candle open)
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def dt(self):
        return datetime.fromtimestamp(self.open_time / 1000, tz=timezone.utc)


def fetch_klines(symbol: str = "BTCUSDT", interval: str = "1m", limit: int = 500):
    """Fetch historical candles from Binance REST (oldest first)."""
    out = []
    remaining = limit
    end_time = None
    while remaining > 0:
        batch = min(remaining, 1000)
        params = {"symbol": symbol, "interval": interval, "limit": batch}
        if end_time is not None:
            params["endTime"] = end_time
        resp = requests.get(BINANCE_REST, params=params, timeout=10)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        chunk = [
            Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
            for r in rows
        ]
        out = chunk + out
        remaining -= len(chunk)
        end_time = chunk[0].open_time - 1
        if len(chunk) < batch:
            break
    # de-dup just in case and sort
    seen = {}
    for c in out:
        seen[c.open_time] = c
    return [seen[k] for k in sorted(seen)][-limit:]


# --------------------------------------------------------------------------- #
# Fixed clock-aligned Up/Down market (mirrors Polymarket "BTC Up or Down 5m")
# --------------------------------------------------------------------------- #
def floor_to_window(ts_ms, minutes):
    """Snap a timestamp down to the start of its clock-aligned window."""
    span = minutes * 60 * 1000
    return ts_ms - (ts_ms % span)


def predict_fixed_window(
    candles,
    idxs,
    strategy="follow_lead",
    entry_min=2,
    min_move_pct=0.02,
    min_last_candle_pct=0.0,
    min_volatility_pct=0.0,
    volatility_lookback=15,
    window_min=5,
):
    """
    Predict a fixed 5m Up/Down window using only candles available at entry time.

    entry_min=2 means: wait until two 1m candles inside the 5m window have closed,
    then decide. This mirrors a real Polymarket workflow: target is known at the
    window open, but the bot can choose to enter only when the live move is strong.

    Returns (prediction, confidence, reasons, meta). `meta` carries the estimated
    Polymarket-style entry odds and payout when a bet is placed.

    Strategies:
      - follow_lead: bet the current side of target (price > target -> UP)
      - reverse_lead: bet reversal against the current side of target
      - confirm_momentum: follow target side only if latest 1m candle agrees
      - fade_momentum: reverse target side only if latest 1m candle is stretched
    """
    if not idxs:
        return None, 0, [], {}

    first_i = idxs[0]
    entry_i = first_i + max(0, entry_min) - 1
    if entry_i >= len(candles) or entry_i > idxs[-1]:
        return None, 0, [], {}

    target = candles[first_i].open
    entry_price = candles[entry_i].close
    move_pct = (entry_price - target) / target * 100
    abs_move = abs(move_pct)
    if abs_move < min_move_pct:
        return None, 0, [f"move {abs_move:.3f}% below threshold"], {}

    start_i = max(1, entry_i - volatility_lookback + 1)
    recent_moves = []
    for i in range(start_i, entry_i + 1):
        prev_close = candles[i - 1].close
        if prev_close:
            recent_moves.append(abs((candles[i].close - prev_close) / prev_close * 100))
    avg_volatility = float(np.mean(recent_moves)) if recent_moves else 0.0
    if min_volatility_pct > 0 and avg_volatility < min_volatility_pct:
        return None, 0, [f"volatility {avg_volatility:.3f}% below threshold"], {}

    latest_open = candles[entry_i].open
    latest_move_pct = (entry_price - latest_open) / latest_open * 100
    if abs(latest_move_pct) < min_last_candle_pct:
        return None, 0, [f"latest candle {abs(latest_move_pct):.3f}% too small"], {}

    lead = "UP" if move_pct > 0 else "DOWN"
    latest_dir = "UP" if latest_move_pct > 0 else "DOWN"
    reasons = [
        f"entry +{entry_min}m",
        f"price {'above' if lead == 'UP' else 'below'} target {abs_move:.3f}%",
    ]

    confidence = min(95, int(50 + abs_move * 450 + abs(latest_move_pct) * 200))

    if strategy == "follow_lead":
        pred, reasons = lead, reasons + ["follow current side"]
    elif strategy == "reverse_lead":
        pred = "DOWN" if lead == "UP" else "UP"
        reasons = reasons + ["fade current side"]
    elif strategy == "confirm_momentum":
        if latest_dir != lead:
            return None, 0, reasons + ["latest candle disagrees"], {}
        pred, reasons = lead, reasons + ["momentum confirms"]
    elif strategy == "fade_momentum":
        if latest_dir != lead:
            return None, 0, reasons + ["not stretched"], {}
        pred = "DOWN" if lead == "UP" else "UP"
        reasons = reasons + ["fade stretched move"]
    else:
        raise ValueError(f"Unknown 5m strategy: {strategy}")

    minutes_left = max(0.5, window_min - entry_min)
    price = estimate_entry_odds(
        abs_move, minutes_left, avg_volatility,
        predicted_is_leader=(pred == lead),
    )
    meta = {
        "abs_move": round(abs_move, 4),
        "volatility": round(avg_volatility, 4),
        "minutes_left": minutes_left,
        "entry_btc_price": round(entry_price, 2),
        "odds_price": price,
        "payout_mult": odds_payout_multiplier(price),
    }
    return pred, confidence, reasons, meta


def reconstruct_windows(
    candles,
    window_min=5,
    warmup=40,
    limit=40,
    strategy="follow_lead",
    entry_min=2,
    min_move_pct=0.02,
    min_last_candle_pct=0.0,
    min_volatility_pct=0.0,
):
    """
    Rebuild fixed clock-aligned UP/DOWN windows (like Polymarket's 5m market)
    from 1-minute candles. For every *completed* window we record:

      - open_price  : price at the window start (the market "target")
      - close_price : price at the window end
      - actual      : "UP" if close > open else "DOWN" (this is what the site shows)
      - prediction  : what the bot would have called, using ONLY data available
                      at the configured entry time inside the window
      - correct     : whether the bot's call matched the actual outcome

    Returns (recent_windows, stats).
    """
    if not candles:
        return [], window_stats([])

    span = window_min * 60 * 1000
    groups = {}
    order = []
    for i, c in enumerate(candles):
        ws = c.open_time - (c.open_time % span)
        if ws not in groups:
            groups[ws] = []
            order.append(ws)
        groups[ws].append(i)

    windows = []
    for wi, ws in enumerate(order):
        if wi == len(order) - 1:
            continue  # last group is the in-progress window, not resolved yet
        idxs = groups[ws]
        first_i = idxs[0]
        open_price = candles[first_i].open
        close_price = candles[idxs[-1]].close
        actual = "UP" if close_price > open_price else "DOWN"

        if first_i >= warmup:
            pred, conf, reasons, meta = predict_fixed_window(
                candles,
                idxs,
                strategy=strategy,
                entry_min=entry_min,
                min_move_pct=min_move_pct,
                min_last_candle_pct=min_last_candle_pct,
                min_volatility_pct=min_volatility_pct,
                window_min=window_min,
            )
        else:
            pred, conf, reasons, meta = None, 0, [], {}

        windows.append({
            "start_ts": ws,
            "end_ts": ws + span,
            "open_price": round(open_price, 2),
            "close_price": round(close_price, 2),
            "actual": actual,
            "prediction": pred,
            "confidence": conf,
            "reasons": reasons,
            "strategy": strategy,
            "entry_min": entry_min,
            "min_move_pct": min_move_pct,
            "min_last_candle_pct": min_last_candle_pct,
            "min_volatility_pct": min_volatility_pct,
            "correct": (pred == actual) if pred is not None else None,
            "move_pct": round((close_price - open_price) / open_price * 100, 4),
            "odds_price": meta.get("odds_price"),
            "payout_mult": meta.get("payout_mult"),
        })

    return windows[-limit:], window_stats(windows)


def window_stats(windows):
    """Aggregate accuracy for reconstructed Up/Down windows."""
    predicted = [w for w in windows if w["prediction"] is not None]
    resolved = len(predicted)
    wins = sum(1 for w in predicted if w["correct"])
    up_pred = [w for w in predicted if w["prediction"] == "UP"]
    down_pred = [w for w in predicted if w["prediction"] == "DOWN"]
    up_win = sum(1 for w in up_pred if w["correct"])
    down_win = sum(1 for w in down_pred if w["correct"])

    # actual base rates (how often the market itself went up vs down)
    up_actual = sum(1 for w in windows if w["actual"] == "UP")
    down_actual = sum(1 for w in windows if w["actual"] == "DOWN")

    # current win/loss streak of the bot (most recent first)
    streak = 0
    streak_kind = None
    for w in reversed(predicted):
        kind = "win" if w["correct"] else "loss"
        if streak_kind is None:
            streak_kind = kind
            streak = 1
        elif kind == streak_kind:
            streak += 1
        else:
            break

    # Odds-based P&L expressed in units of one stake (engine multiplies by $stake).
    # Win pays (payout_mult - 1) of the stake as profit; a loss costs the full stake.
    pnl_units = 0.0
    payouts = []
    for w in predicted:
        mult = w.get("payout_mult") or odds_payout_multiplier(w.get("odds_price") or 0.5)
        payouts.append(mult)
        pnl_units += (mult - 1.0) if w["correct"] else -1.0
    avg_payout_mult = round(sum(payouts) / len(payouts), 3) if payouts else 0.0

    return {
        "total_windows": len(windows),
        "predicted": resolved,
        "skipped": len(windows) - resolved,
        "wins": wins,
        "losses": resolved - wins,
        "win_rate": round(wins / resolved * 100, 1) if resolved else 0.0,
        "up_pred": len(up_pred),
        "up_pred_win_rate": round(up_win / len(up_pred) * 100, 1) if up_pred else 0.0,
        "down_pred": len(down_pred),
        "down_pred_win_rate": round(down_win / len(down_pred) * 100, 1) if down_pred else 0.0,
        "up_actual": up_actual,
        "down_actual": down_actual,
        "up_actual_rate": round(up_actual / len(windows) * 100, 1) if windows else 0.0,
        "streak": streak,
        "streak_kind": streak_kind,
        "pnl_units": round(pnl_units, 4),
        "roi_pct": round(pnl_units / resolved * 100, 1) if resolved else 0.0,
        "avg_payout_mult": avg_payout_mult,
    }


class BinanceLiveClient:
    """
    Streams 1-minute klines. Calls:
      - on_tick(price, ts_ms) on every message (live close of current candle)
      - on_candle_closed(Candle) when a candle finalizes (x == True)
    Auto-reconnects on disconnect.
    """

    def __init__(self, symbol, interval, on_tick=None, on_candle_closed=None):
        self.symbol = symbol.lower()
        self.interval = interval
        self.on_tick = on_tick
        self.on_candle_closed = on_candle_closed
        self.ws = None
        self.running = True

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            k = data.get("k")
            if not k:
                return
            price = float(k["c"])
            ts = int(k["T"])
            if self.on_tick:
                self.on_tick(price, ts)
            if k.get("x") and self.on_candle_closed:
                candle = Candle(
                    int(k["t"]), float(k["o"]), float(k["h"]),
                    float(k["l"]), float(k["c"]), float(k["v"]),
                )
                self.on_candle_closed(candle)
        except Exception as e:
            print(f"[binance] message error: {e}")

    def _on_open(self, ws):
        ws.send(json.dumps({
            "method": "SUBSCRIBE",
            "params": [f"{self.symbol}@kline_{self.interval}"],
            "id": 1,
        }))
        print("[binance] websocket connected")

    def _on_error(self, ws, error):
        print(f"[binance] websocket error: {error}")

    def _on_close(self, ws, code, msg):
        print("[binance] websocket closed")

    def run_forever(self):
        import ssl
        while self.running:
            self.ws = websocket.WebSocketApp(
                BINANCE_WS,
                on_message=self._on_message,
                on_open=self._on_open,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self.ws.run_forever(
                ping_interval=30, ping_timeout=10,
                sslopt={"cert_reqs": ssl.CERT_NONE},
            )
            if self.running:
                print("[binance] reconnecting in 3s...")
                time.sleep(3)

    def start(self):
        t = threading.Thread(target=self.run_forever, daemon=True)
        t.start()
        return t

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()


# --------------------------------------------------------------------------- #
# Paper trader (accuracy tracking)
# --------------------------------------------------------------------------- #
class PaperTrader:
    """
    Records every prediction and, after `horizon_min` minutes, checks whether
    BTC actually moved in the predicted direction. Persists to SQLite so the
    measured accuracy survives restarts.
    """

    def __init__(
        self,
        db_path="paper_trades.db",
        horizon_min=5,
        min_confidence=60,
        cooldown_min=None,
        allow_overlap=False,
        stake=5.0,
        starting_balance=100.0,
        payout_ratio=1.0,
        fee=0.0,
    ):
        self.db_path = db_path
        self.horizon_min = horizon_min
        self.min_confidence = min_confidence
        self.cooldown_min = horizon_min if cooldown_min is None else cooldown_min
        self.allow_overlap = allow_overlap
        # Money model (Polymarket-style even-money binary by default):
        #   win  -> +stake * payout_ratio  (minus fee)
        #   loss -> -stake                  (minus fee)
        self.stake = stake
        self.starting_balance = starting_balance
        self.payout_ratio = payout_ratio
        self.fee = fee
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS predictions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts  INTEGER NOT NULL,
                resolve_ts  INTEGER NOT NULL,
                signal      TEXT NOT NULL,
                confidence  INTEGER NOT NULL,
                horizon_min INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                exit_price  REAL,
                move_pct    REAL,
                stake       REAL,
                entry_odds  REAL,
                pnl         REAL,
                resolved    INTEGER NOT NULL DEFAULT 0,
                correct     INTEGER,
                reasons     TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS m5_windows (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                start_ts                 INTEGER NOT NULL UNIQUE,
                end_ts                   INTEGER NOT NULL,
                strategy                 TEXT NOT NULL,
                entry_min                INTEGER NOT NULL,
                min_move_pct             REAL NOT NULL,
                min_last_candle_pct      REAL NOT NULL,
                min_volatility_pct       REAL NOT NULL DEFAULT 0,
                open_price               REAL NOT NULL,
                close_price              REAL NOT NULL,
                move_pct                 REAL NOT NULL,
                actual                   TEXT NOT NULL,
                prediction               TEXT,
                confidence               INTEGER NOT NULL,
                correct                  INTEGER,
                reasons                  TEXT,
                odds_price               REAL,
                payout_mult              REAL,
                recorded_ts              INTEGER NOT NULL
            )
            """
        )
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(predictions)").fetchall()
        }
        if "move_pct" not in columns:
            self._conn.execute("ALTER TABLE predictions ADD COLUMN move_pct REAL")
        if "stake" not in columns:
            self._conn.execute("ALTER TABLE predictions ADD COLUMN stake REAL")
        if "entry_odds" not in columns:
            self._conn.execute("ALTER TABLE predictions ADD COLUMN entry_odds REAL")
        if "pnl" not in columns:
            self._conn.execute("ALTER TABLE predictions ADD COLUMN pnl REAL")
        m5_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(m5_windows)").fetchall()
        }
        if "min_volatility_pct" not in m5_columns:
            self._conn.execute(
                "ALTER TABLE m5_windows ADD COLUMN min_volatility_pct REAL NOT NULL DEFAULT 0"
            )
        if "odds_price" not in m5_columns:
            self._conn.execute("ALTER TABLE m5_windows ADD COLUMN odds_price REAL")
        if "payout_mult" not in m5_columns:
            self._conn.execute("ALTER TABLE m5_windows ADD COLUMN payout_mult REAL")
        self._conn.commit()
        self._backfill_odds()

    def _backfill_odds(self):
        """Give legacy bets odds-based payouts so P&L is consistent everywhere."""
        rows = self._conn.execute(
            "SELECT id, confidence, stake, correct, resolved FROM predictions "
            "WHERE entry_odds IS NULL"
        ).fetchall()
        for row in rows:
            odds = confidence_to_odds(row["confidence"])
            self._conn.execute(
                "UPDATE predictions SET entry_odds=? WHERE id=?", (odds, row["id"])
            )
            if row["resolved"]:
                stake = row["stake"] if row["stake"] is not None else self.stake
                pnl = self._pnl_for(bool(row["correct"]), odds, stake)
                self._conn.execute(
                    "UPDATE predictions SET pnl=? WHERE id=?", (float(pnl), row["id"])
                )

        # Legacy 5m windows predate odds capture; estimate from confidence so the
        # live "memory" P&L is consistent instead of defaulting to even-money 2x.
        m5_rows = self._conn.execute(
            "SELECT id, confidence FROM m5_windows "
            "WHERE prediction IS NOT NULL AND payout_mult IS NULL"
        ).fetchall()
        for row in m5_rows:
            odds = confidence_to_odds(row["confidence"])
            self._conn.execute(
                "UPDATE m5_windows SET odds_price=?, payout_mult=? WHERE id=?",
                (odds, odds_payout_multiplier(odds), row["id"]),
            )
        if rows or m5_rows:
            self._conn.commit()

    def _pnl_for(self, correct, entry_odds=None, stake=None):
        """
        Dollar profit/loss for one resolved bet, Polymarket-style.

        A bet buys shares at `entry_odds` (price 0-1). On a win each share pays
        $1, so profit = stake * (1/price - 1). On a loss the stake is lost.
        Falls back to even-money if no odds were stored (legacy rows).
        """
        stake = self.stake if stake is None else stake
        if not correct:
            return -stake - self.fee
        if entry_odds and entry_odds > 0:
            return stake * (1.0 / float(entry_odds) - 1.0) - self.fee
        return stake * self.payout_ratio - self.fee

    def _is_in_cooldown(self, created_ts):
        if self.allow_overlap or self.cooldown_min <= 0:
            return False
        row = self._conn.execute(
            "SELECT created_ts FROM predictions ORDER BY created_ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return False
        return created_ts < row["created_ts"] + self.cooldown_min * 60 * 1000

    def record(self, signal, confidence, entry_price, reasons=None, ts_ms=None):
        """Record a new prediction. Returns the row id, or None if filtered out."""
        if signal not in ("UP", "DOWN") or confidence < self.min_confidence:
            return None
        created = int(ts_ms if ts_ms is not None else time.time() * 1000)
        resolve = created + self.horizon_min * 60 * 1000
        with self._lock:
            if self._is_in_cooldown(created):
                return None
            entry_odds = confidence_to_odds(confidence)
            cur = self._conn.execute(
                """INSERT INTO predictions
                   (created_ts, resolve_ts, signal, confidence, horizon_min,
                    entry_price, stake, entry_odds, reasons)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (created, resolve, signal, int(confidence), self.horizon_min,
                 float(entry_price), float(self.stake), float(entry_odds),
                 json.dumps(reasons or [])),
            )
            self._conn.commit()
            return cur.lastrowid

    def resolve_due(self, current_price, now_ms=None):
        """Resolve all matured predictions against the current price."""
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        resolved = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM predictions WHERE resolved = 0 AND resolve_ts <= ?",
                (now,),
            ).fetchall()
            for row in rows:
                move_pct = (current_price - row["entry_price"]) / row["entry_price"] * 100
                correct = (
                    (row["signal"] == "UP" and current_price > row["entry_price"]) or
                    (row["signal"] == "DOWN" and current_price < row["entry_price"])
                )
                pnl = self._pnl_for(correct, row["entry_odds"], row["stake"])
                self._conn.execute(
                    """UPDATE predictions
                       SET resolved=1, exit_price=?, move_pct=?, correct=?, pnl=?
                       WHERE id=?""",
                    (float(current_price), float(move_pct),
                     1 if correct else 0, float(pnl), row["id"]),
                )
                resolved.append((row["id"], bool(correct)))
            if resolved:
                self._conn.commit()
        return resolved

    def resolve_at_price_history(self, price_at):
        """
        For backtests: resolve using a callable price_at(ts_ms) -> price.
        Resolves every pending prediction whose horizon has data.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM predictions WHERE resolved = 0"
            ).fetchall()
            for row in rows:
                exit_price = price_at(row["resolve_ts"])
                if exit_price is None:
                    continue
                move_pct = (exit_price - row["entry_price"]) / row["entry_price"] * 100
                correct = (
                    (row["signal"] == "UP" and exit_price > row["entry_price"]) or
                    (row["signal"] == "DOWN" and exit_price < row["entry_price"])
                )
                pnl = self._pnl_for(correct, row["entry_odds"], row["stake"])
                self._conn.execute(
                    """UPDATE predictions
                       SET resolved=1, exit_price=?, move_pct=?, correct=?, pnl=?
                       WHERE id=?""",
                    (float(exit_price), float(move_pct),
                     1 if correct else 0, float(pnl), row["id"]),
                )
            self._conn.commit()

    def stats(self):
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c FROM predictions").fetchone()["c"]
            resolved = self._conn.execute(
                "SELECT COUNT(*) c FROM predictions WHERE resolved=1"
            ).fetchone()["c"]
            wins = self._conn.execute(
                "SELECT COUNT(*) c FROM predictions WHERE resolved=1 AND correct=1"
            ).fetchone()["c"]
            pending = total - resolved
            up = self._conn.execute(
                "SELECT COUNT(*) c FROM predictions WHERE resolved=1 AND signal='UP' AND correct=1"
            ).fetchone()["c"]
            up_tot = self._conn.execute(
                "SELECT COUNT(*) c FROM predictions WHERE resolved=1 AND signal='UP'"
            ).fetchone()["c"]
            down = self._conn.execute(
                "SELECT COUNT(*) c FROM predictions WHERE resolved=1 AND signal='DOWN' AND correct=1"
            ).fetchone()["c"]
            down_tot = self._conn.execute(
                "SELECT COUNT(*) c FROM predictions WHERE resolved=1 AND signal='DOWN'"
            ).fetchone()["c"]
            avg_move = self._conn.execute(
                "SELECT AVG(ABS(move_pct)) v FROM predictions WHERE resolved=1"
            ).fetchone()["v"]
            avg_win_move = self._conn.execute(
                """SELECT AVG(ABS(move_pct)) v FROM predictions
                   WHERE resolved=1 AND correct=1"""
            ).fetchone()["v"]
            avg_loss_move = self._conn.execute(
                """SELECT AVG(ABS(move_pct)) v FROM predictions
                   WHERE resolved=1 AND correct=0"""
            ).fetchone()["v"]
            pnl_total = self._conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) v FROM predictions WHERE resolved=1"
            ).fetchone()["v"]
            staked = self._conn.execute(
                "SELECT COALESCE(SUM(stake), 0) v FROM predictions WHERE resolved=1"
            ).fetchone()["v"]
            best = self._conn.execute(
                "SELECT MAX(pnl) v FROM predictions WHERE resolved=1"
            ).fetchone()["v"]
            worst = self._conn.execute(
                "SELECT MIN(pnl) v FROM predictions WHERE resolved=1"
            ).fetchone()["v"]
        win_rate = (wins / resolved * 100) if resolved else 0.0
        balance = self.starting_balance + (pnl_total or 0.0)
        roi = ((pnl_total or 0.0) / staked * 100) if staked else 0.0
        return {
            "total": total,
            "resolved": resolved,
            "pending": pending,
            "wins": wins,
            "losses": resolved - wins,
            "win_rate": round(win_rate, 1),
            "up_win_rate": round(up / up_tot * 100, 1) if up_tot else 0.0,
            "up_count": up_tot,
            "down_win_rate": round(down / down_tot * 100, 1) if down_tot else 0.0,
            "down_count": down_tot,
            "avg_move_pct": round(avg_move or 0.0, 4),
            "avg_win_move_pct": round(avg_win_move or 0.0, 4),
            "avg_loss_move_pct": round(avg_loss_move or 0.0, 4),
            "stake": round(self.stake, 2),
            "starting_balance": round(self.starting_balance, 2),
            "pnl": round(pnl_total or 0.0, 2),
            "staked": round(staked or 0.0, 2),
            "balance": round(balance, 2),
            "roi": round(roi, 2),
            "best_trade": round(best or 0.0, 2),
            "worst_trade": round(worst or 0.0, 2),
        }

    def recent(self, limit=25):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM predictions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def record_m5_window(self, window):
        """Persist one completed 5-minute market window for live learning."""
        if not window:
            return False
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO m5_windows
                   (start_ts, end_ts, strategy, entry_min, min_move_pct,
                    min_last_candle_pct, min_volatility_pct,
                    open_price, close_price, move_pct,
                    actual, prediction, confidence, correct, reasons,
                    odds_price, payout_mult, recorded_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(window["start_ts"]),
                    int(window["end_ts"]),
                    str(window.get("strategy") or ""),
                    int(window.get("entry_min") or 0),
                    float(window.get("min_move_pct") or 0.0),
                    float(window.get("min_last_candle_pct") or 0.0),
                    float(window.get("min_volatility_pct") or 0.0),
                    float(window["open_price"]),
                    float(window["close_price"]),
                    float(window.get("move_pct") or 0.0),
                    str(window["actual"]),
                    window.get("prediction"),
                    int(window.get("confidence") or 0),
                    None if window.get("correct") is None else int(bool(window["correct"])),
                    json.dumps(window.get("reasons") or []),
                    window.get("odds_price"),
                    window.get("payout_mult"),
                    int(time.time() * 1000),
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def m5_stats(self, limit=None):
        """Stats from persisted 5-minute windows (live memory, not backtest)."""
        with self._lock:
            where = ""
            params = ()
            if limit:
                where = "WHERE id IN (SELECT id FROM m5_windows ORDER BY id DESC LIMIT ?)"
                params = (int(limit),)
            rows = self._conn.execute(
                f"SELECT * FROM m5_windows {where} ORDER BY id ASC", params
            ).fetchall()

        windows = [dict(r) for r in rows]
        predicted = [w for w in windows if w["prediction"] is not None]
        wins = sum(1 for w in predicted if w["correct"] == 1)
        losses = sum(1 for w in predicted if w["correct"] == 0)
        up_pred = [w for w in predicted if w["prediction"] == "UP"]
        down_pred = [w for w in predicted if w["prediction"] == "DOWN"]
        up_wins = sum(1 for w in up_pred if w["correct"] == 1)
        down_wins = sum(1 for w in down_pred if w["correct"] == 1)

        pnl_units = 0.0
        payouts = []
        for w in predicted:
            mult = w.get("payout_mult") or odds_payout_multiplier(w.get("odds_price") or 0.5)
            payouts.append(mult)
            pnl_units += (mult - 1.0) if w["correct"] == 1 else -1.0
        return {
            "total_windows": len(windows),
            "predicted": len(predicted),
            "skipped": len(windows) - len(predicted),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(predicted) * 100, 1) if predicted else 0.0,
            "up_pred": len(up_pred),
            "up_pred_win_rate": round(up_wins / len(up_pred) * 100, 1) if up_pred else 0.0,
            "down_pred": len(down_pred),
            "down_pred_win_rate": round(down_wins / len(down_pred) * 100, 1) if down_pred else 0.0,
            "pnl_units": round(pnl_units, 4),
            "roi_pct": round(pnl_units / len(predicted) * 100, 1) if predicted else 0.0,
            "avg_payout_mult": round(sum(payouts) / len(payouts), 3) if payouts else 0.0,
        }

    def recent_m5_windows(self, limit=25):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM m5_windows ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def reset(self):
        with self._lock:
            self._conn.execute("DELETE FROM predictions")
            self._conn.execute("DELETE FROM m5_windows")
            self._conn.commit()
