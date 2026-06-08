import unittest

from bot_core import Candle, reconstruct_windows


def make_candle(index, close):
    open_time = index * 60_000
    open_price = 100.0 if index % 5 == 0 else close
    return Candle(open_time, open_price, max(open_price, close), min(open_price, close), close, 1.0)


class FixedWindowTest(unittest.TestCase):
    def test_reconstruct_windows_returns_stats(self):
        candles = [make_candle(i, 100 + i * 0.1) for i in range(60)]

        windows, stats = reconstruct_windows(
            candles,
            window_min=5,
            warmup=5,
            limit=20,
            strategy="follow_lead",
            entry_min=1,
            min_move_pct=0.01,
        )

        self.assertGreater(len(windows), 0)
        self.assertIn("predicted", stats)
        self.assertIn("win_rate", stats)
        self.assertIn("pnl_units", stats)


if __name__ == "__main__":
    unittest.main()
