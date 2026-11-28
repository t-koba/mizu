"""Declarative queries over typed relations: grammar, bounds and row visibility."""
import copy
import datetime as dt
import json
import unittest

import support  # noqa: F401 -- puts src on sys.path for the standalone unittest run

from mizu.errors import Denied, LimitExceeded
from mizu.kernel.query import (
    MAX_ARGS, MAX_DEPTH, MAX_IN_VALUES, MAX_PATTERN_CHARS,
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
        self.assertEqual(set(result), {"rows", "next", "more", "count"})

    def test_field_predicate_and_order_select_earliest_rows(self):
        result = self.run_query({"relation": "d",
                                 "where": {"op": "eq", "field": "principal", "value": "p"},
                                 "order": [{"field": "at", "desc": False}],
                                 "select": ["msg_id", "at"], "limit": 10})
        self.assertEqual([row["msg_id"] for row in result["rows"]], ["m1", "m2"])
        self.assertEqual([row["at"] for row in result["rows"]], [at(5), at(6)])
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
            self.assertEqual(set(page), {"rows", "next", "more", "count"})
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
        within = self.run_query(query, max_rows=17)
        self.assertEqual([row["id"] for row in within["rows"]], ["m2", "m5"])
        self.assertIsNone(within["count"])
        with self.assertRaises(LimitExceeded):
            self.run_query(query, max_rows=16)

    def test_denied_rows_do_not_change_page_or_consume_the_row_bound(self):
        schema = Relation("v", {"id": "str", "seq": "int"}, key="id")
        visible_rows = [{"id": f"k{i}", "seq": i} for i in range(1, 4)]
        denied_rows = [{"id": f"x{i}", "seq": 100 + i} for i in range(1, 6)]
        query = {"relation": "v", "where": {"op": "le", "field": "seq", "value": 3},
                 "select": ["id"], "order": [{"field": "seq", "desc": False}],
                 "limit": 2, "count": True}

        def visible(name, row):
            return dict(row) if row["id"].startswith("k") else None

        expected = None
        for extra in (0, 1, 3, len(denied_rows)):
            with self.subTest(denied_rows=extra):
                result = self.run_query(
                    query, relations={"v": visible_rows + denied_rows[:extra]},
                    schemas={"v": schema}, visible=visible, max_rows=3)
                self.assertEqual(set(result), {"rows", "next", "more", "count"})
                self.assertNotIn("examined", result)
                self.assertEqual(result["rows"], [{"id": "k1"}, {"id": "k2"}])
                self.assertEqual(result["next"], 2)
                self.assertTrue(result["more"])
                self.assertEqual(result["count"], 3)
                outcome = ("ok", result["rows"], result["next"], result["more"],
                           result["count"])
                expected = outcome if expected is None else expected
                self.assertEqual(outcome, expected)

        peer_schema = Relation("p", {"ref": "str"}, key="ref")
        hidden_peers = [{"ref": f"x{i}"} for i in range(1, 4)]
        join = {"relation": "v", "where": {
            "op": "not_exists", "relation": "p", "on": {"id": "ref"}},
                "select": ["id"], "order": [{"field": "seq", "desc": False}],
                "limit": 2, "count": True}

        def visible_peers(name, row):
            return dict(row) if name == "v" or not row["ref"].startswith("x") else None

        expected = None
        for extra in (0, 1, len(hidden_peers)):
            with self.subTest(denied_peers=extra):
                # Two visible base rows consume the bound fully; denied peers
                # used to push it over and raise even though none are readable.
                result = self.run_query(
                    join, relations={"v": visible_rows[:2], "p": hidden_peers[:extra]},
                    schemas={"v": schema, "p": peer_schema}, visible=visible_peers,
                    max_rows=2)
                self.assertEqual(set(result), {"rows", "next", "more", "count"})
                self.assertNotIn("examined", result)
                self.assertEqual(result["rows"], [{"id": "k1"}, {"id": "k2"}])
                self.assertEqual(result["next"], 2)
                self.assertFalse(result["more"])
                self.assertEqual(result["count"], 2)
                outcome = ("ok", result["rows"], result["next"], result["more"],
                           result["count"])
                expected = outcome if expected is None else expected
                self.assertEqual(outcome, expected)

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
            # Backtracking cost must be bounded by the pattern, not the field:
            # two quantifiers partition input (cubic) and an unanchored one
            # retries at every start position (quadratic).
            {"relation": "m", "where": {"op": "regex", "field": "id",
                                        "pattern": "x*x*y$"}},
            {"relation": "m", "where": {"op": "regex", "field": "id",
                                        "pattern": "x*y$"}},
            {"relation": "m", "where": {"op": "regex", "field": "id",
                                        "pattern": "^a|b*$"}},
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

        query = {"relation": "m", "select": ["id"], "limit": 1}
        for kwargs in ({"max_rows": 0}, {"max_bytes": 0}, {"cursor": -1},
                       {"visible": object()}, {"max_rows": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(Denied):
                    self.run_query(query, **kwargs)
        with self.assertRaises(Denied):
            self.run_query(query, cursor=6, relations={"m": MESSAGES[:2]})

    def test_regex_evaluates_a_long_declared_field(self):
        long_row = [{**MESSAGES[0], "id": "x" * 65536}]
        result = self.run_query(
            {"relation": "m", "where": {"op": "regex", "field": "id",
                                        "pattern": "^x+$"},
             "select": ["queue"], "limit": 5},
            relations={"m": long_row})
        self.assertEqual(result["rows"], [{"queue": "Q"}])
        self.assertFalse(result["more"])

    def test_long_field_single_quantifier_stays_linear(self):
        # A single anchored repetition matches on a 65 KiB field in one scan;
        # the pattern cost bound must never become a field-length refusal.
        for pattern in ("^x*y$", "^.*x$", "^[^x]*y$"):
            with self.subTest(pattern=pattern):
                long_row = [{**MESSAGES[0], "id": "x" * 65536}]
                result = self.run_query(
                    {"relation": "m", "where": {"op": "regex", "field": "id",
                                                "pattern": pattern},
                     "select": ["queue"], "limit": 5},
                    relations={"m": long_row})
                self.assertIsInstance(result["rows"], list)

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


    def test_keyless_relation_tolerates_hidden_automatic_tie_fields(self):
        schema = Relation("x", {"a": "str", "b": "int", "body": "str"})
        rows = [{"a": "two", "b": 2, "body": "hidden-2"},
                {"a": "one", "b": 1, "body": "hidden-1"}]

        def visible(name, row):
            return {field: value for field, value in row.items() if field != "body"}

        result = self.run_query({"relation": "x", "select": ["a"], "count": True},
                                relations={"x": rows}, schemas={"x": schema},
                                visible=visible)
        self.assertEqual(result["rows"], [{"a": "one"}, {"a": "two"}])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["next"], 2)
        self.assertFalse(result["more"])
        self.assertTrue(all("hidden" not in str(value) for row in result["rows"]
                            for value in row.values()))
        with self.assertRaises(Denied):
            self.run_query({"relation": "x", "select": ["a"],
                            "order": [{"field": "body", "desc": False}]},
                           relations={"x": rows}, schemas={"x": schema},
                           visible=visible)

    def test_keyed_relation_tolerates_hidden_key(self):
        schema = Relation("m", {"id": "str", "seq": "int", "body": "str"}, key="id")
        rows = [{"id": "k2", "seq": 2, "body": "s2"},
                {"id": "k1", "seq": 1, "body": "s1"}]

        def visible(name, row):
            return {field: value for field, value in row.items() if field != "id"}

        result = self.run_query({"relation": "m", "select": ["seq"], "count": True},
                                relations={"m": rows}, schemas={"m": schema},
                                visible=visible)
        self.assertEqual(result["rows"], [{"seq": 2}, {"seq": 1}])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["next"], 2)
        self.assertFalse(result["more"])

    def test_hidden_tie_field_keeps_order_and_pages_stable(self):
        schema = Relation("t", {"id": "str", "rank": "int", "note": "str",
                                "body": "str"}, key="id")
        rows = [{"id": f"t{i}", "rank": 0 if i == 3 else 1,
                 "note": f"n{i}", "body": f"s{i}"} for i in range(1, 5)]
        query = {"relation": "t", "select": ["note"],
                 "order": [{"field": "rank", "desc": True}],
                 "limit": 2, "count": True}

        def narrow(name, row):
            return {field: value for field, value in row.items()
                    if field not in ("id", "body")}

        def wide(name, row):
            return {field: value for field, value in row.items() if field != "id"}

        for view in (narrow, wide):
            with self.subTest(view=view.__name__):
                first = self.run_query(query, relations={"t": rows},
                                       schemas={"t": schema}, visible=view)
                again = self.run_query(query, relations={"t": rows},
                                       schemas={"t": schema}, visible=view)
                self.assertEqual(first, again)
                self.assertEqual(first["rows"], [{"note": "n1"}, {"note": "n2"}])
                self.assertEqual(first["count"], 4)
                self.assertNotIn("examined", first)
                self.assertEqual(first["next"], 2)
                self.assertTrue(first["more"])
                second = self.run_query(query, relations={"t": rows},
                                        schemas={"t": schema}, visible=view,
                                        cursor=first["next"])
                self.assertEqual(second["rows"], [{"note": "n4"}, {"note": "n3"}])
                self.assertEqual(second["next"], 4)
                self.assertFalse(second["more"])
                notes = [row["note"] for row in first["rows"] + second["rows"]]
                self.assertEqual(notes, ["n1", "n2", "n4", "n3"])
                self.assertEqual(len(set(notes)), 4)
                self.assertTrue(all(set(row) == {"note"}
                                    for row in first["rows"] + second["rows"]))

    def test_wide_keyless_relation_uses_bounded_tie_prefix(self):
        schema = Relation("wide", {f"f{i}": "str" for i in range(10)})
        rows = [{f"f{i}": f"{label}{i}" for i in range(10)} for label in ("a", "b")]

        def visible(name, row):
            return ({field: row[field] for field in ("f0", "f9")}
                    if row["f0"].startswith("a") else {"f9": row["f9"]})

        result = self.run_query({"relation": "wide", "select": ["f9"], "count": True},
                                relations={"wide": rows}, schemas={"wide": schema},
                                visible=visible)
        self.assertEqual(result["rows"], [{"f9": "b9"}, {"f9": "a9"}])
        self.assertEqual(result["count"], 2)
        self.assertFalse(result["more"])

    def test_full_explicit_order_keeps_input_order(self):
        schema = Relation("wide", {f"f{i}": "str" for i in range(10)})
        rows = [{**{f"f{i}": "same" for i in range(10)}, "f9": label}
                for label in ("z", "a")]
        query = {"relation": "wide", "select": ["f9"],
                 "order": [{"field": f"f{i}", "desc": False} for i in range(8)],
                 "count": True}
        result = self.run_query(query, relations={"wide": rows},
                                schemas={"wide": schema})
        self.assertEqual(result["rows"], [{"f9": "z"}, {"f9": "a"}])
        self.assertEqual(result["count"], 2)


if __name__ == "__main__":
    unittest.main()
