"""Offline-input materialization: generic host adapter, fail closed. Offline only."""
import json
import sys
import unittest

from support import Fixture
from mizu import materialize
from mizu.config import load
from mizu.errors import Denied, ConfigError
from mizu.fs import canonical, digest


def settings(command, timeout=5, maximum=524288):
    return {"command": command, "timeout_seconds": timeout, "max_bytes": maximum}


def good_adapter_code(content="lockfile-content", cache="warm-cache-1"):
    # Adapter computes digest locally like a correct implementation would.
    return (
        "import sys,json,hashlib;"
        "json.load(sys.stdin);"
        f"content={content!r};"
        "from pathlib import Path;"
        "import sys; sys.path.insert(0, 'src');"
        "from mizu.fs import canonical, digest;"
        f"print(json.dumps({{'content': content, 'digest': digest(canonical(content)), 'cache': {cache!r}}}))"
    )


class MaterializeAdapterTests(Fixture):
    def test_success_returns_digest_bound_content(self):
        out = materialize.invoke(
            settings([sys.executable, "-c", good_adapter_code()]), "need cargo cache for manifests")
        self.assertEqual(out["content"], "lockfile-content")
        self.assertEqual(out["digest"], digest(canonical("lockfile-content")))
        self.assertEqual(out["cache"], "warm-cache-1")
        self.assertEqual(out["trust"], "external-untrusted")

    def test_unconfigured_refused(self):
        with self.assertRaises(Denied):
            materialize.invoke(settings([]), "need something")

    def test_bad_spec_refused_without_spawn(self):
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", "pass"]), "")
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", "pass"]), "   ")
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", "pass"]), "x" * 8193)

    def test_digest_mismatch_refused(self):
        code = "import sys,json; json.load(sys.stdin); print(json.dumps({'content': 'a', 'digest': 'b'*64}))"
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", code]), "need deps")

    def test_nonzero_exit_refused(self):
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", "import sys; sys.exit(3)"]), "need deps")

    def test_malformed_json_refused(self):
        code = "import sys; sys.stdout.write('not-json\\n')"
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", code]), "need deps")

    def test_config_defaults_absent_section(self):
        # Existing configs without [materialize] keep loading, disabled.
        self.assertEqual(self.config.materialize.get("command"), [])
        self.assertEqual(self.config.materialize.get("timeout_seconds"), 20)

    def test_config_unknown_key_refused(self):
        text = self.file.read_text()
        self.assertIn("[materialize]", text)
        self.file.write_text(text.replace("[materialize]", "[materialize]\nunknown_key = 1", 1))
        with self.assertRaisesRegex(ConfigError, "Unknown keys"):
            load(self.file)

    def test_runtime_requires_visible_workspace(self):
        ctx = self.context("worker")
        # Unconfigured adapter fails closed even with the grant path wired.
        from mizu.config import Role
        role = ctx.role
        # worker has write workspace; call without adapter configured
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"spec": "need test cache"})

    def test_consult_cannot_hold_materialize(self):
        from mizu.runtime import check_consult_role
        from mizu.config import Role
        from pathlib import Path
        role = Role("consult", "primary", (Path("x"),), "read", ("files", "read", "finish", "materialize"))
        with self.assertRaises(ConfigError):
            check_consult_role("consult", role)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
