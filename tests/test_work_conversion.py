"""Stage-3 first slice: operator-run work-item conversion as userland example.

The procedure appends declared queue messages through the public CLI. These
tests use the real store and CLI; the dedicated work-item store stays
authoritative and is never written by the conversion.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
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
SCRIPT = ROOT / "examples" / "development" / "convert_work.py"
SCHEMA = tomllib.loads(
    (ROOT / "examples" / "development" / "queues.toml").read_text(encoding="utf-8")
)["queues"]["work"]["schema"]
MESSAGE_ID = re.compile(r"[0-9a-f]{32}\Z")


def load_script():
    spec = importlib.util.spec_from_file_location("convert_work", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def chain_id(item, rev, kind):
    seed = json.dumps(["work-item-chain-conversion-v1", item, rev, kind],
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:32]


def expected_id(item, rev, status):
    seed = json.dumps(["work-item-conversion-v1", item, rev, status],
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:32]


class WorkConversion(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items

    def env(self):
        variables = dict(os.environ)
        variables["MIZU_CONFIG"] = str(self.file)
        variables["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + variables.get("PYTHONPATH", "")
        return variables

    def convert(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "sample", *args],
                              capture_output=True, text=True, env=self.env(),
                              check=False)

    def cli_json(self, *args):
        result = subprocess.run(
            [sys.executable, "-m", "mizu", "--config", str(self.file), *args],
            capture_output=True, text=True, env=self.env(), check=True)
        return json.loads(result.stdout)

    def messages(self):
        page = self.cli_json("queue", "sample", "read", "work", "--host",
                             "--cursor", "0", "--limit", "100")
        return page["messages"]

    def make_item(self, key, title, description, *, scope=None):
        record = self.store.create(idempotency_key=key, title=title, actor="worker",
                                   description=description, **({"scope": scope} if scope else {}))
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

    def test_open_and_closed_items_become_exact_declared_messages(self):
        opened = self.make_item("open", "Open item", "Open description", scope="Open scope")
        dropped = self.make_item("dropped", "Dropped item", "Dropped description")
        self.store.transition(dropped, expected_rev=1, status="dropped", actor="worker",
                              reason="No longer needed")
        done = self.make_item("done", "Done item", "Done description")
        snapshot = self.make_snapshot("conversion-run")
        self.store.transition(done, expected_rev=1, status="doing", actor="worker")
        bind_delivery(self.project, done, snapshot["id"], expected_rev=2)
        self.store.transition(done, expected_rev=3, status="done", actor="worker",
                              reason="Finished", snapshot_id=snapshot["id"])
        doing = self.make_item("doing", "Doing item", "Doing description")
        self.store.transition(doing, expected_rev=1, status="doing", actor="worker")

        result = self.convert()
        self.assertEqual(result.returncode, 0, result.stderr)
        messages = self.messages()
        by_item = {row["body"]["item"]: row for row in messages}
        self.assertEqual(set(by_item), {opened, doing, dropped, done})

        self.assertEqual(by_item[opened]["body"], {
            "kind": "item", "item": opened, "rev": 1, "title": "Open item",
            "status": "open", "body": "Open description", "scope": "Open scope"})
        self.assertEqual(by_item[doing]["body"], {
            "kind": "item", "item": doing, "rev": 2, "title": "Doing item",
            "status": "doing", "body": "Doing description", "holder": "role:worker"})
        self.assertEqual(by_item[dropped]["body"], {
            "kind": "item", "item": dropped, "rev": 2, "title": "Dropped item",
            "status": "dropped", "body": "Dropped description",
            "reason": "No longer needed"})
        self.assertEqual(by_item[done]["body"], {
            "kind": "item", "item": done, "rev": 4, "title": "Done item",
            "status": "done", "body": "Done description", "reason": "Finished",
            "snapshot": snapshot["id"]})

        for row in messages:
            self.assertRegex(row["id"], MESSAGE_ID)
            self.assertLessEqual(set(row["body"]), set(SCHEMA))
            self.assertEqual(row["id"], expected_id(row["body"]["item"],
                                                     row["body"]["rev"], row["body"]["status"]))
            self.assertNotIn("checks", row["body"])  # not exposed by the inventory
        self.assertEqual(len({row["id"] for row in messages}), len(messages))

    def test_retry_with_same_ids_does_not_duplicate_messages(self):
        self.make_item("open", "Open item", "Open description")
        first = self.convert()
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.messages()

        second = self.convert()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.messages(), before)
        self.assertEqual(len(before), 1)

    def test_dry_run_plans_bodies_without_appending(self):
        item = self.make_item("open", "Open item", "Open description")
        result = self.convert("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        planned = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["id"], expected_id(item, 1, "open"))
        self.assertEqual(planned[0]["body"]["kind"], "item")
        self.assertEqual(self.messages(), [])

    def test_malformed_inventory_invalid_json_and_cli_failure_refuse(self):
        module = load_script()
        bad_row = {"open": {"items": [{"id": "wi_bad", "rev": 1,
                                        "title": "x", "status": "open"}]},
                   "closed": {"items": []}}
        cases = (
            ("{", "invalid inventory JSON"),
            (json.dumps(bad_row), "malformed inventory row"),
            (json.dumps({"open": {"items": []}, "closed": {"items": "not a list"}}),
             "malformed inventory"),
        )
        for raw, expected in cases:
            with self.subTest(expected=expected), patch.object(module, "cli", return_value=raw) as call:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = module.main(["sample"])
                self.assertEqual(code, 1)
                self.assertIn(expected, stderr.getvalue())
                self.assertLessEqual(len(stderr.getvalue()), 1024)
                self.assertEqual(call.call_count, 1)  # inventory only; no append

        with patch.object(module, "cli", side_effect=module.ConversionError("CLI failed: boom")) as call:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = module.main(["sample"])
            self.assertEqual(code, 1)
            self.assertIn("CLI failed: boom", stderr.getvalue())
            self.assertEqual(call.call_count, 1)

        for args in (["sample", "--limit", "0"], ["sample", "--closed", "101"]):
            with self.subTest(args=args), patch.object(module, "cli") as call:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = module.main(args)
                self.assertEqual(code, 1)
                self.assertIn("must be an integer", stderr.getvalue())
                call.assert_not_called()

    def test_conversion_does_not_change_the_work_item_store(self):
        self.make_item("open", "Open item", "Open description")
        before_files = self.seed_fingerprint()
        before_records = self.store.current_items()
        result = self.convert()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.seed_fingerprint(), before_files)
        self.assertEqual(self.store.current_items(), before_records)
        audit = self.store.audit()
        self.assertTrue(audit["consistent"])
        self.assertEqual(audit["items"], 1)

    def convert_chain(self, item, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--project-root", str(self.project.root),
             "--item", item, *args],
            capture_output=True, text=True, env=self.env(), check=False)

    def test_chain_conversion_carries_full_events_with_stable_ids(self):
        description = "Full stored description, never a preview. " * 60
        scope = "Full accepted scope, never a preview. " * 60
        reason = "Full blocked reason, never a preview. " * 60
        completion_reason = "Full completion reason, never a preview. " * 60
        item = self.make_item("chain", "Chain item", description, scope=scope)
        self.store.transition(item, expected_rev=1, status="doing", actor="worker",
                              reason="Claimed")
        wait = {"kind": "deadline", "at": "2035-01-01T00:00:00+00:00"}
        self.store.transition(item, expected_rev=2, status="blocked", actor="worker",
                              wait=wait, reason=reason)
        self.store.transition(item, expected_rev=3, status="doing", actor="worker",
                              reason="Resumed")
        snapshot = self.make_snapshot("chain-run")
        bind_delivery(self.project, item, snapshot["id"], expected_rev=4)
        self.store.transition(item, expected_rev=5, status="done", actor="worker",
                              reason=completion_reason, snapshot_id=snapshot["id"])
        before_files = self.seed_fingerprint()

        result = self.convert_chain(item)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.messages()
        self.assertEqual(len(rows), 8)
        kinds = [row["body"]["kind"] for row in rows]
        self.assertEqual(kinds, ["item", "transition", "transition", "wait",
                                 "transition", "transition", "transition", "completion"])
        by_event = {(row["body"]["rev"], row["body"]["kind"]): row["body"] for row in rows}
        self.assertEqual((by_event[(1, "item")]["body"], by_event[(1, "item")]["scope"]),
                         (description, scope))
        self.assertEqual(by_event[(3, "transition")]["reason"], reason)
        self.assertEqual(by_event[(3, "wait")]["body"],
                         json.dumps(wait, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")))
        self.assertEqual(by_event[(3, "wait")]["status"], "blocked")
        self.assertEqual(by_event[(6, "completion")]["reason"], completion_reason)
        self.assertEqual(by_event[(6, "completion")]["snapshot"], snapshot["id"])
        self.assertEqual(by_event[(2, "transition")]["holder"], "role:worker")
        self.assertNotIn("holder", by_event[(6, "completion")])
        for row in rows:
            body = row["body"]
            self.assertLessEqual(set(body), set(SCHEMA))
            self.assertTrue(set(body).isdisjoint(
                {"description_preview", "scope_preview", "reason_preview"}))
            self.assertRegex(row["id"], MESSAGE_ID)
            self.assertEqual(len({row["id"] for row in rows}), len(rows))
            self.assertEqual(row["id"], chain_id(body["item"], body["rev"], body["kind"]))
            self.assertNotEqual(row["id"], expected_id(body["item"], body["rev"],
                                                       body["status"]))
        self.assertEqual(self.seed_fingerprint(), before_files)

        retry = self.convert_chain(item)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.messages(), rows)
        self.assertEqual(self.seed_fingerprint(), before_files)

    def test_chain_conversion_refuses_mixed_or_missing_options(self):
        root = str(self.project.root)
        item = "wi_" + "a" * 32
        cases = (
            ["sample", "--project-root", root, "--item", item],
            ["--project-root", root],
            ["--item", item],
            ["--project-root", root, "--item", item, "--limit", "5"],
            ["--project-root", root, "--item", item, "--closed", "3"],
            ["sample", "--item", item, "--project-root", root],
            ["--project-root", root, "--item", "wi_bad"],
        )
        before_files = self.seed_fingerprint()
        for args in cases:
            with self.subTest(args=args):
                result = self.convert(*args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertLessEqual(len(result.stderr), 1024)
                self.assertNotIn(root, result.stderr)
        self.assertEqual(self.messages(), [])
        self.assertEqual(self.seed_fingerprint(), before_files)

    def test_chain_conversion_refuses_malformed_oversized_and_missing_files(self):
        item = self.make_item("chain-bad", "Chain item", "Stored description")
        self.store.transition(item, expected_rev=1, status="doing", actor="worker",
                              reason="Claimed")
        directory = self.project.root / "work-items" / item
        backup = {path.name: path.read_bytes() for path in directory.iterdir()}

        def restore():
            for path in directory.iterdir():
                path.unlink()
            for name, data in backup.items():
                (directory / name).write_bytes(data)

        cases = (
            ("missing seed", lambda: (directory / "create.json").unlink(),
             "create.json is missing"),
            ("oversized seed", lambda: (directory / "create.json").write_bytes(b"x" * 131073),
             "byte bound"),
            ("unexpected seed field",
             lambda: (directory / "create.json").write_text(json.dumps(
                 {**json.loads((directory / "create.json").read_text()), "extra": 1})),
             "unexpected direct shape"),
            ("preview seed field", lambda: (directory / "create.json").write_text(json.dumps(
                 {**json.loads((directory / "create.json").read_text()),
                  "work_item": {**json.loads((directory / "create.json").read_text())["work_item"],
                                "description_preview": "short"}})),
             "unexpected work-item shape"),
            ("malformed revision", lambda: (directory / "2.json").write_text("{"),
             "not valid JSON"),
            ("oversized revision", lambda: (directory / "2.json").write_bytes(b" " * 131594),
             "byte bound"),
            ("gapped revisions", lambda: (directory / "2.json").rename(directory / "4.json"),
             "gapped work-item history"),
            ("unexpected entry", lambda: (directory / "notes.json").write_text("{}"),
             "unexpected work-item directory entry"),
        )
        for label, mutate, expected in cases:
            with self.subTest(case=label):
                restore()
                mutate()
                before_files = self.seed_fingerprint()
                result = self.convert_chain(item)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertLessEqual(len(result.stderr), 1024)
                self.assertNotIn(str(self.project.root), result.stderr)
                self.assertEqual(self.messages(), [])
                self.assertEqual(self.seed_fingerprint(), before_files)
        restore()

    def test_chain_conversion_includes_a_valid_retirement_completion(self):
        reason = ("Retirement reason stored in full, not a preview. " * 40).strip()
        item = self.make_item("retired", "Retired item", "Stored description")
        self.store.transition(item, expected_rev=1, status="doing", actor="worker",
                              reason="Claimed")
        directory = self.project.root / "work-items" / item
        seed = json.loads((directory / "create.json").read_text())["work_item"]
        revision = json.loads((directory / "2.json").read_text())
        chain_hash = hashlib.sha256()
        for record in (seed, revision):
            chain_hash.update(canonical(record) + b"\n")
        request = {"id": item, "expected_rev": 2, "status": "dropped",
                   "actor": "worker", "reason": reason}
        retirement = {"schema": 1, "request": request,
                      "request_sha256": hashlib.sha256(canonical(request)).hexdigest(),
                      "chain_sha256": chain_hash.hexdigest(),
                      "created_at": "2030-01-01T00:00:00+00:00"}
        write_fixture_json(directory / "retirement.json", retirement)
        before_files = self.seed_fingerprint()

        result = self.convert_chain(item)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.messages()
        self.assertEqual([row["body"]["kind"] for row in rows],
                         ["item", "transition", "completion"])
        completion = rows[-1]
        self.assertEqual(completion["body"]["reason"], reason)
        self.assertEqual(completion["body"]["status"], "dropped")
        self.assertEqual(completion["body"]["rev"], 2)
        self.assertNotIn("holder", completion["body"])
        self.assertEqual(completion["id"], chain_id(item, 2, "completion"))
        self.assertEqual(self.seed_fingerprint(), before_files)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
