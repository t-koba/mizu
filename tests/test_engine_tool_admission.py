"""Run-local pre-send admission contracts; synthetic adapter identities only."""
import dataclasses
from unittest.mock import patch

from support import Fixture
from mizu.errors import Cancelled, Denied, LimitExceeded


class EngineToolAdmissionTests(Fixture):
    def granted_context(self, **role_options):
        ctx = self.context('consult')
        ctx.role = dataclasses.replace(ctx.role, engine_tools=('tool', 'other'), **role_options)
        return ctx

    def admit(self, ctx, identity='sdk-id', name='tool'):
        return ctx.handle('_engine_tool', {'name': name, 'invocation_id': identity})

    def test_same_identity_counts_once_and_new_execution_attempt_counts_again(self):
        ctx = self.granted_context()
        self.assertEqual(self.admit(ctx), {'allowed': True})
        self.assertEqual(self.admit(ctx), {'allowed': True})
        self.assertEqual(ctx.tool_count, 1)
        ctx.execution_attempt += 1
        self.admit(ctx)
        self.assertEqual(ctx.tool_count, 2)
        self.assertEqual(len(ctx.engine_tool_invocations), 2)

    def test_invalid_missing_ids_and_ungranted_names_do_not_count(self):
        ctx = self.granted_context()
        for identity in (None, '', 7, [], {}, 'x' * 257, 'x\n', 'x\x00', 'x\x7f', 'x\x85', 'x\u200b', ' '):
            with self.subTest(identity=identity), self.assertRaises(Denied):
                self.admit(ctx, identity)
        with self.assertRaises(Denied):
            ctx.handle('_engine_tool', {'name': 'tool'})
        for name in ('ungranted', None, [], {}):
            with self.subTest(name=name), self.assertRaises(Denied):
                self.admit(ctx, name=name)
        self.assertEqual(ctx.tool_count, 0)
        self.assertEqual(ctx.engine_tool_invocations, {})
        self.admit(ctx, 'x' * 256)
        self.assertEqual(ctx.tool_count, 1)

    def test_identity_collision_and_revoked_grant_refuse_before_counting(self):
        ctx = self.granted_context()
        self.admit(ctx)
        with self.assertRaisesRegex(Denied, 'different name'):
            self.admit(ctx, name='other')
        ctx.role = dataclasses.replace(ctx.role, engine_tools=())
        with self.assertRaisesRegex(Denied, 'explicit grant'):
            self.admit(ctx)
        self.assertEqual(ctx.tool_count, 1)
        self.assertEqual(ctx.engine_tool_invocations, {(0, 'sdk-id'): 'tool'})

    def test_idempotency_storage_bound_does_not_evict_or_charge(self):
        ctx = self.granted_context()
        with patch('mizu.runtime.MAX_ENGINE_TOOL_INVOCATIONS', 2):
            self.admit(ctx, 'one')
            self.admit(ctx, 'two')
            with self.assertRaisesRegex(LimitExceeded, 'evidence bound'):
                self.admit(ctx, 'three')
            self.admit(ctx, 'one')  # A known invocation needs no new storage.
        self.assertEqual(ctx.tool_count, 2)
        self.assertEqual(len(ctx.engine_tool_invocations), 2)

    def test_tool_limit_keeps_classification_and_does_not_cache_refused_invocation(self):
        ctx = self.granted_context(tools_per_run=1)
        self.admit(ctx)
        self.admit(ctx)
        self.assertEqual(ctx.tool_count, 1)
        with self.assertRaisesRegex(LimitExceeded, 'Per-run tool limit'):
            self.admit(ctx, 'new')
        self.assertEqual(ctx.bound_hit, 'tools')
        self.assertEqual(ctx.tool_count, 2)  # Existing over-limit bookkeeping.
        self.assertNotIn((0, 'new'), ctx.engine_tool_invocations)
        with self.assertRaises(Denied):
            self.admit(ctx)  # Bound hits also refuse retries of admitted ids.
        self.assertEqual(ctx.tool_count, 2)

    def test_all_wrap_up_bounds_refuse_repeated_and_new_invocations(self):
        for bound in ('requests', 'tools', 'deadline', 'drain'):
            with self.subTest(bound=bound):
                ctx = self.granted_context()
                self.admit(ctx)
                ctx.bound_hit = bound
                for identity in ('sdk-id', 'new'):
                    with self.assertRaises(Denied):
                        self.admit(ctx, identity)
                self.assertEqual(ctx.tool_count, 1)
                self.assertEqual(len(ctx.engine_tool_invocations), 1)

    def test_seal_and_stop_refuse_even_previously_admitted_identity(self):
        ctx = self.granted_context()
        self.admit(ctx)
        ctx.handle('finish', {'outcome': 'wait', 'summary': 'sealed'})
        count = ctx.tool_count
        for identity in ('sdk-id', 'new'):
            with self.assertRaisesRegex(Denied, 'sealed'):
                self.admit(ctx, identity)
        self.assertEqual(ctx.tool_count, count)
        for stop in ('stop', 'operations_stopped', 'closed'):
            with self.subTest(stop=stop):
                ctx = self.granted_context()
                self.admit(ctx)
                if stop == 'closed':
                    ctx.closed = True
                else:
                    getattr(ctx, stop).set()
                with self.assertRaises(Cancelled):
                    self.admit(ctx)
                self.assertEqual(ctx.tool_count, 1)
