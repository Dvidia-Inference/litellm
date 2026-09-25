import unittest

from pricing import cost, liberated


class PricingTest(unittest.TestCase):
    def test_marks(self):
        self.assertTrue(liberated("qwen3.6-35b-a3b-abliterated"))
        self.assertTrue(liberated("Qwen3.8-27B-Heretic"))
        self.assertTrue(liberated("qwen3.8-27b-uncensored"))
        self.assertFalse(liberated("qwen3.6"))

    def test_dollars(self):
        self.assertEqual(cost("qwen3.6", 1_000_000, 0), 1)
        self.assertEqual(cost("qwen3.6", 500_000, 500_000), 1)
        self.assertEqual(cost("gemma-abliterated", 1_000_000, 0), 4)
        self.assertEqual(cost("x", 0, 0), 0)


if __name__ == "__main__":
    unittest.main()
