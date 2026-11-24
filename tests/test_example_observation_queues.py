"""Stage-5 example userland: observation queue declarations and policy.

The example files are declarative only: adding them changes no runtime
authority. These tests check that the declarations validate, that their schemas
enforce the stated bounds, and that a host-authenticated driver can append an
observation the way the generic kernel driver does.
"""
import tempfile
import tomllib
import unittest
from pathlib import Path

import support  # noqa: F401  (puts src on the path)
from mizu.cli import configure
from mizu.config import load
from mizu.errors import Denied
from mizu.kernel import QueueStore, parse_queues
from mizu.kernel.queues import allows

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "development"
FRAGMENT = EXAMPLE / "observation_queues.toml"
POLICY = EXAMPLE / "policies" / "observations.md"
README = EXAMPLE / "README.md"


def declarations():
    return tomllib.loads(FRAGMENT.read_text(encoding="utf-8"))["queues"]


OBSERVATION = {
    "driver": "example-driver",
    "kind": "revision",
    "summary": "a new revision was observed",
    "observed_at": "2026-01-01T00:00:00+00:00",
    "digest": "a" * 64,
    "detail": "bounded evidence detail",
}


class ObservationDeclaration(unittest.TestCase):
    def test_both_queues_validate_with_bounded_credentials(self):
        raw = declarations()
        self.assertEqual(set(raw), {"vcs_observations", "ci_observations"})
        queues = parse_queues(raw)
        for name in ("vcs_observations", "ci_observations"):
            queue = queues[name]
            # A driver appends as host, so host must hold the write grant.
            self.assertTrue(allows(queue.write, "host"), name)
            # Readers and the wake target are declared; no wildcard wake.
            self.assertIn("role:*", queue.read)
            self.assertEqual(queue.wake, ("role:worker",))
            fields = queue.fields
            self.assertTrue(fields["driver"].required)
            self.assertTrue(fields["kind"].required)
            self.assertTrue(fields["summary"].required)
            self.assertTrue(fields["observed_at"].required)
            self.assertFalse(fields["digest"].required)
            self.assertEqual(fields["driver"].max, 80)
            self.assertEqual(fields["summary"].max, 4096)
            self.assertEqual(fields["detail"].max, 16384)
            self.assertEqual(fields["digest"].type, "digest")

    def test_readme_include_loads_through_the_real_config_loader(self):
        # The README composes all three fragments; prove that include graph
        # validates with a real top config and leaves drivers default-closed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfgdir = root / "config"
            cfgdir.mkdir()
            top = cfgdir / "config.toml"
            configure(top, None)
            lines = top.read_text(encoding="utf-8").splitlines(keepends=True)
            kept, in_queues = [], False
            for line in lines:
                stripped = line.lstrip()
                if stripped.startswith("[queues"):
                    in_queues = True
                if in_queues:
                    if stripped.startswith("[") and not stripped.startswith("[queues"):
                        in_queues = False
                    else:
                        continue
                if not in_queues:
                    kept.append(line)
            text = "".join(kept).replace(
                'data_dir = "~/.local/state/mizu"',
                "data_dir = " + repr(str(root / "data")))
            names = ("queues.toml", "outcomes.toml", "observation_queues.toml")
            # Copy the fragments beside the top config and include them by bare
            # relative name, as the README documents. A cross-tree os.path.relpath
            # breaks on platform temp symlinks (macOS /var -> /private/var), where
            # the lexically computed ".." count does not match the resolved path.
            for name in names:
                (cfgdir / name).write_bytes((EXAMPLE / name).read_bytes())
            include = "include = [" + ", ".join('"%s"' % n for n in names) + "]\n"
            top.write_text(include + text, encoding="utf-8")
            config = load(top)
            self.assertEqual(set(config.queues), {
                "proposals", "questions", "notices", "work",
                "vcs_observations", "ci_observations"})
            self.assertEqual(config.drivers, {})
            for name in ("vcs_observations", "ci_observations"):
                self.assertIn("host", config.queues[name].write)

    def test_fragment_introduces_only_the_queues_root_key(self):
        # No new root owner (and no [drivers] table / real driver command):
        # the example stays declarative and changes no runtime authority.
        data = tomllib.loads(FRAGMENT.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"queues"})
        self.assertNotIn("command", FRAGMENT.read_text(encoding="utf-8"))

    def test_schema_bounds_are_enforced_on_host_append(self):
        queues = parse_queues(declarations())
        with tempfile.TemporaryDirectory() as tmp:
            store = QueueStore(Path(tmp), queues)
            for name in ("vcs_observations", "ci_observations"):
                ack = store.append(name, dict(OBSERVATION), "host")
                self.assertEqual(ack["seq"], 1)
                self.assertIn("id", ack)
                with self.assertRaises(Denied):
                    store.append(name, {**OBSERVATION, "summary": "x" * 5000}, "host")
                with self.assertRaises(Denied):
                    missing = {k: v for k, v in OBSERVATION.items() if k != "summary"}
                    store.append(name, missing, "host")
                with self.assertRaises(Denied):
                    store.append(name, {**OBSERVATION, "undeclared": 1}, "host")
                # A reader role may read the observation back.
                page = store.read(name, "role:worker")
                self.assertEqual(len(page["messages"]), 1)

    def test_policy_and_readme_describe_authority_neutrally(self):
        policy = POLICY.read_text(encoding="utf-8")
        for queue in ("vcs_observations", "ci_observations"):
            self.assertIn(queue, policy)
        self.assertIn("external-untrusted", policy)
        self.assertIn("authoritative", policy)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("observation_queues.toml", readme)
        self.assertIn("policies/observations.md", readme)
        self.assertIn("changes no runtime authority", readme)


if __name__ == "__main__":
    unittest.main()
