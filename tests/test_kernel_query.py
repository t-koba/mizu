"""Declarative queries over typed relations: grammar, bounds and row visibility."""
import copy
import datetime as dt
import json
import unittest

import support  # noqa: F401 -- puts src on sys.path for the standalone unittest run

from mizu.errors import Denied, LimitExceeded
from mizu.kernel.query import (
    MAX_ARGS, MAX_DEPTH, MAX_IN_VALUES, MAX_PATTERN_CHARS, MAX_REGEX_INPUT_CHARS,
    MAX_VALUE_CHARS, Relation, evaluate,
)

UTC = dt.timezone.utc


def at(hour):
    return dt.datetime(2026, 1, 1, hour, tzinfo=UTC)


# Schemas and field names belong to the caller, not to the kernel.
M = Relation("m", {"id": "str", "queue": "str", "seq": "int", "author": "str",
                   "at": "datetime", "deliver_at": "datetime", "body_kind": "str"}, key="id")
R = Relation("r", {"from_id": "str", "to_id": "str", "ref_author": "str"}, key="from_id")
D = Relation("d", {"msg_id": "str", "principal": "str", "unit": "str", "at": "datetime"},
             key="msg_id")
SCHEMAS = {"m": M, "r": R, "d": D}

MESSAGES = [
    {"id": "m1", "queue": "Q", "seq": 1, "author": "alice", "at": at(1),
     "deliver_at": at(2), "body_kind": "alpha-1"},
    {"id": "m2", "queue": "Q", "seq": 2, "author": "bob", "at": at(2),
     "deliver_at": at(3), "body_kind": "beta-1"},
    {"id": "m3", "queue": "Q", "seq": 3, "author": "carol", "at": at(3),
     "deliver_at": at(4), "body_kind": "alpha-2"},
    {"id": "m4", "queue": "OTHER", "seq": 4, "author": "dave", "at": at(4),
     "deliver_at": at(5), "body_kind": "beta-2"},
    {"id": "m5", "queue": "Q", "seq": 5, "author": "erin", "at": at(5),
     "deliver_at": at(6), "body_kind": "alpha-3"},
]
REFS = [
    {"from_id": "m2", "to_id": "m1", "ref_author": "alice"},
    {"from_id": "m3", "to_id": "m1", "ref_author": "bob"},
    {"from_id": "m4", "to_id": "m3", "ref_author": "alice"},
    {"from_id": "m5", "to_id": "m2", "ref_author": "carol"},
]
FACTS = [
    {"msg_id": "m2", "principal": "p", "unit": "u2", "at": at(6)},
    {"msg_id": "m1", "principal": "p", "unit": "u1", "at": at(5)},
    {"msg_id": "m3", "principal": "q", "unit": "u3", "at": at(4)},
]


def all_fields(name, row):
    return dict(row)


