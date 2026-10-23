from __future__ import annotations
import dataclasses
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mizu.cli import configure
from mizu.errors import Denied
from mizu.config import load
from mizu.fs import mkdir
from mizu.process import run
from mizu.project import initialize
from mizu.runtime import Context


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


def bind_delivery(project, item_id, snapshot_id, *, expected_rev=1):
    """Record the two explicit lifecycle revisions needed by completion fixtures."""
    store = project.work_items
    if expected_rev == 1:
        store.transition(item_id, expected_rev=1, status="doing", actor="worker")
        expected_rev = 2
    return store.transition(item_id, expected_rev=expected_rev, status="doing", actor="worker",
                            delivery_snapshot_id=snapshot_id)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mizu-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.file = self.root / "config/config.toml"
        configure(self.file, None)
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

    def context(self, name="worker"):
        import uuid
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        role = self.config.roles[name]
        snap = self.project.snapshots.get()
        workspace = self.project.workspace
        if role.workspace != "write":
            workspace = run / "input"
            self.project.snapshots.materialize(snap, workspace)
        return Context(self.config, self.project, role, run, snap, workspace)

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
            except Denied:
                # The engine returns a refused seal as a tool result and lets
                # the unit wrap up under the bound it reached; the double
                # does the same for a reached bound, so a bound is not an
                # unhandled error here. Every other refusal — an empty
                # summary, an unknown profile, a missing state, an
                # unrepresentable workspace — still fails the test loudly.
                if context.bound_hit is None:
                    raise
        if not context.operations_stopped.is_set():
            for attempt, sequence in tuple(context.request_deadlines):
                if attempt == context.execution_attempt:
                    context.handle("_request_done", {"sequence": sequence})
        return {"requests": context.request_count, "profile": profile or context.role.profile}


def finish_with_decision_acks(context, prompt, profile=None):
    """Test role explicitly handles every decision in its delivered prompt."""
    rows = json.loads(prompt)["decision_events"]
    context.handle("finish", {"outcome": "wait", "summary": "Handled delivered decisions",
                              "state": "Next step is explicit.",
                              "ack_events": [row["ack_token"] for row in rows]})
