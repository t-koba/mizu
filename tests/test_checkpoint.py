"""Podman checkpoint-image refusal: flags would be silently ignored (CVE-2026-94603)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ROOT  # noqa: F401  (keeps sys.path contract identical to other suites)

from mizu.errors import ConfigError, Denied
from mizu.process import Result
from mizu.sandbox import (
    CHECKPOINT_ANNOTATION,
    assert_no_checkpoint,
    inspect_text_is_checkpoint,
)

MARKER = CHECKPOINT_ANNOTATION


def _config(tmp: str, executable="podman"):
    import json as _json
    from mizu.cli import configure
    from mizu.config import load
    root = Path(tmp)
    config_file = root / "config.toml"
    configure(config_file, None)
    text = config_file.read_text().replace('data_dir = "~/.local/state/mizu"',
                                            'data_dir = ' + _json.dumps(str(root / "data")))
    text = text.replace('provider = ""', 'provider = "p"').replace('model = ""', 'model = "m"')
    text = text.replace('\\ndaily_requests = 0', '\\ndaily_requests = 1').replace('free_disk_mb = 1024', 'free_disk_mb = 0')
    text = text.replace('image = ""', 'image = "sha256:' + 'a' * 64 + '"')
    text = text.replace('executable = "podman"', f'executable = "{executable}"')
    config_file.write_text(text)
    return load(config_file)


class CheckpointHelperTests(unittest.TestCase):
    def test_clean_inspect_is_allowed(self):
        self.assertFalse(inspect_text_is_checkpoint("[]"))
        self.assertFalse(inspect_text_is_checkpoint(json.dumps([{"Id": "sha256:x", "Annotations": {}}])))

    def test_nested_annotation_key_is_refused(self):
        payload = json.dumps([{"Id": "sha256:x", "Annotations": {MARKER: "runc"}}])
        self.assertTrue(inspect_text_is_checkpoint(payload))
        nested = json.dumps({"Manifest": {"annotations": {MARKER: "runc"}}})
        self.assertTrue(inspect_text_is_checkpoint(nested))

    def test_unparseable_output_falls_back_to_substring(self):
        self.assertTrue(inspect_text_is_checkpoint("not-json " + MARKER))
        self.assertFalse(inspect_text_is_checkpoint("not-json without the marker"))


class CheckpointExecuteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mizu-checkpoint-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_execute_refuses_checkpoint_image(self):
        from mizu.sandbox import Sandbox
        config = _config(self.temp.name)
        work = self.root / "work"
        work.mkdir()
        box = Sandbox(config, self.root, "worker", self.root / "run")
        checkpoint = json.dumps([{"Id": "sha256:x", "Annotations": {MARKER: "runc"}}])
        with patch("mizu.sandbox.run",
                   side_effect=[Result(0, checkpoint, "", "exited", 0.0),
                                Result(0, "", "", "exited", 0.0)]):
            with self.assertRaises(Denied) as caught:
                box.execute(work, "echo hi", writable=False)
        self.assertIn("checkpoint", str(caught.exception).lower())

    def test_execute_proceeds_for_clean_image(self):
        from mizu.sandbox import Sandbox
        config = _config(self.temp.name)
        work = self.root / "work"
        work.mkdir()
        run_dir = self.root / "run"
        run_dir.mkdir()
        box = Sandbox(config, self.root, "worker", run_dir)
        calls = [Result(0, "[]", "", "exited", 0.0),
                 Result(0, "ok", "", "exited", 0.1),
                 Result(0, "", "", "exited", 0.0)]
        with patch("mizu.sandbox.run", side_effect=calls):
            record = box.execute(work, "echo hi", writable=False)
        self.assertEqual(record["exit_code"], 0)

    def test_infra_failure_does_not_mask_as_checkpoint(self):
        from mizu.sandbox import Sandbox
        config = _config(self.temp.name, executable="mizu-missing-runtime-xyz")
        work = self.root / "work"
        work.mkdir()
        box = Sandbox(config, self.root, "worker", self.root / "run")
        # Inspect cannot run (missing runtime) so the preflight yields to the
        # normal path, which records its own startup evidence.
        with patch("mizu.sandbox.run",
                   side_effect=[Result(None, "", "missing", "startup_error", 0),
                                Result(None, "", "missing", "startup_error", 0),
                                Result(0, "", "", "exited", 0)]):
            record = box.execute(work, "echo hi", writable=False)
        self.assertEqual(record["reason"], "startup_error")

    def test_assert_no_checkpoint_needs_image(self):
        import dataclasses
        config = _config(self.temp.name)
        empty = dataclasses.replace(config, sandbox=dataclasses.replace(config.sandbox, image=""))
        with self.assertRaises(ConfigError):
            assert_no_checkpoint(empty, {})


if __name__ == "__main__":
    raise SystemExit(unittest.main())
