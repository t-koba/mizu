from __future__ import annotations
import contextlib
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
_default_config = None
_default_config_temporary = None


def default_config_tree() -> Path:
    """Create the real default configuration tree once for this test process."""
    global _default_config, _default_config_temporary
    if _default_config is None:
        _default_config_temporary = tempfile.TemporaryDirectory(prefix="mizu-test-config-")
        _default_config = Path(_default_config_temporary.name) / "config" / "config.toml"
        configure(_default_config, None)
    return _default_config.parent
sys.path.insert(0, str(ROOT / "src"))
from mizu.cli import configure
from mizu.errors import Denied
from mizu.config import load
from mizu.fs import canonical, mkdir
from mizu.process import run
from mizu.project import initialize
from mizu.runtime import Context


def write_fixture_json(path: Path, value: object) -> None:
    """Write an exclusive JSON fixture without exercising production durability."""
    with path.open("xb") as stream:
        stream.write(canonical(value))


def forget_ledger(root: Path) -> None:
    """Drop the derived work-item index after a fixture edits history files directly.

    Product writes keep the index current; history written or changed outside
    them is only seen after a rebuild, which validates every chain again.
    """
    from mizu.ledger import NAME
    for suffix in ("", "-wal", "-shm"):
        (Path(root) / (NAME + suffix)).unlink(missing_ok=True)


def history_write_json(path, value, **kwargs):
    """Write a record outside the product's writers, as an older release or an edit would.

    The project's derived index is dropped so the next read rebuilds it from
    the files and validates them again.
    """
    from mizu.fs import write_json
    from mizu.ledger import NAME
    write_json(path, value, **kwargs)
    for ancestor in Path(path).parents:
        if (ancestor / NAME).exists():
            forget_ledger(ancestor)


def index_items(root, items, *, replace=False):
    """Put synthetic current records straight into the derived index (scale tests)."""
    import sqlite3
    from mizu.ledger import NAME
    from mizu.work_item_index import COLUMNS
    from mizu.work_items import _timestamp_order
    rows = [(item["id"], item["rev"], item["status"], item["holder"],
             _timestamp_order(item["created_at"]), _timestamp_order(item["updated_at"]),
             (item["origin"] or {}).get("insight_id"), (item["origin"] or {}).get("rev"),
             (item.get("wait") or {}).get("kind"), None, item.get("blocked_since"), None,
             canonical(item).decode()) for item in items]
    with contextlib.closing(sqlite3.connect(Path(root) / NAME)) as db, db:
        if replace:
            db.execute("DELETE FROM items")
        db.executemany(f"INSERT OR REPLACE INTO items({', '.join(COLUMNS)}) "
                       f"VALUES ({', '.join('?' * len(COLUMNS))})", rows)


def without_blocked_age(value):
    """Copy a projection without its intentionally per-read age samples."""
    if isinstance(value, dict):
        return {key: without_blocked_age(item) for key, item in value.items()
                if key != "blocked_age_seconds"}
    if isinstance(value, list):
        return [without_blocked_age(item) for item in value]
    return value


def host_child_cost() -> float:
    """Return this host's measured cost of starting and reaping a child.

    The deadline tests check promptness: the call must end because the
    configured bound ended it, not because the wall clock ran out. A fixed
    number of seconds cannot say that on an unknown host, so the test
    measures what this host spends on a trivial child of the same
    interpreter and scales its bound with that measurement.
    """
    started = time.monotonic()
    run([sys.executable, "-c", "pass"], timeout=60, maximum=1024)
    return time.monotonic() - started


def bind_delegation_fixture(store):
    """Bind fake resources for gate tests; NOT process/OCI cleanup evidence."""
    if not hasattr(os, 'getuid'):
        raise unittest.SkipTest('User-manager binding fixture requires a POSIX uid')
    reservation = store.reservation()
    directory = (store.root.parent / reservation['run_id']).resolve()
    directory.mkdir(exist_ok=True)
    from mizu.delegation_jobs import user_cgroup
    unit = 'mizu-delegation-' + reservation['run_id'] + '.service'
    binding = store.runner_bind(reservation['id'], reservation['token'], str(directory),
                                {'unit': unit, 'cgroup': user_cgroup(unit)}, [], authority_digest='b' * 64)
    return {'binding': binding, 'scope_clean': True, 'containers_absent': []}


def seed_terminal_delegation_fixture(store, job_id, token, result):
    """Seed terminal ledger state, NOT a supported handback or cleanup proof.

    Only tests use this to exercise terminal reads, retention and access gates;
    production runner_quiescent and runner_prepared_quiescent must refuse even
    fabricated positive evidence. For a fenced prepared recovery (recovering
    intent, cancel-requested, no execution) the TEST seed records a failed
    outcome; for post-execution it retains the reported execution outcome.
    The TEST seed is explicit test-only state, never product handback evidence.
    """
    from mizu.delegation_jobs import MAX_RESULT_BYTES, _text
    _text(result, MAX_RESULT_BYTES)
    with store._locked():
        state = store._load()
        job = store._runner(state, job_id, token)
        if job['execution'] is None:
            intent = job['intent']
            assert intent is not None and intent['recovering'] and intent['id'] is not None
            assert not intent['claimed'] and job['status'] == 'cancel-requested'
            outcome = 'failed'
        else:
            outcome = job['execution']
        store._terminal(state, job, result, outcome)
        store._save(state)


