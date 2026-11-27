"""Stage-3 pre-cutover gate: the declared queue must reproduce the store.

The integration test uses the real Fixture, public CLI, work-item store and
converted chains; the gate only reads them. Pure tests inject malformed
inventory, page bounds and changed queue bodies without the CLI.
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
from unittest.mock import patch

import support  # noqa: F401  (puts src on the path)
from support import Fixture, bind_delivery, write_fixture_json
from mizu.fs import canonical

ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = ROOT / "examples" / "development" / "verify_work_conversion.py"
CONVERT_SCRIPT = ROOT / "examples" / "development" / "convert_work.py"
SCHEMA = tomllib.loads(
    (ROOT / "examples" / "development" / "queues.toml").read_text(encoding="utf-8")
)["queues"]["work"]["schema"]
ITEM_A = "wi_" + "a" * 32
ITEM_B = "wi_" + "b" * 32


def load_gate():
    directory = str(GATE_SCRIPT.parent)
    added = directory not in sys.path
    if added:
        sys.path.insert(0, directory)
    try:
        spec = importlib.util.spec_from_file_location("verify_work_conversion", GATE_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if added:
            sys.path.remove(directory)


GATE = load_gate()


def row(kind, item, rev, seq, **fields):
    body = {"kind": kind, "item": item, "rev": rev, "title": "Title", "status": "open",
            **fields}
    return {"seq": seq, "id": f"{seq:032x}", "body": body, "queue": "work"}


def complete_page(items, *, total=None, section="open"):
    page = {"items": items, "total": len(items) if total is None else total,
            "cursor": None, "next_cursor": None, "overflow_count": 0,
            "truncated": False}
    return page


class CutoverGate(Fixture):
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

    def cli_json(self, *args):
        result = subprocess.run(
            [sys.executable, "-m", "mizu", "--config", str(self.file), *args],
            capture_output=True, text=True, env=self.env(), check=True)
        return json.loads(result.stdout)

    def queue_rows(self):
        page = self.cli_json("queue", "sample", "read", "work", "--host",
                             "--cursor", "0", "--limit", "100")
        return page["messages"]

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

    def fingerprint(self):
        root = self.project.root / "work-items"
        return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(root.rglob("*")) if path.is_file()}

    def make_retired_item(self):
        item = self.make_item("gate-retired", "Retired", "Retired description",
                              origin="retired-origin")
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
        return item

    def test_converted_chains_reproduce_the_store_view_before_cutover(self):
        opened = self.make_item("gate-open", "Open", "Open description",
                                scope="Open scope", origin="open-origin")
        doing = self.make_item("gate-doing", "Doing", "Doing description")
        self.store.transition(doing, expected_rev=1, status="doing", actor="searcher")
        doing_path = self.project.root / "work-items" / doing / "2.json"
        doing_record = json.loads(doing_path.read_text())
        doing_record["checks"] = ["gate-check"]
        doing_path.write_bytes(canonical(doing_record))
        wait = {"kind": "deadline", "at": "2035-01-01T00:00:00+00:00"}
        blocked = self.make_item("gate-blocked", "Blocked", "Blocked description")
        self.store.transition(blocked, expected_rev=1, status="blocked", actor="worker",
                              wait=wait, reason="Waiting")
        done = self.make_item("gate-done", "Done", "Done description")
        self.store.transition(done, expected_rev=1, status="doing", actor="worker")
        self.store.transition(done, expected_rev=2, status="blocked", actor="worker",
                              wait=wait, reason="Waiting")
        self.store.transition(done, expected_rev=3, status="doing", actor="worker")
        snapshot = self.make_snapshot("gate-run")
        bind_delivery(self.project, done, snapshot["id"], expected_rev=4)
        self.store.transition(done, expected_rev=5, status="done", actor="worker",
                              reason="Finished", snapshot_id=snapshot["id"])
        dropped = self.make_item("gate-dropped", "Dropped", "Dropped description")
        self.store.transition(dropped, expected_rev=1, status="dropped", actor="worker",
                              reason="No longer needed")
        retired = self.make_retired_item()
        items = (opened, doing, blocked, done, dropped, retired)
        before_files = self.fingerprint()
        before_records = self.store.current_items()

        for item in items:
            result = self.convert_chain(item)
            self.assertEqual(result.returncode, 0, result.stderr)
        before_queue = self.queue_rows()
        gate = self.run_script(GATE_SCRIPT, "--project-root", str(self.project.root))
        self.assertEqual(gate.returncode, 0, gate.stderr)
        self.assertEqual(json.loads(gate.stdout),
                         {"project": "sample", "checked": len(items), "matched": True})
        self.assertEqual(self.queue_rows(), before_queue)
        self.assertEqual(self.fingerprint(), before_files)
        self.assertEqual(self.store.current_items(), before_records)


class GateRules(unittest.TestCase):
    def test_changed_queue_body_from_a_hand_built_row_is_refused(self):
        seed = row("item", ITEM_A, 1, 1, body="Original body", status="open")
        store_view = GATE.project([seed], SCHEMA)
        changed = row("item", ITEM_A, 1, 1, body="Altered body", status="open")
        queue_view = GATE.project([changed], SCHEMA)
        with self.assertRaisesRegex(GATE.ConversionError, "does not reproduce"):
            GATE.compare_views(store_view, queue_view, [ITEM_A])

    def test_missing_or_changed_completion_proof_is_refused(self):
        seed = row("item", ITEM_A, 1, 1, body="Body", status="open")
        completion = row("completion", ITEM_A, 1, 2, body="Body", status="done",
                         proof="a" * 64)
        store_view = GATE.project([seed, completion], SCHEMA)
        changed = (
            row("completion", ITEM_A, 1, 2, body="Body", status="done"),
            row("completion", ITEM_A, 1, 2, body="Body", status="done",
                proof="b" * 64),
        )
        for other in changed:
            with self.subTest(body=other["body"]):
                queue_view = GATE.project([seed, other], SCHEMA)
                with self.assertRaisesRegex(GATE.ConversionError, "does not reproduce"):
                    GATE.compare_views(store_view, queue_view, [ITEM_A])

    def test_matching_completion_proof_compares_equal(self):
        seed = row("item", ITEM_A, 1, 1, body="Body", status="open")
        completion = row("completion", ITEM_A, 1, 2, body="Body", status="done",
                         proof="a" * 64)
        store_view = GATE.project([seed, completion], SCHEMA)
        queue_view = GATE.project([seed, completion], SCHEMA)
        GATE.compare_views(store_view, queue_view, [ITEM_A])

    def test_extra_and_missing_queue_items_are_refused(self):
        seed_a = row("item", ITEM_A, 1, 1)
        seed_b = row("item", ITEM_B, 1, 2)
        view_a = GATE.project([seed_a], SCHEMA)
        view_ab = GATE.project([seed_a, seed_b], SCHEMA)
        with self.assertRaisesRegex(GATE.ConversionError, "absent from the store inventory"):
            GATE.compare_views(view_a, view_ab, [ITEM_A])
        with self.assertRaisesRegex(GATE.ConversionError, "missing stored work items"):
            GATE.compare_views(view_ab, view_a, [ITEM_A, ITEM_B])
        GATE.compare_views(view_ab, view_ab, [ITEM_B, ITEM_A])  # canonical sorting

    def test_malformed_and_unbounded_inventory_pages_are_refused(self):
        good = complete_page([{"id": ITEM_A}])
        cases = (
            (None, "not an object"),
            ({"open": good, "closed": None}, "closed page is not an object"),
            ({"open": {**good, "next_cursor": "next"},
              "closed": complete_page([], section="closed")}, "open page is incomplete"),
            ({"open": good, "closed": {**complete_page([], section="closed"),
                                       "next_cursor": "next"}}, "closed page is incomplete"),
            ({"open": good, "closed": {**complete_page([], section="closed"),
                                       "overflow_count": 1}}, "closed page is incomplete"),
            ({"open": {**good, "overflow_count": 1}, "closed": complete_page([], section="closed")},
             "open page is incomplete"),
            ({"open": {**good, "truncated": True}, "closed": complete_page([], section="closed")},
             "open page is incomplete"),
            ({"open": {**good, "total": 2}, "closed": complete_page([], section="closed")},
             "page total disagrees"),
            ({"open": good, "closed": {**complete_page([], section="closed"),
                                       "cursor": "later"}}, "input cursor"),
            ({"open": complete_page([{"id": ITEM_A}] * 101),
              "closed": complete_page([], section="closed")}, "bounded item list"),
            ({"open": complete_page([{"id": "wi_bad"}]),
              "closed": complete_page([], section="closed")}, "invalid work item id"),
            ({"open": complete_page([{"id": ITEM_A}, {"id": ITEM_A}]),
              "closed": complete_page([], section="closed")}, "duplicate work item"),
        )
        for inventory, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(GATE.ConversionError) as caught:
                    GATE.inventory_items(inventory)
                self.assertIn(expected, str(caught.exception))

    def test_chain_rows_carry_declared_ids_and_sequences(self):
        messages = [
            {"id": "a" * 32, "body": {"kind": "item", "item": ITEM_A, "rev": 1}},
            {"id": "b" * 32, "body": {"kind": "completion", "item": ITEM_A, "rev": 1}},
        ]
        root = Path("sample")
        with patch.object(GATE, "chain_plan", return_value=("sample", messages)) as planned:
            rows = GATE.chain_rows(root, [ITEM_A], SCHEMA)
        planned.assert_called_once_with(root, ITEM_A, SCHEMA)
        self.assertEqual([r["seq"] for r in rows], [1, 2])
        self.assertEqual([r["id"] for r in rows], ["a" * 32, "b" * 32])
        self.assertEqual([r["body"] for r in rows], [m["body"] for m in messages])
        with patch.object(GATE, "chain_plan", return_value=("other", messages)):
            with self.assertRaisesRegex(GATE.ConversionError, "different project"):
                GATE.chain_rows(root, [ITEM_A], SCHEMA)
        with patch.object(GATE, "chain_plan",
                          return_value=("sample", [messages[0], messages[0]])):
            with self.assertRaisesRegex(GATE.ConversionError, "duplicate converted message id"):
                GATE.chain_rows(root, [ITEM_A], SCHEMA)

    def test_argument_and_inventory_bounds_fail_closed(self):
        root = ROOT / "sample"
        cases = (
            (["--pages", "1"], "argument error"),
            (["--project-root", str(root), "--pages", "0"], "--pages must be an integer"),
            (["--project-root", str(root), "--pages", "65"], "--pages must be an integer"),
            (["--project-root", str(ROOT / "Bad.Name")], "invalid project name"),
        )
        for args, expected in cases:
            with self.subTest(args=args), patch.object(GATE, "cli") as cli_call, \
                    patch.object(GATE, "read_messages") as read:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = GATE.main(args)
                self.assertEqual(code, 1)
                self.assertIn(expected, stderr.getvalue())
                self.assertLessEqual(len(stderr.getvalue()), 1024)
                cli_call.assert_not_called()
                read.assert_not_called()

        inventory = {"open": {**complete_page([{"id": ITEM_A}]), "overflow_count": 1},
                     "closed": complete_page([], section="closed")}
        with patch.object(GATE, "cli", return_value=json.dumps(inventory)) as cli_call, \
                patch.object(GATE, "read_messages") as read:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = GATE.main(["--project-root", str(root)])
        self.assertEqual(code, 1)
        self.assertIn("open page is incomplete: overflow", stderr.getvalue())
        cli_call.assert_called_once_with(["work-items", "sample", "--limit", "100",
                                          "--closed", "100"])
        read.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(unittest.main())
