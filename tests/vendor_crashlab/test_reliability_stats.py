from __future__ import annotations

import unittest

from recon_lab.vendor.crashlab import reliability_stats


class SharedWilsonModuleTests(unittest.TestCase):
    def test_known_interval_2_of_20(self) -> None:
        # 2 of 20 -> 2.8%-30.1%.
        low, high = reliability_stats.wilson_interval(2, 20)
        self.assertEqual(round(low, 3), 0.028)
        self.assertEqual(round(high, 3), 0.301)


if __name__ == "__main__":
    unittest.main()