def bind_delivery(project, item_id, snapshot_id, *, expected_rev=1):
    """Record the two explicit lifecycle revisions needed by completion fixtures."""
    store = project.work_items
    if expected_rev == 1:
        store.transition(item_id, expected_rev=1, status="doing", actor="worker")
        expected_rev = 2
    return store.transition(item_id, expected_rev=expected_rev, status="doing", actor="worker",
                            snapshot_id=snapshot_id)


def set_project_target(fixture, target):
    """Change trusted fixture metadata and reopen the project."""
    from mizu.project import Project
    path = fixture.project.root / "project.toml"
    text = path.read_text().split("\n[vcs.lineage_target]\n")[0]
    if target is not None:
        text += "\n[vcs.lineage_target]\n" + "".join(
            f"{key} = {json.dumps(value)}\n" for key, value in target.items())
    path.write_text(text)
    fixture.project = Project(fixture.config, fixture.project.name)


class Fixture(unittest.TestCase):
    @contextlib.contextmanager
    def capped(self, retained=1):
        """Run with the retained check-job cap lowered, so eviction is needed."""
        import dataclasses
        original = self.project.config
        self.project.config = dataclasses.replace(
            original, limits=dataclasses.replace(original.limits, check_retained=retained))
        try:
            yield
        finally:
            self.project.config = original

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mizu-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.file = self.root / "config/config.toml"
        shutil.copytree(default_config_tree(), self.file.parent)
        text = self.file.read_text().replace('data_dir = "~/.local/state/mizu"', 'data_dir = ' + json.dumps(str(self.root / "data")))
        text = text.replace('provider = ""', 'provider = "test-provider"').replace('model = ""', 'model = "test-model"')
        text = text.replace('\ndaily_requests = 0', '\ndaily_requests = 100').replace('free_disk_mb = 1024', 'free_disk_mb = 0')
        text = text.replace('image = ""', 'image = "sha256:' + 'a' * 64 + '"')
        self.file.write_text(text)
        self.config = load(self.file)
        source = self.root / "source"
        source.mkdir()
        # Byte-exact fixture: text-mode writes would translate LF to CRLF on
        # Windows, breaking exact snapshot/export assertions on every host.
        (source / "app.py").write_bytes(b"VALUE = 2\n")
        self.goal = self.root / "GOAL.md"
        self.goal.write_text("Preserve correctness. Improve only with evidence.\n")
        self.project = initialize(self.config, "sample", source, self.goal,
                                  ["worker", "searcher", "reviewer", "reporter", "consult"], ["python3 -m unittest"])
        self.project.set_control(armed=True, paused=False, wake_generation="test-wake")

    def context(self, name="worker", *, reservation_view=None):
        import uuid
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        role = self.config.roles[name]
        snap = self.project.snapshots.get()
        workspace = self.project.workspace
        if role.workspace != "write":
            workspace = run / "input"
            self.project.snapshots.materialize(snap, workspace)
        return Context(self.config, self.project, role, run, snap, workspace,
                       reservation_view=reservation_view)

    def admitted_call(self, engine, operation, args):
        """Exercise a child API from an Engine.run caller, never forge a lease.

        A separate fake parent driver keeps child-driver observations intact;
        the read-only reporter caller does not publish or skip unchanged code.
        """
        from mizu.runtime import Engine
        response = None
        def work(ctx, *_):
            nonlocal response
            response = getattr(engine, operation)(ctx, args)
        Engine(engine.config, driver=ScriptDriver(work), stop=engine.stop).run(
            self.project, "reporter")
        return response


class ScriptDriver:
    #: Test double: executes no commands, so Engine skips host sandbox checks.
    requires_sandbox = False

    def __init__(self, callback=None):
        self.callback = callback
        self.calls = []

    def execute(self, context, prompt, *, profile=None):
        self.calls.append((context, prompt, profile))
        context.handle("_hello", {})
        context.handle("_budget", {"sequence": 1})
        if self.callback:
            self.callback(context, prompt, profile)
        if context.finished is None:
            try:
                context.handle("finish", {"outcome": "wait", "summary": "One observed result", "state": "Next step is explicit."})
            except Denied as exc:
                # Only the explicit refusal naming the reached-bound wrap-up
                # is expected here. A bound does not excuse a different
                # product refusal (for example a missing state or an
                # unrepresentable workspace); the double must fail loudly.
                if context.bound_hit is None or str(exc) != context._bound_message():
                    raise
        if not context.operations_stopped.is_set():
            for attempt, sequence in tuple(context.request_deadlines):
                if attempt == context.execution_attempt:
                    context.handle("_request_done", {"sequence": sequence})
        return {"requests": context.request_count, "profile": profile or context.role.profile}


def delivered_ack_tokens(prompt):
    """Fake roles handle supported token rows, never CI waits or job/status data."""
    data = json.loads(prompt)
    return [row["ack_token"]
            for source in ("decision_events", "work_item_events", "wait_events", "obligations")
            for row in data.get(source, []) if "ack_token" in row]


def finish_with_event_acks(context, prompt, profile=None):
    """Test role explicitly handles the events in its delivered prompt."""
    context.handle("finish", {"outcome": "wait", "summary": "Handled delivered events",
                              "state": "Next step is explicit.",
                              "ack_events": delivered_ack_tokens(prompt)})


# Existing decision-focused tests share the same explicit delivered-row contract.
finish_with_decision_acks = finish_with_event_acks
