"""Stage-3 proof: declared work messages rebuild a current item view.

The integration test uses the real Fixture, public CLI and work-item store;
conversion and projection are operator-run userland procedures and never write
the dedicated store. Unit tests cover replay refusal and page bounds.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest.mock import call, patch

import support  # noqa: F401  (puts src on the path)
from support import Fixture, bind_delivery, write_fixture_json
from mizu.fs import canonical
from mizu.work_item_retirement import project as store_terminal_project

ROOT = Path(__file__).resolve().parents[1]
VIEW_SCRIPT = ROOT / "examples" / "development" / "work_view.py"
CONVERT_SCRIPT = ROOT / "examples" / "development" / "convert_work.py"
SCHEMA = tomllib.loads(
    (ROOT / "examples" / "development" / "queues.toml").read_text(encoding="utf-8")
)["queues"]["work"]["schema"]
ITEM_A = "wi_" + "a" * 32
ITEM_B = "wi_" + "b" * 32


def load_view():
    directory = str(VIEW_SCRIPT.parent)
    added = directory not in sys.path
    if added:
        sys.path.insert(0, directory)
    try:
        spec = importlib.util.spec_from_file_location("work_view", VIEW_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if added:
            sys.path.remove(directory)


VIEW = load_view()


def row(kind, item, rev, seq, **fields):
    body = {"kind": kind, "item": item, "rev": rev, "title": "Title", "status": "open",
            **fields}
    return {"seq": seq, "id": f"{seq:032x}", "body": body, "queue": "work"}


class WorkView(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items

    def env(self):
        variables = dict(os.environ)
        variables["MIZU_CONFIG"] = str(self.file)
        variables["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + variables.get("PYTHONPATH", "")
        return variables

    def run_script(self, script, *args):
        return subprocess.run([sys.executable, str(script), *args], capture_output=True,
                              text=True, env=self.env(), check=False)

    def convert_chain(self, item):
        return self.run_script(CONVERT_SCRIPT, "--project-root", str(self.project.root),
                               "--item", item)

    def view(self, *args):
        return self.run_script(VIEW_SCRIPT, "sample", *args)

    def make_item(self, key, title, description, *, scope=None, origin=None):
        options = {"scope": scope, "origin": origin}
        record = self.store.create(idempotency_key=key, title=title, actor="worker",
                                   description=description,
                                   **{name: value for name, value in options.items()
                                      if value is not None})
        return record["record"]["work_item"]["id"]

    def make_snapshot(self, run):
        captured = self.project.snapshots.capture_files(self.project.workspace)
        verification = {"passed": True, "code_digest": captured["code_digest"], "run": run}
        snapshot = self.project.snapshots.create(
            captured, goal="Goal", state="State", run=run, outcome="done",
            summary="Snapshot", verification=verification)
        self.project.snapshots.publish(snapshot)
        return snapshot

    def seed_fingerprint(self):
        root = self.project.root / "work-items"
        return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(root.rglob("*")) if path.is_file()}

    def make_retired_item(self):
        """A short sidecar fixture: conversion binds it, and the store projects it."""
        item = self.make_item("view-retired", "Retired view", "Retired description",
                              scope="Retired scope", origin="retired-origin")
        directory = self.project.root / "work-items" / item
        seed = json.loads((directory / "create.json").read_text())["work_item"]
        last = {**seed, "rev": 2, "actor": "worker", "reason": None, "wait": None,
                "snapshot_id": None, "verification": None,
                "updated_at": seed["created_at"],
                "transition": {"from": "open", "to": "open"}}
        write_fixture_json(directory / "2.json", last)
        chain_digest = hashlib.sha256()
        for record in (seed, last):
            chain_digest.update(canonical(record) + b"\n")
        request = {"id": item, "expected_rev": 2, "status": "dropped", "actor": "worker",
                   "reason": "Exhausted and retired"}
        retirement = {"schema": 1, "request": request,
                      "request_sha256": hashlib.sha256(canonical(request)).hexdigest(),
                      "chain_sha256": chain_digest.hexdigest(),
                      "created_at": "2030-01-01T00:00:00+00:00"}
        write_fixture_json(directory / "retirement.json", retirement)
        return item, store_terminal_project(last, retirement)

    def assert_store_fields(self, view_row, record):
        self.assertEqual(view_row["rev"], record["rev"])
        self.assertEqual(view_row["status"], record["status"])
        self.assertEqual(view_row["title"], record["title"])
        self.assertEqual(view_row.get("body"), record.get("description"))
        self.assertEqual(view_row.get("scope"), record.get("scope"))
        if record.get("origin") is None:
            self.assertNotIn("origin", view_row)
        else:
            self.assertEqual(json.loads(view_row["origin"]), record["origin"])
        holder = record.get("holder")
        self.assertEqual(view_row.get("holder"), "role:" + holder if holder else None)
        self.assertEqual(view_row.get("snapshot"), record.get("snapshot_id"))
        self.assertEqual(view_row.get("reason"), record.get("reason"))
        self.assertEqual(view_row.get("wait"), record.get("wait"))

    def test_converted_chain_reconstructs_current_items_from_the_queue(self):
        opened = self.make_item("view-open", "Open view", "Open description",
                                scope="Open scope", origin="open-origin")
        dropped = self.make_item("view-dropped", "Dropped view", "Dropped description")
        self.store.transition(dropped, expected_rev=1, status="dropped", actor="worker",
                              reason="No longer needed")
        live_wait = {"kind": "deadline", "at": "2035-01-01T00:00:00+00:00"}
        blocked = self.make_item("view-blocked", "Blocked view", "Blocked description")
        self.store.transition(blocked, expected_rev=1, status="blocked", actor="worker",
                              wait=live_wait, reason="Waiting for a wake")
        done = self.make_item("view-done", "Done view", "Done description",
                              scope="Done scope", origin="done-origin")
        wait = {"kind": "deadline", "at": "2035-01-01T00:00:00+00:00"}
        self.store.transition(done, expected_rev=1, status="doing", actor="worker",
                              reason="Claimed")
        self.store.transition(done, expected_rev=2, status="blocked", actor="worker",
                              wait=wait, reason="Waiting")
        self.store.transition(done, expected_rev=3, status="doing", actor="worker",
                              reason="Resumed")
        snapshot = self.make_snapshot("view-run")
        bind_delivery(self.project, done, snapshot["id"], expected_rev=4)
        self.store.transition(done, expected_rev=5, status="done", actor="worker",
                              reason="Finished", snapshot_id=snapshot["id"])
        checked = self.make_item("view-checked", "Checked view", "Checked description")
        self.store.transition(checked, expected_rev=1, status="doing", actor="worker")
        checked_path = self.project.root / "work-items" / checked / "2.json"
        checked_record = json.loads(checked_path.read_text())
        checked_record["checks"] = ["check-a", "check-b"]
        checked_path.write_bytes(canonical(checked_record))
        retired, terminal_record = self.make_retired_item()
        items = (opened, dropped, blocked, done, checked, retired)
        store_records = {item: self.store.current(item)
                         for item in (opened, dropped, blocked, done)}
        store_records[retired] = terminal_record
        before_files = self.seed_fingerprint()
        before_records = self.store.current_items()

        for item in items:
            result = self.convert_chain(item)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.view()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(set(output), {"project", "items"})
        self.assertEqual(output["project"], "sample")
        by_item = {entry["item"]: entry for entry in output["items"]}
        self.assertEqual(list(by_item), sorted(items))
        self.assertEqual(len(by_item), len(items))

        for item in items:
            if item != checked:
                self.assert_store_fields(by_item[item], store_records[item])
        self.assertEqual(by_item[opened]["kind"], "item")
        self.assertEqual(by_item[opened]["status"], "open")
        self.assertEqual(by_item[dropped]["kind"], "completion")
        self.assertEqual(by_item[dropped]["status"], "dropped")
        self.assertEqual(by_item[done]["kind"], "completion")
        self.assertEqual(by_item[blocked]["wait"], live_wait)
        self.assertEqual(by_item[done]["snapshot"], snapshot["id"])
        self.assertNotIn("wait", by_item[done])  # the blocked wait was resolved
        self.assertEqual(by_item[checked]["checks"], checked_record["checks"])
        self.assertEqual(by_item[checked]["holder"], "role:worker")
        self.assertEqual(by_item[retired]["status"], "dropped")
        self.assertEqual(by_item[retired]["reason"], "Exhausted and retired")
        self.assertEqual(by_item[retired]["rev"], 2)
        self.assertNotIn("snapshot", by_item[retired])
        self.assertNotIn("holder", by_item[retired])
        self.assertNotIn("wait", by_item[retired])
        self.assertEqual(self.seed_fingerprint(), before_files)
        self.assertEqual(self.store.current_items(), before_records)

    def test_projection_is_deterministic_and_reads_delivery_history(self):
        item = self.make_item("view-again", "Again", "Again description")
        self.store.transition(item, expected_rev=1, status="doing", actor="worker")
        result = self.convert_chain(item)
        self.assertEqual(result.returncode, 0, result.stderr)
        first = self.view()
        self.assertEqual(first.returncode, 0, first.stderr)
        again = self.view()  # explicit cursors return rows already delivered
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, first.stdout)
        values = json.loads(first.stdout)["items"]
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["rev"], 2)


class ReplayRules(unittest.TestCase):
    def test_replay_keeps_declared_fields_and_sorts_items(self):
        seed_b = row("item", ITEM_B, 1, 1, title="B", status="open", body="B body",
                     origin=canonical("b-origin").decode().rstrip("\n"),
                     holder="role:worker")
        seed_a = row("item", ITEM_A, 1, 2, title="A", status="open", body="A body")
        transition_a = row("transition", ITEM_A, 2, 3, title="A", status="blocked",
                           body="A body", holder="role:auditor", reason="Waiting")
        wait_a = row("wait", ITEM_A, 2, 4, title="A", status="blocked", body="{\"kind\": \"ci\"}")
        completion_a = row("completion", ITEM_A, 2, 5, title="A", status="done",
                           body="A body", reason="Finished", snapshot="c" * 64,
                           proof="d" * 64)
        pending = VIEW.project([seed_b, seed_a, transition_a, wait_a], SCHEMA)
        self.assertEqual(pending[0]["wait"], {"kind": "ci"})
        self.assertEqual(pending[0]["holder"], "role:auditor")
        items = VIEW.project([seed_b, seed_a, transition_a, wait_a, completion_a], SCHEMA)
        self.assertEqual([item["item"] for item in items], [ITEM_A, ITEM_B])
        self.assertEqual(items[1], seed_b["body"])
        self.assertEqual(items[0]["rev"], 2)
        self.assertNotIn("wait", items[0])
        self.assertNotIn("holder", items[0])
        self.assertEqual(items[0]["reason"], "Finished")
        self.assertEqual(items[0]["snapshot"], "c" * 64)
        self.assertEqual(items[0]["proof"], "d" * 64)
        self.assertEqual(items[0]["status"], "done")

    def test_transition_replaces_state_clearing_wait_and_proof_fields(self):
        seed = row("item", ITEM_A, 1, 1, body="Body", holder="role:worker",
                   snapshot="a" * 64, checks=["a"], proof="a" * 64,
                   origin=canonical("origin").decode().rstrip("\n"))
        wait = row("wait", ITEM_A, 1, 2, status="open", body="{\"kind\": \"ci\"}")
        transition = row("transition", ITEM_A, 2, 3, status="doing", body="Body")
        items = VIEW.project([seed, wait, transition], SCHEMA)
        self.assertNotIn("wait", items[0])
        self.assertNotIn("holder", items[0])
        self.assertNotIn("snapshot", items[0])
        self.assertNotIn("checks", items[0])
        self.assertNotIn("proof", items[0])
        self.assertEqual(json.loads(items[0]["origin"]), "origin")

    def test_refuses_malformed_and_out_of_order_messages(self):
        seed = row("item", ITEM_A, 1, 1)
        transition = row("transition", ITEM_A, 2, 2)
        chain = [seed, transition]
        def altered(base, **fields):
            return {**base, "body": {**base["body"], **fields}}

        cases = (
            ([seed, {**transition, "id": seed["id"]}], "duplicate work message id"),
            ([altered(seed, kind="reopen")], "outside the declaration"),
            ([altered(seed, rev=2)], "not at revision 1"),
            ([seed, row("item", ITEM_A, 1, 5)], "duplicate work item seed"),
            ([seed, row("transition", ITEM_A, 3, 5)], "revision gap"),
            ([transition], "has no seed"),
            ([*chain, row("wait", ITEM_A, 3, 5, status="open", body="{}")],
             "revision mismatch"),
            ([*chain, row("wait", ITEM_A, 2, 5, status="open", body="{")],
             "malformed work wait"),
            ([*chain, row("wait", ITEM_A, 2, 5, status="open", body="[]")],
             "not an object"),
            ([*chain, row("wait", ITEM_A, 2, 5, status="open", body='{"n": NaN}')],
             "malformed work wait"),
            ([*chain, row("wait", ITEM_A, 2, 5, status="doing", body="{}")],
             "disagrees"),
            ([*chain, row("completion", ITEM_A, 3, 5, status="dropped")],
             "revision mismatch"),
            ([*chain, row("completion", ITEM_A, 2, 5, status="open")],
             "status is not terminal"),
            ([*chain, row("completion", ITEM_A, 2, 4, status="done"),
              row("completion", ITEM_A, 2, 5, status="dropped")],
             "duplicate work completion"),
            ([{**seed, "body": {**seed["body"], "extra": 1}}], "undeclared work fields"),
            ([{**seed, "body": {**seed["body"], "title": "x" * 201}}],
             "declared string bound"),
            ([{**seed, "seq": True}], "invalid sequence"),
            ([{**seed, "id": "Z" * 32}], "invalid message id"),
            ([{**seed, "body": []}], "non-object body"),
        )
        for messages, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(VIEW.ConversionError) as caught:
                    VIEW.project(messages, SCHEMA)
                self.assertIn(expected, str(caught.exception))
        self.assertEqual(len(VIEW.project([*chain, row("completion", ITEM_A, 2, 4,
                                                        status="done")], SCHEMA)), 1)


class PageRules(unittest.TestCase):
    def test_reads_explicit_cursors_and_bounded_pages(self):
        first = row("item", ITEM_A, 1, 1)
        second = row("transition", ITEM_A, 2, 2)
        pages = [json.dumps({"messages": [first], "more": True, "next": 1, "head": 2}),
                 json.dumps({"messages": [second], "more": False, "next": 2, "head": 2})]
        with patch.object(VIEW, "cli", side_effect=pages) as call_cli:
            rows = VIEW.read_messages("sample", 2)
        self.assertEqual(rows, [first, second])
        self.assertEqual(call_cli.call_args_list, [
            call(["queue", "sample", "read", "work", "--host", "--cursor", "0",
                  "--limit", "512"]),
            call(["queue", "sample", "read", "work", "--host", "--cursor", "1",
                  "--limit", "512"]),
        ])

    def test_unfinished_final_page_and_bad_pages_fail_closed(self):
        unfinished = [json.dumps({"messages": [], "more": True, "next": 1, "head": 3}),
                      json.dumps({"messages": [], "more": True, "next": 2, "head": 3})]
        with patch.object(VIEW, "cli", side_effect=unfinished) as call_cli:
            with self.assertRaisesRegex(VIEW.ConversionError, "exceeds 2 pages"):
                VIEW.read_messages("sample", 2)
        self.assertEqual(call_cli.call_count, 2)
        cases = (
            ("{", "invalid queue page JSON"),
            ("[]", "queue page is not an object"),
            (json.dumps({"messages": [], "more": True, "next": 0}), "continuation did not advance"),
            (json.dumps({"messages": [], "more": "yes", "next": 0}), "continuation flag"),
        )
        for raw, expected in cases:
            with self.subTest(expected=expected), patch.object(VIEW, "cli", return_value=raw):
                with self.assertRaises(VIEW.ConversionError) as caught:
                    VIEW.read_messages("sample", 1)
                self.assertIn(expected, str(caught.exception))

    def test_cli_arguments_and_errors_are_bounded(self):
        for args in (["sample", "--pages", "0"], ["sample", "--pages", "65"],
                     ["sample", "--pages", "nope"], [], ["sAMPLE"]):
            with self.subTest(args=args), patch.object(VIEW, "cli") as call_cli:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = VIEW.main(args)
                self.assertEqual(code, 1)
                self.assertIn("work_view:", stderr.getvalue())
                self.assertLessEqual(len(stderr.getvalue()), 1024)
                call_cli.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(unittest.main())
