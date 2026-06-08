import unittest

from bot_core import estimate_entry_odds, odds_payout_multiplier


class OddsModelTest(unittest.TestCase):
    def test_entry_odds_are_bounded(self):
        price = estimate_entry_odds(
            abs_move_pct=0.05,
            minutes_left=3,
            volatility_pct=0.03,
            predicted_is_leader=True,
        )

        self.assertGreaterEqual(price, 0.50)
        self.assertLessEqual(price, 0.97)

    def test_payout_multiplier_is_inverse_price(self):
        self.assertAlmostEqual(odds_payout_multiplier(0.50), 2.0)
        self.assertAlmostEqual(odds_payout_multiplier(0.80), 1.25)


if __name__ == "__main__":
    unittest.main()
