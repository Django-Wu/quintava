#!/usr/bin/env python3
"""
Polymarket Trading Bot v2
Using Developer API credentials for authenticated trading

Usage:
  python polymarket_bot_v2.py --verify    # quick diagnostics (no trades)
  python polymarket_bot_v2.py --dry-run   # full monitoring, log signals only
  python polymarket_bot_v2.py               # live trading (requires USDC balance)
"""

import argparse
import json
import websocket
import requests
import os
import sys
import hmac
import hashlib
import base64
import time
from datetime import datetime
from collections import deque
import threading
from dotenv import load_dotenv

load_dotenv()

class PolymarketTraderAPI:
    """Polymarket Trader API client with proper authentication"""
    
    def __init__(self, api_key, secret, passphrase, address=None):
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.address = address
        self.base_url = "https://clob.polymarket.com"
    
    def _generate_auth_headers(self, method, path, body=""):
        """Generate L2 authentication headers for Polymarket CLOB API"""
        timestamp = str(int(time.time()))
        message = timestamp + method + path + body
        
        try:
            secret_bytes = base64.urlsafe_b64decode(self.secret)
        except Exception:
            secret_bytes = self.secret.encode()
        
        signature = base64.urlsafe_b64encode(
            hmac.new(secret_bytes, message.encode(), hashlib.sha256).digest()
        ).decode()
        
        headers = {
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.address:
            headers["POLY_ADDRESS"] = self.address
        return headers
    
    def get_balance_allowance(self):
        """Get account balance and allowance"""
        try:
            path = "/balance-allowance"
            headers = self._generate_auth_headers("GET", path)
            response = requests.get(f"{self.base_url}{path}", headers=headers, timeout=5)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            print(f"Error: {e}")
            return None
    
    def get_user(self):
        """Get user information and balance"""
        balance = self.get_balance_allowance()
        if balance:
            return balance
        
        try:
            path = "/data/orders"
            headers = self._generate_auth_headers("GET", path)
            response = requests.get(f"{self.base_url}{path}", headers=headers, timeout=5)
            if response.status_code == 200:
                return {"balance": 0, "orders": response.json()}
        except Exception:
            pass
        
        return None
    
    def test_auth(self):
        """Test whether API credentials are accepted"""
        if not self.address:
            return False, "POLYMARKET_ADDRESS missing in .env"
        
        path = "/data/orders"
        headers = self._generate_auth_headers("GET", path)
        try:
            response = requests.get(f"{self.base_url}{path}", headers=headers, timeout=5)
            if response.status_code == 200:
                return True, "credentials accepted"
            return False, f"HTTP {response.status_code}: {response.text[:80]}"
        except Exception as e:
            return False, str(e)
    
    def get_markets(self, search_term=""):
        """Get markets from public endpoint (no auth needed)"""
        try:
            url = "https://gamma-api.polymarket.com/markets"
            if search_term:
                url += f"?search_term={search_term}"
            
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                return response.json()
            else:
                print(f"Error getting markets: {response.status_code}")
                return []
        except Exception as e:
            print(f"Error: {e}")
            return []
    
    def get_orderbook(self, token_id):
        """Get orderbook for a market token"""
        try:
            url = f"{self.base_url}/book?token_id={token_id}"
            response = requests.get(url, timeout=5)
            
            if response.status_code == 200:
                return response.json()
            else:
                print(f"Error getting orderbook: {response.status_code}")
                return None
        except Exception as e:
            print(f"Error: {e}")
            return None
    
    def create_order(self, token_id, side, size, price):
        """
        Create an order
        side: "BUY" or "SELL"
        size: number of shares
        price: price (0.01 to 0.99)
        """
        try:
            path = "/order"
            body = json.dumps({
                "tokenID": token_id,
                "side": side,
                "size": str(size),
                "price": str(price),
            })
            
            headers = self._generate_auth_headers("POST", path, body)
            response = requests.post(
                f"{self.base_url}{path}",
                headers=headers,
                data=body,
                timeout=5
            )
            
            if response.status_code in [200, 201]:
                print(f"✅ Order created: {side} {size} @ ${price:.2f}")
                return response.json()
            else:
                print(f"❌ Error creating order: {response.status_code}")
                print(f"   {response.text}")
                return None
        except Exception as e:
            print(f"Error: {e}")
            return None
    
    def get_orders(self):
        """Get user orders"""
        try:
            path = "/data/orders"
            headers = self._generate_auth_headers("GET", path)
            response = requests.get(f"{self.base_url}{path}", headers=headers, timeout=5)
            
            if response.status_code == 200:
                return response.json()
            else:
                print(f"Error getting orders: {response.status_code}")
                return []
        except Exception as e:
            print(f"Error: {e}")
            return []


class TradingBot:
    def __init__(self, api_key, secret, passphrase, address=None, risk_per_trade=0.05, dry_run=False):
        self.api = PolymarketTraderAPI(api_key, secret, passphrase, address)
        self.risk_per_trade = risk_per_trade
        self.dry_run = dry_run
        
        self.prices = deque(maxlen=50)
        self.timestamps = deque(maxlen=50)
        self.current_candle = None
        self.current_candle_start = None
        self.signals = deque(maxlen=50)
        self.trades = deque(maxlen=100)
        
        self.balance = 0
        self.btc_market_id = None
        self.btc_token_id = None
        self.btc_market_question = None
        self.running = True
    
    @staticmethod
    def _parse_token_ids(market):
        raw = market.get('clobTokenIds') or []
        if isinstance(raw, str):
            return json.loads(raw)
        return raw
    
    def initialize(self):
        """Initialize bot"""
        print("🤖 Initializing Trading Bot v2...")
        
        # Get user info
        user = self.api.get_user()
        if user:
            self.balance = float(user.get('balance', user.get('available', 0)) or 0)
            print(f"✅ Account Balance: ${self.balance:.2f}")
        else:
            print("⚠️  Could not fetch balance (will use dry-run for signals)")
            self.balance = 0
        
        # Find Bitcoin markets
        print("\n🔍 Searching for Bitcoin price prediction markets...")
        markets = self.api.get_markets()
        
        if markets:
            # Filter for actual Bitcoin price prediction markets
            btc_markets = []
            for market in markets:
                question = market.get('question', '').lower()
                # Look for BTC price, Bitcoin price, will Bitcoin
                if any(term in question for term in ['bitcoin', 'btc', 'will btc', '$btc', 'price of bitcoin']):
                    if market.get('active'):
                        btc_markets.append(market)
            
            if btc_markets:
                print(f"✅ Found {len(btc_markets)} Bitcoin prediction markets:")
                for i, market in enumerate(btc_markets[:5], 1):
                    q = market.get('question')[:60]
                    print(f"  {i}. {q}...")
                    print(f"     ID: {market.get('id')}")
                
                self.btc_market_id = btc_markets[0].get('id')
                tokens = self._parse_token_ids(btc_markets[0])
                self.btc_token_id = tokens[0] if tokens else None
                self.btc_market_question = btc_markets[0].get('question', '')[:60]
                print(f"\n✅ Using market ID: {self.btc_market_id}")
                if self.btc_token_id:
                    print(f"   Token ID: {self.btc_token_id[:20]}...")
            else:
                print("⚠️  No Bitcoin markets found, using first active market")
                active = [m for m in markets if m.get('active')]
                if active:
                    self.btc_market_id = active[0].get('id')
                    tokens = self._parse_token_ids(active[0])
                    self.btc_token_id = tokens[0] if tokens else None
                    self.btc_market_question = active[0].get('question', '')[:60]
                    print(f"   Market: {self.btc_market_question}")
                else:
                    print("❌ No active markets found")
                    return False
        else:
            print("❌ Could not fetch markets")
            return False
        
        return True
    
    def calculate_position_size(self):
        """Calculate position size"""
        max_risk = self.balance * self.risk_per_trade
        position_size = max(min(int(max_risk / 0.50), 100), 1)
        return position_size
    
    def add_price(self, price, timestamp):
        """Add price point"""
        now = datetime.fromtimestamp(timestamp)
        
        if self.current_candle_start is None:
            self.current_candle_start = now
            self.current_candle = {
                'open': price,
                'high': price,
                'low': price,
                'close': price,
                'volume': 1
            }
        else:
            candle_age = (now - self.current_candle_start).total_seconds() / 60
            if candle_age >= 15:
                self.prices.append(self.current_candle['close'])
                self.timestamps.append(self.current_candle_start)
                
                self.current_candle = {
                    'open': price,
                    'high': price,
                    'low': price,
                    'close': price,
                    'volume': 1
                }
                self.current_candle_start = now
                self.analyze_and_trade()
            else:
                self.current_candle['high'] = max(self.current_candle['high'], price)
                self.current_candle['low'] = min(self.current_candle['low'], price)
                self.current_candle['close'] = price
                self.current_candle['volume'] += 1
    
    def calculate_rsi(self, period=14):
        """Calculate RSI"""
        if len(self.prices) < period + 1:
            return None
        
        prices = list(self.prices)
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        
        seed = deltas[:period]
        up = sum([d for d in seed if d > 0]) / period
        down = -sum([d for d in seed if d < 0]) / period
        
        rs = up / down if down != 0 else 0
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def calculate_macd(self):
        """Calculate MACD"""
        if len(self.prices) < 26:
            return None, None, None
        
        prices = list(self.prices)
        
        ema12 = self._ema(prices, 12)
        ema26 = self._ema(prices, 26)
        
        if ema12 is None or ema26 is None:
            return None, None, None
        
        macd_line = ema12 - ema26
        
        macd_history = [self._calculate_ema_point(prices[:i], 12) - self._calculate_ema_point(prices[:i], 26) 
                       for i in range(26, len(prices))]
        signal_line = self._ema(macd_history, 9) if len(macd_history) >= 9 else None
        
        histogram = macd_line - signal_line if signal_line else None
        
        return macd_line, signal_line, histogram
    
    def _ema(self, prices, period):
        if len(prices) < period:
            return None
        return self._calculate_ema_point(prices, period)
    
    def _calculate_ema_point(self, prices, period):
        if len(prices) < period:
            return None
        sma = sum(prices[-period:]) / period
        ema = sma
        multiplier = 2 / (period + 1)
        
        for price in prices[-period:]:
            ema = price * multiplier + ema * (1 - multiplier)
        
        return ema
    
    def analyze_and_trade(self):
        """Analyze and execute trades"""
        if len(self.prices) < 15:
            return
        
        rsi = self.calculate_rsi()
        macd_line, signal_line, histogram = self.calculate_macd()
        
        current_price = list(self.prices)[-1]
        signal = None
        confidence = 0
        
        # RSI signals
        if rsi is not None:
            if rsi < 30:
                signal = "UP"
                confidence += 25
            elif rsi > 70:
                signal = "DOWN"
                confidence += 25
        
        # MACD signals
        if macd_line is not None and signal_line is not None and histogram is not None:
            if histogram > 0 and macd_line > signal_line:
                if signal == "UP":
                    confidence += 25
                elif signal is None:
                    signal = "UP"
                    confidence = 25
            elif histogram < 0 and macd_line < signal_line:
                if signal == "DOWN":
                    confidence += 25
                elif signal is None:
                    signal = "DOWN"
                    confidence = 25
        
        if signal and confidence >= 50:
            self.execute_trade(signal, confidence, current_price)
    
    def execute_trade(self, signal, confidence, price):
        """Execute trade (or log in dry-run mode)"""
        if not self.btc_token_id:
            return
        if not self.dry_run and self.balance <= 0:
            return
        
        position_size = self.calculate_position_size() if self.balance > 0 else 10
        
        if signal == "UP":
            bet_price = 0.55 + (confidence / 100) * 0.30
            side = "BUY"
        else:
            bet_price = 0.45 - (confidence / 100) * 0.30
            side = "BUY"
        
        bet_price = min(max(bet_price, 0.01), 0.99)
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        
        print(f"\n{'='*60}")
        print(f"🎯 [{mode}] TRADE SIGNAL: {signal} (Confidence: {confidence}%)")
        print(f"Market: {self.btc_market_question or self.btc_market_id}")
        print(f"Position: {position_size} shares @ ${bet_price:.2f}")
        print(f"BTC Price: ${price:.2f}")
        if self.dry_run:
            print("⚠️  No order placed (dry-run mode)")
        print(f"{'='*60}\n")
        
        trade_record = {
            'timestamp': datetime.now(),
            'signal': signal,
            'confidence': confidence,
            'size': position_size,
            'price': bet_price,
            'dry_run': self.dry_run,
        }
        
        if self.dry_run:
            self.trades.append(trade_record)
            return
        
        order = self.api.create_order(
            token_id=self.btc_token_id,
            side=side,
            size=position_size,
            price=bet_price
        )
        
        if order:
            self.trades.append(trade_record)


class BinanceWebSocketClient:
    def __init__(self, bot):
        self.bot = bot
        self.ws = None
        self.running = True
    
    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            if 'k' in data:
                kline = data['k']
                price = float(kline['c'])
                timestamp = kline['T'] / 1000
                
                self.bot.add_price(price, timestamp)
                
                if int(self.bot.current_candle['volume']) % 5 == 0:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] BTC: ${price:.2f}")
        except Exception as e:
            print(f"Error: {e}")
    
    def on_error(self, ws, error):
        print(f"WebSocket error: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        print("WebSocket closed")
        self.running = False
    
    def on_open(self, ws):
        print("WebSocket connected")
        ws.send(json.dumps({
            "method": "SUBSCRIBE",
            "params": ["btcusdt@kline_1m"],
            "id": 1
        }))
    
    def start(self):
        ws_url = "wss://stream.binance.com:9443/ws"
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        print("Starting Binance WebSocket...")
        # Skip SSL verification for macOS certificate issues
        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        self.ws.run_forever(ping_interval=30, ping_timeout=10, sslopt={"cert_reqs": ssl.CERT_NONE})


def test_binance_connection(timeout=12):
    """Verify Binance WebSocket delivers BTC prices."""
    import ssl
    prices = []
    connected = threading.Event()
    done = threading.Event()

    def on_message(ws, message):
        data = json.loads(message)
        if 'k' in data:
            prices.append(float(data['k']['c']))
            if len(prices) >= 2:
                done.set()
                ws.close()

    def on_open(ws):
        connected.set()
        ws.send(json.dumps({
            "method": "SUBSCRIBE",
            "params": ["btcusdt@kline_1m"],
            "id": 1
        }))

    ws = websocket.WebSocketApp(
        "wss://stream.binance.com:9443/ws",
        on_message=on_message,
        on_open=on_open,
    )
    thread = threading.Thread(
        target=lambda: ws.run_forever(
            ping_interval=30,
            ping_timeout=10,
            sslopt={"cert_reqs": ssl.CERT_NONE},
        ),
        daemon=True,
    )
    thread.start()

    if not connected.wait(timeout):
        ws.close()
        return False, "connection timeout"

    if not done.wait(timeout):
        ws.close()
        if prices:
            return True, f"1 price received (${prices[0]:,.2f})"
        return False, "no price data received"

    return True, f"{len(prices)} prices, latest ${prices[-1]:,.2f}"


def test_technical_analysis():
    """Verify RSI/MACD and signal logic with synthetic data."""
    bot = TradingBot("test", "test", "test", dry_run=True)
    bot.btc_token_id = "test-token"
    bot.btc_market_question = "Test BTC market"

    base = 100000
    for i in range(20):
        bot.prices.append(base - i * 800)

    rsi = bot.calculate_rsi()
    if rsi is None or not (0 <= rsi <= 100):
        return False, "RSI failed"

    for i in range(10):
        bot.prices.append(base - 16000 + i * 1200)

    macd_line, signal_line, histogram = bot.calculate_macd()
    if macd_line is None:
        return False, "MACD failed"

    bot.analyze_and_trade()
    signals_fired = len(bot.trades)

    return True, f"RSI={rsi:.1f}, MACD={macd_line:.2f}, signals={signals_fired}"


def run_verification(api_key, secret, passphrase, address=None):
    """Run all checks without placing real orders."""
    core_results = []
    trading_results = []

    def check(results, name, passed, detail=""):
        status = "✅ PASS" if passed else "❌ FAIL"
        line = f"{status}  {name}"
        if detail:
            line += f" — {detail}"
        print(line)
        results.append(passed)
        return passed

    print("🔍 Polymarket Bot Verification (no real trades)")
    print("=" * 60)
    print("\nCore bot functionality:")
    print("-" * 60)

    check(core_results, "API credentials in .env", all([api_key, secret, passphrase]))

    api = PolymarketTraderAPI(api_key, secret, passphrase, address)
    markets = api.get_markets()
    btc_markets = [
        m for m in markets
        if any(t in m.get('question', '').lower() for t in ['bitcoin', 'btc'])
        and m.get('active')
    ]
    check(
        core_results,
        "Polymarket markets API",
        len(markets) > 0,
        f"{len(markets)} markets, {len(btc_markets)} active BTC markets",
    )

    ws_ok, ws_detail = test_binance_connection()
    check(core_results, "Binance WebSocket (BTC price stream)", ws_ok, ws_detail)

    ta_ok, ta_detail = test_technical_analysis()
    check(core_results, "Technical analysis (RSI/MACD/signals)", ta_ok, ta_detail)

    if btc_markets:
        tokens = TradingBot._parse_token_ids(btc_markets[0])
        token_id = tokens[0] if tokens else None
        orderbook = api.get_orderbook(token_id) if token_id else None
        ob_ok = orderbook is not None and 'bids' in orderbook
        check(core_results, "Orderbook fetch", ob_ok, btc_markets[0].get('question', '')[:50])
    else:
        check(core_results, "Orderbook fetch", False, "no BTC market to test")

    print("\nTrading API (needed only for live orders):")
    print("-" * 60)

    if address:
        auth_ok, auth_detail = api.test_auth()
        check(trading_results, "Polymarket API authentication", auth_ok, auth_detail)
    else:
        print("⚠️  SKIP  Polymarket API authentication — add POLYMARKET_ADDRESS to .env")
        print("   (your Polygon wallet address from polymarket.com/settings)")
        trading_results.append(False)

    core_passed = sum(core_results)
    core_total = len(core_results)
    print(f"\n{'='*60}")
    print(f"Core bot: {core_passed}/{core_total} checks passed")

    if core_passed == core_total:
        print("\n✅ Bot logic works — Binance, analysis, and market data are OK.")
        print("   Run: python polymarket_bot_v2.py --dry-run")
    else:
        print("\n⚠️  Core checks failed — fix before using the bot.")

    if address and trading_results and trading_results[0]:
        print("✅ Trading API credentials are valid.")
    elif not address:
        print("ℹ️  Add POLYMARKET_ADDRESS to .env to verify trading credentials.")
    else:
        print("⚠️  Trading credentials not accepted — check API keys in Polymarket settings.")

    return core_passed == core_total


def main():
    parser = argparse.ArgumentParser(description="Polymarket BTC Trading Bot")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run diagnostics without trading (recommended first step)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Monitor BTC and log trade signals without placing orders",
    )
    args = parser.parse_args()

    api_key = os.getenv("POLYMARKET_API_KEY")
    secret = os.getenv("POLYMARKET_SECRET")
    passphrase = os.getenv("POLYMARKET_PASSPHRASE")
    address = os.getenv("POLYMARKET_ADDRESS")

    if not all([api_key, secret, passphrase]):
        print("❌ Missing API credentials in .env")
        print("   Required: POLYMARKET_API_KEY, POLYMARKET_SECRET, POLYMARKET_PASSPHRASE")
        sys.exit(1)

    if args.verify:
        sys.exit(0 if run_verification(api_key, secret, passphrase, address) else 1)

    print("🚀 Polymarket Trading Bot v2")
    print("=" * 60)

    dry_run = args.dry_run or os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
    if dry_run:
        print("📋 DRY-RUN mode — signals will be logged, no real orders")
    else:
        print("💰 LIVE mode — real orders when balance > 0 and signal fires")

    bot = TradingBot(api_key, secret, passphrase, address, dry_run=dry_run)

    if not bot.initialize():
        print("❌ Initialization failed")
        sys.exit(1)

    if not dry_run and bot.balance <= 0:
        print("\n⚠️  Balance is $0 — bot will not place orders.")
        print("   Use --dry-run to test signals without depositing USDC.")

    print("\n✅ Bot ready! Press Ctrl+C to stop.")
    print("=" * 60)

    client = BinanceWebSocketClient(bot)
    ws_thread = threading.Thread(target=client.start, daemon=True)
    ws_thread.start()

    try:
        while True:
            time.sleep(60)
            mode = "dry-run" if dry_run else "live"
            print(
                f"\n📊 {datetime.now().strftime('%H:%M:%S')} | "
                f"Mode: {mode} | Balance: ${bot.balance:.2f} | "
                f"Signals: {len(bot.trades)} | Candles: {len(bot.prices)}"
            )
    except KeyboardInterrupt:
        print("\n\n👋 Shutting down...")
        client.running = False


if __name__ == "__main__":
    main()
