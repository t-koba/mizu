import unittest
from summary import summarize


class SummaryTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(summarize([]), {'count': 0, 'sum': 0, 'mean': 0.0})

    def test_regular(self):
        self.assertEqual(summarize([1, 2, 3]), {'count': 3, 'sum': 6, 'mean': 2})

    def test_negative(self):
        self.assertEqual(summarize([-1, 1])['mean'], 0)
