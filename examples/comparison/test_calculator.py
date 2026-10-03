import unittest
from calculator import mean


class Acceptance(unittest.TestCase):
    def test_fraction(self):self.assertEqual(mean([1,2]),1.5)
    def test_negative(self):self.assertEqual(mean([-2,-1]),-1.5)
    def test_empty(self):
        with self.assertRaises(ValueError):mean([])
    def test_integer(self):self.assertEqual(mean([2,4]),3)
