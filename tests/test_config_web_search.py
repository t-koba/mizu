"""Configured RSS/Atom per-feed search shaping policy."""
from support import Fixture
from mizu.config import load
from mizu.errors import ConfigError
from mizu.web import SEARCH_RESULT_LIMIT


class WebSearchConfigTests(Fixture):
    def test_default_and_range(self):
        self.assertEqual(self.config.web["search_feed_item_limit"], 200)
        original = self.file.read_text()
        without_setting = original.replace(
            "search_feed_item_limit = 200     # Per-feed RSS/Atom candidates retained (1-1000).\n", "", 1)
        self.assertNotEqual(without_setting, original)
        self.file.write_text(without_setting)
        self.assertEqual(load(self.file).web["search_feed_item_limit"], 200)
        self.file.write_text(original)

        for value in (1, 200, 201, SEARCH_RESULT_LIMIT):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    "search_feed_item_limit = 200", f"search_feed_item_limit = {value}", 1))
                self.assertEqual(load(self.file).web["search_feed_item_limit"], value)
        for value in (0, -1, SEARCH_RESULT_LIMIT + 1, "true", "1.5", '"1"', "[]", "{}"):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    "search_feed_item_limit = 200", f"search_feed_item_limit = {value}", 1))
                with self.assertRaisesRegex(ConfigError, "web.search_feed_item_limit"):
                    load(self.file)
        self.file.write_text(original)


if __name__ == "__main__":
    import unittest
    unittest.main()