class KernelQueryTests(unittest.TestCase):
    def setUp(self):
        self.relations = {"m": copy.deepcopy(MESSAGES), "r": copy.deepcopy(REFS),
                          "d": copy.deepcopy(FACTS)}

    def run_query(self, query, *, visible=all_fields, max_rows=1000, max_bytes=1 << 20,
                  cursor=0, relations=None, schemas=SCHEMAS):
        return evaluate(relations if relations is not None else self.relations, schemas,
                        query, visible=visible, max_rows=max_rows,
                        max_bytes=max_bytes, cursor=cursor)

    def base_ids(self, where, **kwargs):
        query = {"relation": "m", "where": where, "select": ["id"],
                 "order": [{"field": "seq", "desc": False}], "limit": 20}
        result = self.run_query(query, **kwargs)
        return [row["id"] for row in result["rows"]]

    def test_anti_join_excludes_authored_references_and_counts(self):
        where = {"op": "and", "args": [
            {"op": "eq", "field": "queue", "value": "Q"},
            {"op": "not_exists", "relation": "r", "on": {"id": "to_id"},
             "where": {"op": "eq", "field": "ref_author", "value": "alice"}},
        ]}
        result = self.run_query({"relation": "m", "where": where, "select": ["id"],
                                 "order": [{"field": "seq", "desc": False}],
                                 "limit": 20, "count": True})
        self.assertEqual(result["rows"], [{"id": "m2"}, {"id": "m5"}])
        self.assertEqual(result["count"], 2)
        self.assertFalse(result["more"])
        self.assertEqual(result["next"], 2)
        # Five base rows plus twelve peer rows: one, four, three, zero and four.
        self.assertEqual(result["examined"], 17)

    def test_field_predicate_and_order_select_earliest_rows(self):
        result = self.run_query({"relation": "d",
                                 "where": {"op": "eq", "field": "principal", "value": "p"},
                                 "order": [{"field": "at", "desc": False}],
                                 "select": ["msg_id", "at"], "limit": 10})
        self.assertEqual([row["msg_id"] for row in result["rows"]], ["m1", "m2"])
        self.assertEqual([row["at"] for row in result["rows"]], [at(5), at(6)])
        self.assertEqual(result["examined"], 3)
        self.assertIsNone(result["count"])

    def test_exists_probes_both_reference_directions(self):
        incoming = self.base_ids({"op": "exists", "relation": "r", "on": {"id": "to_id"}})
        outgoing = self.base_ids({"op": "exists", "relation": "r", "on": {"id": "from_id"}})
        self.assertEqual(incoming, ["m1", "m2", "m3"])
        self.assertEqual(outgoing, ["m2", "m3", "m4", "m5"])

    def test_regex_matches_a_declared_string_field(self):
        ids = self.base_ids({"op": "regex", "field": "body_kind", "pattern": "^alpha-[0-9]+$"})
        self.assertEqual(ids, ["m1", "m3", "m5"])

    def test_scalar_predicates_are_typed_and_comparable(self):
        cases = (
            ({"op": "lt", "field": "seq", "value": 3}, ["m1", "m2"]),
            ({"op": "le", "field": "seq", "value": 2}, ["m1", "m2"]),
            ({"op": "gt", "field": "seq", "value": 4}, ["m5"]),
            ({"op": "ge", "field": "seq", "value": 4}, ["m4", "m5"]),
            ({"op": "between", "field": "seq", "low": 2, "high": 4}, ["m2", "m3", "m4"]),
            ({"op": "in", "field": "id", "values": ["m5", "m1"]}, ["m1", "m5"]),
            ({"op": "not", "arg": {"op": "eq", "field": "queue", "value": "Q"}}, ["m4"]),
        )
        for predicate, expected in cases:
            with self.subTest(predicate=predicate):
                self.assertEqual(self.base_ids(predicate), expected)
        iso = self.base_ids({"op": "ge", "field": "at", "value": at(4).isoformat()})
        self.assertEqual(iso, ["m4", "m5"])

    def test_visibility_filters_rows_before_checks_and_counts(self):
        def visible(name, row):
            if name == "m" and row["id"] == "m2":
                return None
            allowed = dict(row)
            if name == "m":
                allowed.pop("author", None)
            return allowed

        query = {"relation": "m", "where": {"op": "eq", "field": "queue", "value": "Q"},
                 "select": ["id"], "order": [{"field": "seq", "desc": False}],
                 "limit": 20, "count": True}
        result = self.run_query(query, visible=visible)
        self.assertEqual([row["id"] for row in result["rows"]], ["m1", "m3", "m5"])
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["examined"], 5)

        with self.assertRaises(Denied):
            self.run_query({"relation": "m",
                            "where": {"op": "eq", "field": "author", "value": "alice"}},
                           visible=visible)
        with self.assertRaises(Denied):
            self.run_query({"relation": "m", "select": ["id", "author"]}, visible=visible)
        with self.assertRaises(Denied):
            self.run_query({"relation": "m", "order": [{"field": "author", "desc": False}]},
                           visible=visible)

    def test_hidden_peer_field_and_row_cannot_be_used(self):
        def no_author(name, row):
            if name == "r":
                return {key: value for key, value in row.items() if key != "ref_author"}
            return dict(row)

        with self.assertRaises(Denied):
            self.run_query({"relation": "m", "where": {
                "op": "exists", "relation": "r", "on": {"id": "to_id"},
                "where": {"op": "eq", "field": "ref_author", "value": "alice"}}},
                visible=no_author)

        def hide_one_ref(name, row):
            if name == "r" and row["from_id"] == "m2":
                return None
            return dict(row)

        ids = self.base_ids({"op": "not_exists", "relation": "r", "on": {"id": "to_id"},
                             "where": {"op": "eq", "field": "ref_author", "value": "alice"}},
                            visible=hide_one_ref)
        self.assertEqual(ids, ["m1", "m2", "m4", "m5"])

    def test_pages_are_disjoint_and_byte_bound_continues(self):
        query = {"relation": "m", "where": {"op": "eq", "field": "queue", "value": "Q"},
                 "select": ["id"], "order": [{"field": "seq", "desc": False}], "limit": 2}
        seen = []
        cursor = 0
        while True:
            page = self.run_query(query, cursor=cursor)
            self.assertLessEqual(len(page["rows"]), 2)
            seen.extend(row["id"] for row in page["rows"])
            self.assertEqual(page["examined"], 5)
            if not page["more"]:
                break
            self.assertEqual(page["next"], cursor + len(page["rows"]))
            cursor = page["next"]
        self.assertEqual(seen, ["m1", "m2", "m3", "m5"])
        self.assertEqual(len(set(seen)), 4)

        one_row = len(json.dumps({"id": "m1"}, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")))
        pages = []
        cursor = 0
        while True:
            page = self.run_query({**query, "limit": 10}, cursor=cursor, max_bytes=one_row)
            self.assertEqual(len(page["rows"]), 1)
            self.assertLessEqual(len(json.dumps(page["rows"][0], separators=(",", ":"))),
                                 one_row)
            pages.append(page["rows"][0]["id"])
            if not page["more"]:
                break
            cursor = page["next"]
        self.assertEqual(pages, ["m1", "m2", "m3", "m5"])
        with self.assertRaises(LimitExceeded):
            self.run_query({**query, "limit": 10}, max_bytes=one_row - 1)

    def test_row_bound_counts_peer_rows_and_fails_closed(self):
        query = {"relation": "m", "where": {
            "op": "and", "args": [
                {"op": "eq", "field": "queue", "value": "Q"},
                {"op": "not_exists", "relation": "r", "on": {"id": "to_id"},
                 "where": {"op": "eq", "field": "ref_author", "value": "alice"}},
            ]}, "select": ["id"], "order": [{"field": "seq", "desc": False}], "limit": 10}
        self.assertEqual(self.run_query(query, max_rows=17)["examined"], 17)
        with self.assertRaises(LimitExceeded):
            self.run_query(query, max_rows=16)

    def test_invalid_expressions_and_shapes_fail_closed(self):
        eq = {"op": "eq", "field": "seq", "value": 1}
        nested = eq
        for _ in range(MAX_DEPTH):
            nested = {"op": "not", "arg": nested}
        bad_queries = [
            [], {"relation": "m", "extra": 1}, {"select": ["id"]},
            {"relation": "nope"}, {"relation": "m", "where": {"op": "wat"}},
            {"relation": "m", "where": {"op": "eq", "field": "nope", "value": 1}},
            {"relation": "m", "where": {"op": "eq", "field": "seq", "value": "1"}},
            {"relation": "m", "where": {"op": "eq", "field": "author",
                                        "value": "x" * (MAX_VALUE_CHARS + 1)}},
            {"relation": "m", "where": {"op": "eq", "field": "id", "value": "x", "extra": 1}},
            {"relation": "m", "where": {"op": "between", "field": "seq",
                                        "low": 3, "high": 1}},
            {"relation": "m", "where": {"op": "in", "field": "id",
                                        "values": ["x"] * (MAX_IN_VALUES + 1)}},
            {"relation": "m", "where": {"op": "and", "args": [eq] * (MAX_ARGS + 1)}},
            {"relation": "m", "where": {"op": "or", "args": []}},
            {"relation": "m", "where": nested},
            {"relation": "m", "where": {"op": "regex", "field": "seq", "pattern": "x"}},
            {"relation": "m", "where": {"op": "regex", "field": "id",
                                        "pattern": "x" * (MAX_PATTERN_CHARS + 1)}},
            {"relation": "m", "where": {"op": "regex", "field": "id", "pattern": "["}},
            {"relation": "m", "where": {"op": "regex", "field": "id", "pattern": "(a+)+$"}},
            {"relation": "m", "where": {"op": "regex", "field": "id", "pattern": r"^(a)\1$"}},
            {"relation": "m", "where": {"op": "regex", "field": "id",
                                        "pattern": "a*a*a*b"}},
            {"relation": "m", "where": {"op": "exists", "relation": "nope",
                                        "on": {"id": "to_id"}}},
            {"relation": "m", "where": {"op": "exists", "relation": "r", "on": {}}},
            {"relation": "m", "where": {"op": "exists", "relation": "r",
                                        "on": {"id": "nope"}}},
            {"relation": "m", "where": {"op": "exists", "relation": "r",
                                        "on": {"id": "to_id"},
                                        "where": {"op": "eq", "field": "id", "value": "x"}}},
            {"relation": "m", "select": ["id", "id"]},
            {"relation": "m", "select": ["nope"]},
            {"relation": "m", "order": [{"field": "seq", "desc": "yes"}]},
            {"relation": "m", "order": [{"field": "nope", "desc": False}]},
            {"relation": "m", "limit": 0}, {"relation": "m", "count": "yes"},
        ]
        for query in bad_queries:
            with self.subTest(query=repr(query)[:100]):
                with self.assertRaises(Denied):
                    self.run_query(query)

    def test_bad_schemas_rows_and_call_bounds_fail_closed(self):
        makers = (
            lambda: Relation("bad name", {"id": "str"}),
            lambda: Relation("m", {"id": "float"}),
            lambda: Relation("m", {"id": "str"}, key="nope"),
            lambda: {"name": "m", "fields": {"id": "str"}},
        )
        for make in makers:
            with self.subTest(make=make):
                with self.assertRaises(Denied):
                    self.run_query({"relation": "m"}, schemas={"m": make()})
        with self.assertRaises(Denied):
            self.run_query({"relation": "d"}, schemas={"n": D})

        bad_rows = [
            [{"id": "x"}],
            [{**MESSAGES[0], "extra": 1}],
            [{**MESSAGES[0], "seq": "1"}],
            [{**MESSAGES[0], "author": True}],
            [{**MESSAGES[0], "author": "x" * (MAX_VALUE_CHARS + 1)}],
            [{**MESSAGES[0], "at": dt.datetime(2026, 1, 1)}],
        ]
        for rows in bad_rows:
            with self.subTest(row=repr(rows)[:100]):
                with self.assertRaises(Denied):
                    self.run_query({"relation": "m"}, relations={"m": rows})

        long_value = [{**MESSAGES[0], "id": "x" * (MAX_REGEX_INPUT_CHARS + 1)}]
        with self.assertRaises(Denied):
            self.run_query({"relation": "m", "where": {"op": "regex", "field": "id",
                                                       "pattern": "^x+$"}},
                           relations={"m": long_value})

        query = {"relation": "m", "select": ["id"], "limit": 1}
        for kwargs in ({"max_rows": 0}, {"max_bytes": 0}, {"cursor": -1},
                       {"visible": object()}, {"max_rows": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(Denied):
                    self.run_query(query, **kwargs)
        with self.assertRaises(Denied):
            self.run_query(query, cursor=6, relations={"m": MESSAGES[:2]})

    def test_evaluation_does_not_mutate_its_inputs(self):
        before = copy.deepcopy(self.relations)
        result = self.run_query({"relation": "m", "where": {
            "op": "exists", "relation": "r", "on": {"id": "to_id"}},
            "select": ["id", "at"], "order": [{"field": "at", "desc": True}],
            "limit": 2, "count": True})
        self.assertEqual(self.relations, before)
        self.assertEqual(SCHEMAS["m"].fields["id"], "str")
        self.assertEqual([row["id"] for row in result["rows"]], ["m3", "m2"])
        self.assertEqual(result["count"], 3)


if __name__ == "__main__":
    unittest.main()
