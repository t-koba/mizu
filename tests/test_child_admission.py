"""Pure child-admission decisions: bounded, ordered and explicitly refused."""
import unittest

from mizu.child_admission import MAX_BUDGET, MAX_CHILD_BATCH, admit_children
from mizu.errors import Denied, LimitExceeded


DECIDE = dict(max_slots=4, active_slots=1,
              request_budget=10, used_requests=2,
              tool_budget=20, used_tools=4)


class ChildAdmissionTests(unittest.TestCase):
    def decide(self, costs, **changes):
        return admit_children(costs=costs, **(DECIDE | changes))

    def test_admits_leading_prefix_and_reports_remaining_capacity(self):
        result = self.decide([(3, 5), (4, 6), (2, 1)])
        self.assertEqual(result['admitted'], [0, 1])
        self.assertEqual(result['refused'], [
            {'index': 2, 'reasons': ['request-budget']},
        ])
        self.assertEqual(result['reserved'], {'slots': 2, 'requests': 7, 'tools': 11})
        self.assertEqual(result['remaining'], {'slots': 1, 'requests': 1, 'tools': 5})
        self.assertTrue(result['consistent'])

    def test_slot_exhaustion_is_explicit_and_blocks_later_children(self):
        result = self.decide([(1, 1), (1, 1), (0, 0)], max_slots=2)
        self.assertEqual(result['admitted'], [0])
        self.assertEqual(result['refused'], [
            {'index': 1, 'reasons': ['slot-limit']},
            {'index': 2, 'reasons': ['earlier-child-refused'], 'blocked_by': 1},
        ])
        self.assertEqual(result['reserved'], {'slots': 1, 'requests': 1, 'tools': 1})

    def test_all_binding_limits_are_reported_in_fixed_order(self):
        result = self.decide([(9, 17)], max_slots=1, request_budget=8, tool_budget=16)
        self.assertEqual(result['admitted'], [])
        self.assertEqual(result['refused'][0]['reasons'],
                         ['slot-limit', 'request-budget', 'tool-budget'])
        self.assertEqual(result['reserved'], {'slots': 0, 'requests': 0, 'tools': 0})

    def test_inconsistent_state_admits_nothing_without_raising(self):
        result = self.decide([(0, 0), (1, 1)], used_requests=11)
        self.assertFalse(result['consistent'])
        self.assertEqual(result['admitted'], [])
        self.assertEqual(result['refused'], [
            {'index': 0, 'reasons': ['state-inconsistent']},
            {'index': 1, 'reasons': ['state-inconsistent']},
        ])
        self.assertEqual(result['remaining'], {'slots': 0, 'requests': 0, 'tools': 0})

    def test_empty_batch_is_a_clean_zero_decision(self):
        result = self.decide([])
        self.assertEqual(result, {
            'admitted': [], 'refused': [],
            'reserved': {'slots': 0, 'requests': 0, 'tools': 0},
            'remaining': {'slots': 3, 'requests': 8, 'tools': 16},
            'consistent': True,
        })

    def test_zero_budgets_and_zero_cost_children_are_valid(self):
        result = self.decide([(0, 0)], max_slots=1, active_slots=0,
                             request_budget=0, used_requests=0,
                             tool_budget=0, used_tools=0)
        self.assertEqual(result['admitted'], [0])
        self.assertEqual(result['reserved'], {'slots': 1, 'requests': 0, 'tools': 0})

    def test_malformed_counts_and_costs_fail_closed(self):
        bad_changes = [
            {'max_slots': True}, {'active_slots': -1},
            {'request_budget': 1.5}, {'used_tools': MAX_BUDGET + 1},
            {'tool_budget': '20'},
        ]
        for changes in bad_changes:
            with self.subTest(changes=changes), self.assertRaises(Denied):
                self.decide([], **changes)
        for costs in ({'requests': 1}, [(1,)], [1, 2], [(1, 2, 3)], [(-1, 0)],
                      [(0, False)]):
            with self.subTest(costs=costs), self.assertRaises(Denied):
                self.decide(costs)

    def test_oversized_batch_refuses_before_deciding(self):
        with self.assertRaises(LimitExceeded):
            self.decide([(0, 0)] * (MAX_CHILD_BATCH + 1))


if __name__ == '__main__':
    unittest.main()
