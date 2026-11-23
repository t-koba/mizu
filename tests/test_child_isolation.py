"""Conflict reporting for isolated child workspaces, without launching children."""
import unittest

from mizu.child_isolation import report_conflicts
from mizu.errors import Denied, LimitExceeded

A = 'a' * 64
B = 'b' * 64
C = 'c' * 64


class ChildIsolationTests(unittest.TestCase):
    def test_one_sided_changes_are_not_conflicts(self):
        base = {'a.txt': A, 'gone.txt': B}
        parent = {'a.txt': B}
        child = {'a.txt': A, 'gone.txt': B, 'new.txt': C}
        self.assertEqual(report_conflicts(base, parent, child), [])

    def test_identical_convergent_edit_is_not_a_conflict(self):
        self.assertEqual(report_conflicts({'a': A}, {'a': B}, {'a': B}), [])

    def test_overlapping_edits_are_explicit_and_sorted(self):
        base = {'z': A, 'a': A, 'same': A}
        parent = {'z': B, 'a': B, 'same': C}
        child = {'z': C, 'a': C, 'same': C}
        self.assertEqual(report_conflicts(base, parent, child), [
            {'path': 'a', 'base': A, 'parent': B, 'child': C,
             'kind': 'both-modified'},
            {'path': 'z', 'base': A, 'parent': B, 'child': C,
             'kind': 'both-modified'},
        ])
        self.assertEqual(
            report_conflicts({'z': A, 'a': A}, {'z': B, 'a': A},
                             {'z': C, 'a': B}),
            [{'path': 'z', 'base': A, 'parent': B, 'child': C,
              'kind': 'both-modified'}])

    def test_modify_delete_and_add_add_conflicts_are_typed(self):
        self.assertEqual(
            report_conflicts({'a': A}, {'a': None}, {'a': B})[0]['kind'],
            'modify-delete')
        self.assertEqual(
            report_conflicts({}, {'a': A}, {'a': B})[0]['kind'],
            'both-added-differently')

    def test_missing_and_malformed_inputs_fail_closed(self):
        for base, parent, child in (
            (None, {}, {}), ({}, [], {}), ({}, {}, {'../x': A}),
            ({}, {}, {'x': 'ZZ'}), ({'x': A}, {'x': 1}, {}),
        ):
            with self.subTest(base=base, parent=parent, child=child):
                with self.assertRaises(Denied):
                    report_conflicts(base, parent, child)

    def test_inventory_is_bounded_without_scanning_all_stored_data(self):
        with self.assertRaisesRegex(Denied, "oversized parent inventory"):
            report_conflicts({}, {str(n): A for n in range(10001)}, {})
        with self.assertRaises(LimitExceeded):
            report_conflicts({str(n): A for n in range(10000)},
                             {str(n): A for n in range(10000, 20000)}, {})


if __name__ == '__main__':
    unittest.main()
