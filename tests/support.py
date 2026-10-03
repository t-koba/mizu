from __future__ import annotations
import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mizu.cli import configure
from mizu.config import load
from mizu.fs import mkdir
from mizu.project import initialize
from mizu.runtime import Context


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mizu-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.file = self.root / "config/config.toml"
        configure(self.file, None)
        text = self.file.read_text().replace('data_dir = "~/.local/state/mizu"', 'data_dir = ' + json.dumps(str(self.root / "data")))
        text = text.replace('provider = ""', 'provider = "test-provider"').replace('model = ""', 'model = "test-model"')
        text = text.replace('daily_requests = 0', 'daily_requests = 100').replace('free_disk_mb = 1024', 'free_disk_mb = 0')
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
            context.handle("finish", {"outcome": "wait", "summary": "One observed result", "state": "Next step is explicit."})
        return {"requests": context.request_count, "profile": profile or context.role.profile}
