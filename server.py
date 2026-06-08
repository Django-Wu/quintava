#!/usr/bin/env python3
"""
Real-time dashboard server for the Polymarket BTC bot.

Runs the trading engine in the background (Binance live data + strategy +
paper trading), serves a web dashboard, and pushes live state to the browser
over a WebSocket. No real money is used in paper mode.

Run:
  python server.py
  # then open http://localhost:8000
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from engine import Engine

engine = Engine(
    symbol="BTCUSDT",
    interval="1m",
    horizon_min=int(os.getenv("HORIZON_MIN", "3")),
    min_confidence=int(os.getenv("MIN_CONFIDENCE", "30")),
    cooldown_min=int(os.getenv("COOLDOWN_MIN", "3")),
    stake=float(os.getenv("STAKE", "5")),
    starting_balance=float(os.getenv("STARTING_BALANCE", "100")),
    m5_strategy=os.getenv("M5_STRATEGY", "follow_lead"),
    m5_entry_min=int(os.getenv("M5_ENTRY_MIN", "1")),
    m5_min_move_pct=float(os.getenv("M5_MIN_MOVE_PCT", "0.05")),
    m5_min_last_candle_pct=float(os.getenv("M5_MIN_LAST_CANDLE_PCT", "0")),
    m5_min_volatility_pct=float(os.getenv("M5_MIN_VOLATILITY_PCT", "0.02")),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.set_loop(asyncio.get_running_loop())
    engine.start()
    yield
    engine.stop()


app = FastAPI(title="Quintava Dashboard", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html")) as f:
        return f.read()


@app.get("/api/state")
async def api_state():
    return engine.snapshot()


@app.get("/api/history")
async def api_history():
    return {"candles": engine.candle_history()}


@app.post("/api/control")
async def api_control(payload: dict):
    action = payload.get("action")
    if action == "set_confidence":
        engine.set_min_confidence(int(payload["value"]))
    elif action == "set_horizon":
        engine.set_horizon(int(payload["value"]))
    elif action == "set_cooldown":
        engine.set_cooldown(int(payload["value"]))
    elif action == "set_stake":
        engine.set_stake(float(payload["value"]))
    elif action == "pause":
        engine.paused = True
    elif action == "resume":
        engine.paused = False
    elif action == "reset":
        engine.reset_paper()
    return engine.snapshot()


@app.post("/api/autotune")
async def api_autotune(payload: dict | None = None):
    payload = payload or {}
    hours = int(payload.get("hours", 72))
    min_signals = int(payload.get("min_signals", 15))
    return await asyncio.to_thread(engine.auto_tune, hours, min_signals)


@app.post("/api/autotune-5m")
async def api_autotune_5m(payload: dict | None = None):
    payload = payload or {}
    hours = int(payload.get("hours", 83))
    min_signals = int(payload.get("min_signals", 30))
    return await asyncio.to_thread(engine.auto_tune_5m, hours, min_signals)


@app.get("/api/backtest")
async def api_backtest(
    hours: int = 48,
    confidence: int = 60,
    horizons: str = "1,3,5,15",
    cooldown: int | None = None,
):
    return await asyncio.to_thread(
        engine.run_backtest, hours, confidence, horizons, cooldown
    )


class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


hub = Hub()
engine.on_update = lambda msg: asyncio.run_coroutine_threadsafe(
    hub.broadcast(msg), engine.loop
) if engine.loop else None


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await hub.connect(ws)
    try:
        await ws.send_json({"type": "snapshot", "data": engine.snapshot()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception:
        hub.disconnect(ws)


if __name__ == "__main__":
    import uvicorn
    print("🚀 Quintava dashboard at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
