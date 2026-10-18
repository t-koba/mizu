"""Terminal delivery protocol: real stores and fake model dispatch, no RPC/network."""
import concurrent.futures
import dataclasses
import json
from contextlib import nullcontext
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu import insight_archive, work_item_events as events
from mizu.cli import execute, parser
from mizu.config import load
from mizu.errors import Busy, ConfigError, Denied, InfraExceeded, LimitExceeded, MissingFinish
from mizu.fs import canonical, digest, lock, read_json, write_json
from mizu.runtime import Engine, _role_events_due, prompt_for, prompt_delta_for
from mizu.services import event_watches, render
from mizu.work_items import _timestamp_order


class WorkItemEventTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.serial = 0

    def seed(self, source="reviewer"):
        self.serial += 1
        proposal = self.project.insights.submit(
            source=source, title=f"Terminal work {self.serial}", body="Evidence", base_snapshot=None)
        decision = self.project.insights.decide(
            proposal["id"], "accept", "reviewed", "", "run", expected_rev=1)
        return proposal, decision["work_item"]

    def terminal(self, source="reviewer", *, timestamp=None, done=False):
        proposal, seed = self.seed(source)
        kwargs = {}
        if done:
            captured = self.project.snapshots.capture_files(self.project.workspace)
            verification = {"passed": True, "code_digest": captured["code_digest"], "run": "verified-run"}
            snapshot = self.project.snapshots.create(
                captured, goal="Goal", state="State", run="verified-run", outcome="done",
                summary="Verified", verification=verification)
            self.project.snapshots.publish(snapshot)
            kwargs["snapshot_id"] = snapshot["id"]
        with patch("mizu.work_items.now", return_value=timestamp) if timestamp else nullcontext():
            item = self.store.transition(seed["id"], expected_rev=1,
                                         status="done" if done else "dropped", actor="worker",
                                         reason="Terminal reason " + "界" * 2000, **kwargs)
        return proposal, item

    def page(self, role="reviewer", actions=("done", "dropped"), **kwargs):
        return events.pending(self.store, role, actions, **kwargs)

    def subscribed_config(self, **changes):
        roles = dict(self.config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], work_item_events=("done", "dropped"), **changes)
        return dataclasses.replace(self.config, roles=roles)

    def test_disabled_default_and_config_validation(self):
        self.assertTrue(all(not r.work_item_events for r in self.config.roles.values()))
        original = self.file.read_text()
        for values, scope, valid in (("[]", "own", True), ('["done"]', "any", True),
                                      ('["done", "dropped"]', "own", True),
                                      ('["done", "done"]', "own", False),
                                      ('["doing"]', "own", False), ('["DONE"]', "own", False),
                                      ('"done"', "own", False), ("[]", "any", False)):
            with self.subTest(values=values, scope=scope):
                self.file.write_text(original.replace(
                    "[roles.reviewer]", f'[roles.reviewer]\nwork_item_events = {values}\ndecision_sources = "{scope}"'))
                if valid:
                    self.assertEqual(load(self.file).roles["reviewer"].work_item_sources, scope)
                    self.assertEqual(load(self.file).roles["reviewer"].decision_sources, "own")
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)

    def test_independent_route_defaults_migration_and_validation(self):
        original = self.file.read_text()
        # decision subscription, terminal subscription, raw route lines, effective pair
        cases = [
            ('[]', '[]', '', ("own", "own")),
            ('["reject"]', '["done"]', 'decision_sources = "any"', ("any", "any")),
            ('[]', '["done"]', 'decision_sources = "any"', ("own", "any")),
            ('["reject"]', '[]', 'decision_sources = "any"', ("any", "own")),
            ('[]', '["done"]', 'work_item_sources = "any"', ("own", "any")),
            ('["reject"]', '["done"]', 'decision_sources = "any"\nwork_item_sources = "own"', ("any", "own")),
            ('["reject"]', '["done"]', 'decision_sources = "own"\nwork_item_sources = "any"', ("own", "any")),
            ('["reject"]', '["done"]', 'decision_sources = "any"\nwork_item_sources = "any"', ("any", "any")),
            ('["reject"]', '[]', 'decision_sources = "any"\nwork_item_sources = "own"', ("any", "own")),
            ('[]', '["done"]', 'decision_sources = "any"\nwork_item_sources = "any"', None),
            ('["reject"]', '[]', 'work_item_sources = "any"', None),
            ('[]', '[]', 'work_item_sources = "any"', None),
            ('[]', '["done"]', 'work_item_sources = "everyone"', None),
            ('[]', '["done"]', 'work_item_sources = true', None),
            ('[]', '["done"]', 'work_item_sources = []', None),
            ('["reject"]', '["done"]', 'decision_sources = "everyone"\nwork_item_sources = "own"', None),
        ]
        for decisions, terminal, routes, expected in cases:
            with self.subTest(decisions=decisions, terminal=terminal, routes=routes):
                self.file.write_text(original.replace(
                    "[roles.reviewer]", f"[roles.reviewer]\ndecision_events = {decisions}\n"
                    f"work_item_events = {terminal}\n{routes}"))
                if expected is None:
                    with self.assertRaises(ConfigError):
                        load(self.file)
                else:
                    role = load(self.file).roles["reviewer"]
                    self.assertEqual((role.decision_sources, role.work_item_sources), expected)

    def test_independent_routes_through_pending_admission_and_cli(self):
        text = self.file.read_text()
        for name, decisions, terminal in (("reviewer", "any", "own"), ("searcher", "own", "any")):
            text = text.replace(f"[roles.{name}]", f'[roles.{name}]\ndecision_events = ["accept"]\n'
                                f'work_item_events = ["dropped"]\ndecision_sources = "{decisions}"\n'
                                f'work_item_sources = "{terminal}"')
        self.file.write_text(text)
        config = load(self.file)
        self.project.config = config
        self.terminal(source="worker")
        driver = ScriptDriver()
        engine = Engine(config, driver=driver)
        for name in ("reviewer", "searcher"):
            role = config.roles[name]
            with self.subTest(role=name):
                decisions = self.project.insights.decision_events(name, role.decision_events,
                                                                  sources=role.decision_sources)
                terminal = events.pending(self.store, name, role.work_item_events, sources=role.work_item_sources)
                self.assertEqual(len(decisions), int(name == "reviewer"))
                self.assertEqual(terminal["pending"], int(name == "searcher"))
                self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), name))
                cursor_before = self.project.insights.decision_cursor(name)
                with patch.object(self.project.insights, "acknowledge_decisions",
                                  wraps=self.project.insights.acknowledge_decisions) as decision_ack:
                    result = engine.run(self.project, name, event_trigger=True)
                self.assertEqual(result["status"], "completed")
                prompt = json.loads(driver.calls[-1][1])
                self.assertEqual(prompt["decision_events"], decisions)
                self.assertEqual(prompt.get("work_item_events", []), terminal["events"])
                if name == "reviewer":
                    self.assertEqual(decision_ack.call_args.kwargs["sources"], "any")
                else:
                    decision_ack.assert_not_called()
                    self.assertEqual(self.project.insights.decision_cursor(name), cursor_before)
                self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), name))
                self.assertEqual(engine.run(self.project, name, event_trigger=True)["skipped"], "no-event")
        output = execute(parser().parse_args(["--config", str(self.file), "work-items", self.project.name]))
        self.assertEqual(output["closed"]["items"][0]["event_acknowledgements"], {"searcher": True})
        summaries = {row["role"]: row for row in output["event_delivery"]["roles"]}
        self.assertEqual((summaries["reviewer"]["sources"], summaries["reviewer"]["acknowledged"]), ("own", 0))
        self.assertEqual((summaries["searcher"]["sources"], summaries["searcher"]["acknowledged"]), ("any", 1))
        # Any-source terminal delivery did not consume the foreign decision.
        self.assertEqual(len(self.project.insights.decision_events("searcher", ("accept",), sources="any")), 1)

    def test_origin_not_actor_and_unknown_only_any(self):
        proposal, item = self.terminal()
        row = self.page()["events"][0]
        self.assertEqual(row["event"], f"work_item:{item['id']}:2")
        self.assertEqual((row["source"], row["type"], row["actor"], row["origin_source"]),
                         ("work_item", "dropped", "worker", "reviewer"))
        self.assertEqual(row["origin"], {"insight_id": proposal["id"], "rev": 1})
        self.assertTrue(row["reason_truncated"])
        self.assertLessEqual(len(row["reason_preview"].encode()), 1024)
        self.assertEqual(self.page("worker")["events"], [])
        self.assertEqual(len(self.page("searcher", sources="any")["events"]), 1)
        # Removing the supported origin body is a legacy routing hole, not an
        # invitation to fall back to transition actor (or the identity sidecar).
        (self.project.root / "inbox" / f"{proposal['id']}.json").unlink()
        self.assertEqual(self.page()["events"], [])
        self.assertEqual(self.page("worker")["events"], [])
        unknown = self.page("searcher", sources="any")
        self.assertEqual(unknown["unknown_routing"], 1)
        self.assertIsNone(unknown["events"][0]["origin_source"])
        self.assertEqual(unknown["events"][0]["routing"], "unknown")

    def test_revised_then_archived_origin_preserves_seed_refs_and_routing(self):
        proposal, item = self.terminal()
        revised = self.project.insights.revise(proposal["id"], source="reviewer", title="Revised",
                                                body="New evidence", base_snapshot=None, expected_rev=1)
        self.assertEqual(revised["rev"], 2)
        original = self.page()["events"][0]
        self.assertEqual(original["title"], item["title"])
        self.assertEqual(original["origin"]["rev"], 1)
        # Use the supported archive writer, then remove the inbox record as GC does.
        body = self.project.insights.read(proposal["id"])
        identity = read_json(self.project.root / "insight-ids" / f"{proposal['id']}.json")
        self.assertEqual(identity["id"], proposal["id"])
        insight_archive.store(self.project.root, body, {}, identity)
        (self.project.root / "inbox" / f"{proposal['id']}.json").unlink()
        self.assertEqual(self.page()["events"], [original])
        events.acknowledge(self.store, "reviewer", [original])
        self.assertEqual(self.page()["acknowledged"], 1)

    def test_done_has_snapshot_and_bounded_verification_reference(self):
        _, item = self.terminal(done=True)
        row = self.page()["events"][0]
        self.assertEqual(row["snapshot_id"], item["snapshot_id"])
        self.assertEqual(row["verification"], {"sha256": digest(canonical(item["verification"])),
                                               "passed": True,
                                               "code_digest": item["verification"]["code_digest"]})
        self.assertLessEqual(len(canonical(row)), 8192)
        events.acknowledge(self.store, "reviewer", [row])
        self.assertEqual(self.page()["events"], [])

    def test_timestamp_normalization_ties_rollback_and_limit_drain(self):
        times = ("2030-03-01T09:00:00+09:00", "2030-03-01T00:00:00+00:00",
                 "2020-01-01T00:00:00+00:00", "2030-03-01T00:00:01+00:00")
        items = [self.terminal(timestamp=t)[1] for t in times]
        ordered = [items[2]["id"], *sorted([items[0]["id"], items[1]["id"]]), items[3]["id"]]
        all_seen = []
        for n in range(4):
            page = self.page(limit=1)
            self.assertEqual((page["pending"], page["acknowledged"]), (4-n, n))
            self.assertEqual(page["overflow_count"], 3-n)
            self.assertEqual(page["truncated"], n < 3)
            all_seen += [page["events"][0]["id"]]
            events.acknowledge(self.store, "reviewer", page["events"])
        self.assertEqual(all_seen, ordered)
        self.assertEqual(self.page()["events"], [])
        # A terminal event can arrive after a successful batch with an older
        # clock value. Exact receipts must still expose it.
        _, rollback = self.terminal(timestamp="2010-01-01T00:00:00+00:00")
        self.assertEqual(self.page(limit=1)["events"][0]["id"], rollback["id"])

    def test_later_decision_cursor_never_hides_older_work_event_and_vice_versa(self):
        self.terminal(timestamp="2020-01-01T00:00:00+00:00")
        proposal = self.project.insights.submit(source="reviewer", title="Decision only",
                                                body="Evidence", base_snapshot=None)
        self.project.insights.decide(proposal["id"], "reject", "No work", "", "run", expected_rev=1)
        decisions = self.project.insights.decision_events("reviewer", ("reject",))
        self.project.insights.acknowledge_decisions("reviewer", decisions)
        cursor = self.project.insights.decision_cursor("reviewer")
        self.assertEqual(len(self.page()["events"]), 1)
        events.acknowledge(self.store, "reviewer", self.page()["events"])
        self.assertEqual(self.project.insights.decision_cursor("reviewer"), cursor)
        proposal = self.project.insights.submit(source="reviewer", title="Decision only",
                                                body="Evidence", base_snapshot=None)
        self.project.insights.decide(proposal["id"], "reject", "Another", "", "run", expected_rev=1)
        self.assertEqual(len(self.project.insights.decision_events("reviewer", ("reject",))), 1)
        self.assertEqual(self.page()["events"], [])

    def test_partial_ack_failure_is_exact_and_replay_safe_and_per_role(self):
        for _ in range(3):
            self.terminal()
        selected = self.page()["events"]
        real_write = events.write_json
        writes = []
        def fail_second(path, value, **kwargs):
            writes.append(path)
            if len(writes) == 2:
                raise OSError("receipt unavailable")
            real_write(path, value, **kwargs)
        with patch.object(events, "write_json", side_effect=fail_second):
            with self.assertRaises(OSError):
                events.acknowledge(self.store, "reviewer", selected)
        page = self.page()
        self.assertEqual(page["acknowledged"], 1)
        self.assertEqual(page["events"], selected[1:])
        # A disjoint concurrent acknowledgement cannot overwrite another one.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda row: events.acknowledge(self.store, "reviewer", [row]), selected[1:]))
        events.acknowledge(self.store, "reviewer", selected)
        self.assertEqual(self.page()["acknowledged"], 3)
        # Identical concurrent receipts are idempotent and immutable.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: events.acknowledge(self.store, "searcher", [selected[0]]), range(2)))
        self.assertEqual(self.page("searcher", sources="any")["acknowledged"], 1)
        self.assertEqual(self.page("searcher", sources="any")["pending"], 2)
        # An uncommitted atomic-write leftover is never a receipt.
        directory = self.project.root / "work-item-receipts" / "reviewer"
        (directory / ".new-abcdefgh").write_text("incomplete")
        self.assertEqual(self.page()["acknowledged"], 3)
        self.assertTrue((directory / ".new-abcdefgh").exists())

    def test_malformed_or_incomplete_chain_and_unsafe_receipt_fail_closed(self):
        _, item = self.terminal()
        path = self.project.root / "work-items" / item["id"] / "2.json"
        original = path.read_bytes()
        bad = dict(item, transition={"from": "doing", "to": "dropped"})
        write_json(path, bad)
        with self.assertRaises(Denied):
            self.page()
        path.write_bytes(original)
        gap = path.with_name("3.json")
        path.rename(gap)
        with self.assertRaises(Denied):
            self.page()
        gap.rename(path)
        selected = self.page()["events"]
        events.acknowledge(self.store, "reviewer", selected)
        receipt = self.project.root / "work-item-receipts/reviewer" / f"{item['id']}.2.json"
        write_json(receipt, {"event": selected[0]["event"]})
        with self.assertRaises(Denied):
            self.page()
        with self.assertRaises(Denied):
            events.acknowledge(self.store, "reviewer", [{**selected[0], "id": "../outside"}])

    def test_acknowledged_position_is_exact_bounded_and_ordered(self):
        for _ in range(12):
            self.terminal()
        rows = self.page(limit=100)["events"]
        events.acknowledge(self.store, "reviewer", rows)
        page = self.page(limit=100)
        expected = [row["event"] for row in sorted(
            rows, key=lambda row: (_timestamp_order(row["terminal_at"]), row["id"], row["rev"]))[-10:]]
        self.assertEqual(page["acknowledged"], 12)
        self.assertEqual(page["acknowledged_event_ids"], expected)
        self.assertTrue(page["acknowledged_ids_truncated"])

    def test_event_admission_prompts_run_evidence_and_successful_only_ack(self):
        config = self.subscribed_config()
        driver = ScriptDriver()
        engine = Engine(config, driver=driver)
        self.assertEqual(engine.run(self.project, "reviewer", event_trigger=True)["skipped"], "no-event")
        engine.run(self.project, "reviewer")  # marks unchanged code
        self.terminal()
        batch = self.page()["events"]
        # Ack is reached only after completed result is on disk.
        real_ack = events.acknowledge
        def assert_durable(store, role, rows):
            ctx = driver.calls[-1][0]
            self.assertEqual(read_json(ctx.run_dir / "result.json")["status"], "completed")
            return real_ack(store, role, rows)
        with patch.object(events, "acknowledge", side_effect=assert_durable):
            result = engine.run(self.project, "reviewer", event_trigger=True)
        delivered_prompt = json.loads(driver.calls[-1][1])
        self.assertEqual(delivered_prompt["work_item_events"], batch)
        self.assertEqual(list(delivered_prompt).index("work_item_events"),
                         list(delivered_prompt).index("decision_events") + 1)
        run = self.project.root / "runs" / result["run"]
        self.assertEqual(read_json(run / "started.json")["admission"], "work_item")
        self.assertEqual(read_json(run / "started.json")["work_item_events"], 1)
        self.assertEqual(read_json(run / "prompt_projection.json")["work_item_events"], 1)
        self.assertEqual(read_json(run / "work-item-events.json")["events"], batch)
        self.assertEqual(json.loads(driver.calls[-1][1])["work_item_events"], batch)
        # Full and durable-session delta prompts carry a separate field.
        ctx = driver.calls[-1][0]
        self.assertEqual(json.loads(prompt_for(ctx))["work_item_events"], batch)
        self.assertEqual(json.loads(prompt_delta_for(ctx, []))["work_item_events"], batch)
        self.assertEqual(self.page()["events"], [])
        self.assertEqual(engine.run(self.project, "reviewer", event_trigger=True)["skipped"], "no-event")

    def test_ack_failure_replays_and_new_events_during_run_not_acknowledged(self):
        self.terminal()
        original = self.page()["events"]
        driver = ScriptDriver(lambda *_: self.terminal())
        engine = Engine(self.subscribed_config(), driver=driver)
        with patch.object(events, "acknowledge", side_effect=OSError("full disk")):
            result = engine.run(self.project, "reviewer", event_trigger=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.page()["pending"], 2)
        self.assertEqual(read_json(driver.calls[-1][0].run_dir / "work-item-events.json")["events"], original)
        # After retry both are delivered; an arrival during this second run
        # remains pending because acknowledgements use only the frozen batch.
        engine.run(self.project, "reviewer", event_trigger=True)
        self.assertEqual((self.page()["pending"], self.page()["acknowledged"]), (1, 2))

    def test_failed_unsealed_and_bounded_runs_do_not_ack(self):
        self.terminal()
        def fail(*_):
            raise Denied("injected execution failure")
        with self.assertRaises(Denied):
            Engine(self.subscribed_config(), driver=ScriptDriver(fail)).run(self.project, "reviewer", event_trigger=True)
        class Unsealed:
            requires_sandbox = False
            def execute(self, *args, **kwargs):
                return {"requests": 0}
        with self.assertRaises(MissingFinish):
            Engine(self.subscribed_config(), driver=Unsealed()).run(self.project, "reviewer", event_trigger=True)
        def bounded(ctx, *_):
            ctx.bound_hit = "requests"
        Engine(self.subscribed_config(), driver=ScriptDriver(bounded)).run(self.project, "reviewer", event_trigger=True)
        self.assertEqual((self.page()["pending"], self.page()["acknowledged"]), (1, 0))

    def test_busy_and_selector_wait_do_not_ack(self):
        self.terminal()
        engine = Engine(self.subscribed_config(), driver=ScriptDriver())
        with lock(self.project.root / "locks/run-reviewer.lock"):
            with self.assertRaises(Busy):
                engine.run(self.project, "reviewer", event_trigger=True)
        # Use a real empty selector policy, as in selection integration tests.
        # A coalesced pre-run selector wait is enough to exercise the gate;
        # the selection mechanism's own tests cover initial wait persistence.
        waiting = {"profile": "", "next_evaluation_at": None}
        selector_config = self.subscribed_config(selector="test-selector")
        with patch("mizu.classification.prepare", return_value=waiting), \
                patch("mizu.selection.wait_fingerprint", return_value="stable"), \
                patch("mizu.selection.read_wait", return_value={"fingerprint": "stable", "run": "prior-wait"}):
            result = Engine(selector_config, driver=ScriptDriver()).run(self.project, "reviewer", event_trigger=True)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(self.page()["pending"], 1)
        exhausted = dataclasses.replace(self.subscribed_config(),
                                         limits=dataclasses.replace(self.config.limits, daily_requests=0))
        with self.assertRaises(InfraExceeded):
            Engine(exhausted, driver=ScriptDriver()).run(self.project, "reviewer", event_trigger=True)
        self.assertEqual(self.page()["acknowledged"], 0)

    def test_daemon_gate_and_platform_watch_rendering(self):
        config = self.subscribed_config(interval_seconds=0, calendar=(), daemon=False)
        self.project.config = config
        watches = event_watches(config, self.project)
        self.assertIn(str(self.project.root / "work-items"), watches["reviewer"])
        self.assertNotIn(str(self.project.root / "work-item-receipts/reviewer"), watches["reviewer"])
        for system, marker in (("linux", "PathChanged="), ("macos", "WatchPaths")):
            definitions = render(dataclasses.replace(config, timezone="local"), self.project, self.root / "mizu", system=system)
            text = "\n".join(definitions.values())
            self.assertIn(marker, text)
            self.assertIn(str(self.project.root / "work-items"), text)
        self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), "reviewer"))
        with patch("mizu.work_items.os.utime") as hint:
            self.terminal()
        hint.assert_called_once_with(self.project.root / "work-items", None, follow_symlinks=False)
        self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), "reviewer"))
        with patch("mizu.work_item_events.os.utime") as ack_hint:
            events.acknowledge(self.store, "reviewer", self.page()["events"])
        ack_hint.assert_called_once_with(self.project.root / "work-items", None, follow_symlinks=False)
        self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), "reviewer"))

    def test_cli_progress_is_bounded_and_read_only(self):
        # Persist an opt-in so the CLI's read-only Project uses it.
        self.file.write_text(self.file.read_text().replace(
            "[roles.reviewer]", '[roles.reviewer]\nwork_item_events = ["dropped"]'))
        self.terminal()
        def contents():
            return {p.relative_to(self.project.root).as_posix(): (p.stat().st_mtime_ns, p.read_bytes())
                    for p in self.project.root.rglob("*") if p.is_file()}
        before = contents()
        args = parser().parse_args(["--config", str(self.file), "work-items", self.project.name])
        with patch("mizu.work_items.lock", side_effect=AssertionError("read lock")), \
                patch.object(events, "acknowledge", side_effect=AssertionError("read ack")):
            output = execute(args)
        rows = output["event_delivery"]["roles"]
        self.assertTrue(all(row["subscribed"] for row in rows))
        reviewer = next(row for row in rows if row["role"] == "reviewer")
        current_event_id = self.page()["events"][0]["event"]
        self.assertEqual((reviewer["subscribed"], reviewer["pending"], reviewer["acknowledged"]),
                         (["dropped"], 1, 0))
        self.assertEqual(reviewer["pending_event_ids"], [current_event_id])
        self.assertEqual(reviewer["acknowledged_event_ids"], [])
        self.assertNotIn("events", reviewer)
        self.assertEqual(contents(), before)
        events.acknowledge(self.store, "reviewer", self.page()["events"])
        output = execute(args)
        reviewer = next(row for row in output["event_delivery"]["roles"] if row["role"] == "reviewer")
        self.assertEqual((reviewer["pending"], reviewer["acknowledged"]), (0, 1))
        self.assertEqual(reviewer["pending_event_ids"], [])
        self.assertEqual(reviewer["acknowledged_event_ids"], [current_event_id])
        roles = dict(self.subscribed_config().roles)
        roles["searcher"] = dataclasses.replace(roles["searcher"], work_item_events=("dropped",))
        bounded = events.progress(self.store, roles, limit=1)
        self.assertEqual(len(bounded["roles"]), 1)
        self.assertTrue(bounded["truncated"])
        self.assertFalse(bounded["coverage_complete"])


    def test_closed_cli_exact_receipts_outside_summary_sample(self):
        text = self.file.read_text()
        for name, route in (("worker", "own"), ("reviewer", "own"), ("searcher", "any")):
            text = text.replace(f"[roles.{name}]", f'[roles.{name}]\nwork_item_events = ["dropped"]\n'
                                f'work_item_sources = "{route}"')
        self.file.write_text(text)
        for n in range(12):
            self.terminal(timestamp=f"2030-01-{n+1:02d}T00:00:00+00:00")
        rows = self.page(limit=100)["events"]
        events.acknowledge(self.store, "reviewer", rows)
        # The all-source role has a receipt for only the oldest event.
        events.acknowledge(self.store, "searcher", rows[:1])
        before = {p.relative_to(self.project.root): (p.stat().st_mtime_ns, p.read_bytes())
                  for p in self.project.root.rglob("*") if p.is_file()}
        args = parser().parse_args(["--config", str(self.file), "work-items", self.project.name, "--closed", "100"])
        with patch("mizu.work_items.lock", side_effect=AssertionError("read lock")), \
                patch("mizu.work_items.WorkItems._recover_temporary_writes", side_effect=AssertionError("recovery")), \
                patch.object(events, "write_json", side_effect=AssertionError("read write")):
            output = execute(args)
        after = {p.relative_to(self.project.root): (p.stat().st_mtime_ns, p.read_bytes())
                 for p in self.project.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        summary = next(row for row in output["event_delivery"]["roles"] if row["role"] == "reviewer")
        self.assertEqual(summary["acknowledged"], 12)
        self.assertEqual(len(summary["acknowledged_event_ids"]), 10)
        self.assertTrue(summary["acknowledged_ids_truncated"])
        self.assertNotIn(rows[0]["event"], summary["acknowledged_event_ids"])
        closed = output["closed"]["items"]
        self.assertEqual(closed[-1]["id"], rows[0]["id"])
        for row in closed:
            self.assertEqual(row["event_acknowledgements"],
                             {"reviewer": True, "searcher": row["id"] == rows[0]["id"]})
            self.assertEqual(list(row["event_acknowledgements"]), ["reviewer", "searcher"])
        # Transition actor worker is not routed by own; done-only is not routed.
        text = text.replace('[roles.searcher]\nwork_item_events = ["dropped"]',
                            '[roles.searcher]\nwork_item_events = ["done"]')
        self.file.write_text(text)
        output = execute(args)
        self.assertEqual(output["closed"]["items"][-1]["event_acknowledgements"], {"reviewer": True})
        # Once the submitter is unknown, only any can match (not actor worker).
        (self.project.root / "inbox" / f"{rows[0]['origin']['insight_id']}.json").unlink()
        output = execute(args)
        self.assertEqual(output["closed"]["items"][-1]["event_acknowledgements"], {})
        self.file.write_text(text.replace('[roles.searcher]\nwork_item_events = ["done"]',
                                          '[roles.searcher]\nwork_item_events = ["dropped"]'))
        self.assertEqual(execute(args)["closed"]["items"][-1]["event_acknowledgements"], {"searcher": True})

    def test_closed_acknowledgements_bounds_and_invalid_receipts_fail_closed(self):
        _, item = self.terminal()
        roles = self.subscribed_config().roles
        base = self.store.page_closed_readonly()
        with patch.object(events, "MAX_CLOSED_PAGE_BYTES", len(canonical(base))):
            with self.assertRaises(LimitExceeded):
                events.closed_page(self.store, roles)
        # More than the summary's 100 roles must not be silently omitted.
        many = {f"role-{n:04d}": dataclasses.replace(roles["reviewer"], work_item_sources="any")
                for n in range(120)}
        result = events.closed_page(self.store, many)
        self.assertEqual(len(result["items"][0]["event_acknowledgements"]), 120)
        payload_size = len(canonical(self.page()["events"][0]))
        with patch.object(events, "MAX_CLOSED_ROW_BYTES", payload_size):
            with self.assertRaises(LimitExceeded):
                events.closed_page(self.store, many)
        events.acknowledge(self.store, "reviewer", self.page()["events"])
        receipt = self.project.root / "work-item-receipts/reviewer" / f"{item['id']}.2.json"
        value = read_json(receipt)
        value["event"] = f"work_item:{item['id']}:1"
        write_json(receipt, value)
        with self.assertRaises(Denied):
            events.closed_page(self.store, roles)

    def test_receipts_survive_backup_restore(self):
        from mizu.storage import backup, restore
        from mizu.project import Project
        self.terminal()
        events.acknowledge(self.store, "reviewer", self.page()["events"])
        self.project.set_control(paused=True)
        archive = self.root / "backup.tar.gz"
        backup(self.project, archive)
        restore(self.config, "restored", archive)
        restored = Project(self.config, "restored", read_only=True)
        page = events.pending(restored.work_items, "reviewer", ("dropped",))
        self.assertEqual((page["pending"], page["acknowledged"]), (0, 1))

    def test_result_commit_failure_does_not_ack(self):
        self.terminal()
        from mizu import runtime
        real_write = runtime.write_json
        def fail_completion(path, value, **kwargs):
            if path.name == "result.json" and value.get("status") == "completed":
                raise OSError("injected completion durability failure")
            return real_write(path, value, **kwargs)
        with patch.object(runtime, "write_json", side_effect=fail_completion), \
                patch.object(events, "acknowledge") as ack:
            with self.assertRaises(OSError):
                Engine(self.subscribed_config(), driver=ScriptDriver()).run(
                    self.project, "reviewer", event_trigger=True)
        ack.assert_not_called()
        self.assertEqual(self.page()["pending"], 1)

    def test_any_source_unknown_routes_admits_once_without_decision_subscription(self):
        proposal, _ = self.terminal(source="searcher")
        (self.project.root / "inbox" / f"{proposal['id']}.json").unlink()
        own = Engine(self.subscribed_config(), driver=ScriptDriver())
        self.assertEqual(own.run(self.project, "reviewer", event_trigger=True)["skipped"], "no-event")
        engine = Engine(self.subscribed_config(work_item_sources="any"), driver=ScriptDriver())
        self.assertEqual(engine.run(self.project, "reviewer", event_trigger=True)["status"], "completed")
        self.assertEqual(self.page(sources="any")["acknowledged"], 1)
        self.assertEqual(engine.run(self.project, "reviewer", event_trigger=True)["skipped"], "no-event")

    def test_legacy_coverage_uses_reconciled_seed_and_never_recovers_reads(self):
        event_id = "a" * 32
        legacy = {"id": "legacy-origin", "action": "accept", "rev": 1,
                  "reason": "Legacy assessment", "revisit": "", "run": "legacy-run",
                  "created_at": "2024-01-01T00:00:00+00:00"}
        write_json(self.project.root / "decision-history" / f"{event_id}.json", legacy)
        page = self.page(sources="any")
        self.assertEqual((page["unseeded_decisions"], page["coverage_complete"]), (1, False))
        self.assertEqual(page["events"], [])
        self.store.reconcile(event_id, expected_action="accept", outcome="open",
                             reason="Still actionable", title="Retained work", actor="worker")
        terminal = self.store.transition(f"wi_{event_id}", expected_rev=1, status="dropped",
                                         actor="worker", reason="Superseded")
        # WorkItems validates its atomic-write leftovers in readonly mode;
        # the event adapter must not invent a weaker glob/recovery path.
        temporary = self.project.root / "work-items" / terminal["id"] / ".new-abcdefgh"
        temporary.write_text("uncommitted")
        before = temporary.read_bytes()
        with patch.object(self.store, "_recover_temporary_writes", side_effect=AssertionError("recovery")):
            page = self.page(sources="any")
        self.assertTrue(page["coverage_complete"])
        self.assertEqual(page["events"][0]["id"], terminal["id"])
        self.assertEqual(page["events"][0]["routing"], "unknown")
        self.assertEqual(temporary.read_bytes(), before)

    def test_bounded_dispatch_drain_excludes_receipts_before_limit(self):
        for _ in range(3):
            self.terminal()
        config = self.subscribed_config()
        config = dataclasses.replace(config, limits=dataclasses.replace(config.limits, decision_events_limit=1))
        self.project.config = config
        driver = ScriptDriver()
        engine = Engine(config, driver=driver)
        selected = []
        for remaining in (2, 1, 0):
            result = engine.run(self.project, "reviewer", event_trigger=True)
            self.assertEqual(result["status"], "completed")
            page = read_json(driver.calls[-1][0].run_dir / "work-item-events.json")
            selected.append(page["events"][0]["event"])
            self.assertEqual(len(page["events"]), 1)
            self.assertEqual(page["overflow_count"], remaining)
            self.assertEqual(self.page()["pending"], remaining)
        self.assertEqual(len(set(selected)), 3)
        self.assertEqual(engine.run(self.project, "reviewer", event_trigger=True)["skipped"], "no-event")

    def test_subscription_and_limit_bounds_fail_closed(self):
        for actions in (("doing",), ("done", "done")):
            with self.assertRaises(Denied):
                self.page(actions=actions)
        for limit in (0, 101, True, 1.5):
            with self.assertRaises(Denied):
                self.page(limit=limit)
        with self.assertRaises(Denied):
            self.page(sources="actor")
