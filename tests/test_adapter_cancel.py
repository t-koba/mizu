"""Run stop reaches VCS and materialize adapters through cancel."""
import sys
import time
import unittest
from unittest.mock import patch

from support import Fixture
from mizu.errors import Denied


class Sentinel(Exception):
    pass


def boom(*args, **kwargs):
    boom.kwargs = kwargs
    raise Sentinel()


def vcs_settings(command=None, timeout=5):
    return {"command": command if command is not None else [sys.executable, "-c", "pass"],
            "timeout_seconds": timeout, "max_bytes": 524288}


class AdapterCancelForwardingTests(unittest.TestCase):
    def test_vcs_invoke_forwards_cancel(self):
        from mizu import vcs
        with patch.object(vcs, "run", boom):
            with self.assertRaises(Sentinel):
                vcs.invoke(vcs_settings(), {"op": "status"}, cancel=lambda: True)
            self.assertTrue(boom.kwargs["cancel"]())
            with self.assertRaises(Sentinel):
                vcs.invoke(vcs_settings(), {"op": "status"})
            self.assertFalse(boom.kwargs["cancel"]())

    def test_materialize_invoke_forwards_cancel(self):
        from mizu import materialize
        with patch.object(materialize, "run", boom):
            with self.assertRaises(Sentinel):
                materialize.invoke(vcs_settings(), "need deps", cancel=lambda: True)
            self.assertTrue(boom.kwargs["cancel"]())

    def test_read_via_forwards_cancel(self):
        from mizu import vcs
        with patch.object(vcs, "run", boom):
            with self.assertRaises(Sentinel):
                vcs.read_via(vcs_settings(), "proposals", {}, cancel=lambda: True)
            self.assertTrue(boom.kwargs["cancel"]())

    def test_mutating_helpers_forward_cancel(self):
        from mizu import vcs
        sha = "a" * 40
        cases = [
            (vcs.fetch_refs, (vcs_settings(),), {}),
            (vcs.retire_via, (vcs_settings(), "mizu/main", sha),
             {"retire_grant": True, "owned_prefixes": ["mizu/"]}),
            (vcs.dispose_via, (vcs_settings(), "close", "pr-1", sha, sha, "main"), {"close_grant": True}),
        ]
        for func, args, extra in cases:
            with self.subTest(func=func.__name__):
                settings = dict(args[0])
                settings.update(extra)
                full_args = (settings,) + args[1:]
                with patch.object(vcs, "run", boom):
                    with self.assertRaises(Sentinel):
                        func(*full_args, cancel=lambda: True)
                    self.assertTrue(boom.kwargs["cancel"]())

    def test_close_if_adopted_forwards_cancel(self):
        from mizu import vcs
        pins = {"id": "pr-1", "head_sha": "a" * 40, "base_sha": "b" * 40,
                "target_repo": "forge:owner/repo", "target_ref": "main",
                "target_sha": "c" * 40}
        settings = vcs_settings()
        settings["close_grant"] = True
        with patch.object(vcs, "run", boom):
            with self.assertRaises(Sentinel):
                vcs.close_if_adopted_via(settings, pins, cancel=lambda: True)
            self.assertTrue(boom.kwargs["cancel"]())

    def test_publish_via_forwards_cancel(self):
        from mizu import vcs
        settings = vcs_settings()
        with patch.object(vcs, "run", boom):
            with self.assertRaises(Sentinel):
                vcs.publish_via(settings, "push",
                                {"branch": "mizu/x", "code_digest": "d" * 64,
                                 "publication_id": "e" * 64, "source_commit": "a" * 40},
                                cancel=lambda: True)
            self.assertTrue(boom.kwargs["cancel"]())


class AdapterCancelWiringTests(Fixture):
    def test_materialize_op_passes_live_cancel(self):
        from mizu import materialize
        seen = {}

        def fake_invoke(settings, spec, **kwargs):
            seen.update(kwargs)
            raise Sentinel()

        import dataclasses
        ctx = self.context("worker")
        ctx.role = dataclasses.replace(ctx.role, capabilities=ctx.role.capabilities + ("materialize",))
        with patch.object(materialize, "invoke", fake_invoke):
            with self.assertRaises(Sentinel):
                ctx.handle("materialize", {"spec": "need deps"})
        self.assertEqual(seen["cancel"], ctx.tools_cancelled)

    def test_vcs_read_op_passes_live_cancel(self):
        from mizu import vcs
        seen = {}

        def fake_read(settings, op, params, **kwargs):
            seen.update(kwargs)
            raise Sentinel()

        import dataclasses
        ctx = self.context("worker")
        ctx.role = dataclasses.replace(ctx.role, capabilities=ctx.role.capabilities + ("vcs_read",))
        with patch.object(vcs, "read_via", fake_read):
            with self.assertRaises(Sentinel):
                ctx.handle("vcs_read", {"op": "status", "branch": "main"})
        self.assertEqual(seen["cancel"], ctx.tools_cancelled)

    def test_stopped_run_abandons_slow_materialize(self):
        import dataclasses
        code = "import time; time.sleep(30)"
        settings = {"command": [sys.executable, "-c", code],
                    "timeout_seconds": 60, "max_bytes": 524288,
                    "diagnostic_bytes": 0}
        config = dataclasses.replace(
            self.config, materialize={**self.config.materialize, **settings})
        import dataclasses
        import threading
        ctx = self.context("worker")
        ctx.config = config
        ctx.role = dataclasses.replace(ctx.role, capabilities=ctx.role.capabilities + ("materialize",))
        threading.Timer(0.2, ctx.stop.set).start()
        started = time.monotonic()
        with self.assertRaisesRegex(Denied, "cancelled"):
            ctx.handle("materialize", {"spec": "need deps"})
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
