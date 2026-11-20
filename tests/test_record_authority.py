"""Focused owner tests for shared-record authority selection and its runtime seam."""
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from mizu import operator_records, queue_record_delivery, record_authority, runtime


class RecordAuthorityOwnerTests(unittest.TestCase):
    def test_marker_selects_queues_only_when_queues(self):
        project = types.SimpleNamespace(insights=types.SimpleNamespace(root=Path('/unused')))
        for kind, expected in (("queues", queue_record_delivery),
                               ("shared", operator_records),
                               (None, operator_records)):
            with self.subTest(kind=kind):
                with patch.object(record_authority.queue_record_delivery,
                                  "authority_kind", return_value=kind) as marker:
                    self.assertIs(record_authority._record_delivery(project), expected)
                marker.assert_called_once_with(project.insights.root)

    def test_runtime_reexports_owner_by_identity(self):
        self.assertIs(runtime._record_delivery, record_authority._record_delivery)


if __name__ == '__main__':
    unittest.main()
