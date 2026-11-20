"""The shipped work_items_read closed-page mode is described in operator docs.

The third read mode (closed=true over done/dropped items with filters) is a
public capability; the capability table, architecture and operations docs must
describe it and its unlocked, non-recovering read behavior.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


class ClosedPagingDocsTests(unittest.TestCase):
    def test_configuration_capability_row_describes_closed_mode(self):
        row = next(line for line in (DOCS / "configuration.md").read_text(encoding="utf-8").splitlines()
                   if line.startswith("| `work_items_read` |"))
        for token in ("closed=true", "closed_after", "closed_before", "origin"):
            self.assertIn(token, row)

    def test_architecture_lists_three_page_modes(self):
        text = (DOCS / "architecture.md").read_text(encoding="utf-8")
        self.assertIn("the runtime exposes the three\npage modes", text)
        self.assertIn("`page_closed(cursor=None, limit=25", text)

    def test_architecture_separates_unlocked_page_closed_from_locked_readers(self):
        text = (DOCS / "architecture.md").read_text(encoding="utf-8")
        self.assertIn("the locked readers\n`page_open(cursor=None, limit=25)`, `projection(item_id)` and\n"
                      "`explain(item_id, *, after_rev=0, limit=25)`, the unlocked, non-recovering\n"
                      "read-only `page_closed(cursor=None, limit=25", text)

    def test_architecture_details_the_closed_page_reader(self):
        text = (DOCS / "architecture.md").read_text(encoding="utf-8")
        self.assertIn("selects a third mode with `closed=true`", text)
        self.assertIn("unlocked, non-recovering store view", text)
        self.assertIn("never cap `total`", text)

    def test_operations_qualifies_locked_page_open_claim(self):
        text = (DOCS / "operations.md").read_text(encoding="utf-8")
        self.assertIn("the open-page and by-ID modes of `work_items_read`", text)
        self.assertIn("the unlocked, non-recovering `page_closed` reader", text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
