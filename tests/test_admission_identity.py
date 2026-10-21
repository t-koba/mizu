"""Offline adapter-wrapped refusals retain trusted host exception identity."""
from unittest.mock import patch
from support import Fixture
from mizu.config import role_requests_per_run
from mizu.errors import Denied, InfraExceeded, MizuError, ModelFailure
from mizu.fs import read_json
from mizu.runtime import Engine, WRAP_UP_OPS


class WrappedDriver:
    requires_sandbox = False
    def __init__(self, callback):
        self.callback = callback
        self.calls = 0
    def execute(self, context, prompt, *, profile=None):
        self.calls += 1
        context.handle('_hello', {})
        try:
            self.callback(context)
        except MizuError as exc:
            # Reproduce a bridge refusal returned as an SDK error event. The
            # text is deliberately unrelated; classification cannot trust it.
            raise ModelFailure('pi', kind='error', message='adapter terminal error') from exc
        return {'requests': context.request_count}


class AdmissionIdentityTests(Fixture):
    def test_wrapped_exhausted_allowances_are_bounded_without_publication(self):
        for bound in ('deadline', 'drain', 'requests'):
            with self.subTest(bound=bound):
                before = self.project.snapshots.get()['id']
                def callback(ctx):
                    (ctx.workspace/'app.py').write_text('VALUE = 3\n')
                    ctx.bound_hit = bound
                    ctx.wrap_up_attempted = True
                    ctx.request_count = role_requests_per_run(ctx.config, ctx.role)
                    ctx.wrap_up_requests = ctx._wrap_up_request_used_limit()[1]
                    ctx.handle('_budget', {'sequence': 1})
                result = Engine(self.config, driver=WrappedDriver(callback)).run(self.project, 'worker')
                self.assertEqual((result['status'], result['bound']), ('bounded', bound))
                self.assertEqual(result['publication'], 'not_published')
                self.assertEqual(self.project.snapshots.get()['id'], before)
                self.assertEqual((self.project.workspace/'app.py').read_text(), 'VALUE = 3\n')
                self.assertFalse(self.project.control()['paused'])
                self.assertEqual(read_json(self.project.root/'health/worker.json', {}).get('consecutive_failures', 0), 0)

    def test_wrapped_first_request_bound_enters_reduced_wrapup_once(self):
        def callback(ctx):
            if not ctx.wrap_up_attempted:
                ctx.request_count = role_requests_per_run(ctx.config, ctx.role)
                ctx.handle('_budget', {'sequence': 1})
            else:
                self.assertEqual(set(ctx.role.capabilities), set(self.config.roles['worker'].capabilities) & WRAP_UP_OPS)
                for sequence in range(1, ctx._wrap_up_request_used_limit()[1] + 2):
                    ctx.handle('_budget', {'sequence': sequence})
                    ctx.handle('_request_done', {'sequence': sequence})
        driver = WrappedDriver(callback)
        result = Engine(self.config, driver=driver).run(self.project, 'worker')
        self.assertEqual((result['status'], result['bound']), ('bounded', 'requests'))
        self.assertEqual(driver.calls, 2)

    def test_wrapped_daily_quota_still_defers_without_publication(self):
        def callback(ctx): ctx.handle('_budget', {'sequence': 1})
        with patch('mizu.runtime.Budget.take', side_effect=InfraExceeded('daily quota')):
            with self.assertRaises(InfraExceeded):
                Engine(self.config, driver=WrappedDriver(callback)).run(self.project, 'worker')
        self.assertFalse(self.project.control()['paused'])
        self.assertEqual(read_json(self.project.root/'health/worker.json', {}).get('consecutive_failures', 0), 0)

    def test_wrapped_invalid_sequence_remains_a_fault(self):
        def callback(ctx): ctx.handle('_budget', {'sequence': 0})
        with self.assertRaises(Denied):
            Engine(self.config, driver=WrappedDriver(callback)).run(self.project, 'worker')
        self.assertTrue(list((self.project.root/'runs').glob('*/error.json')))

    def test_successful_admission_clears_old_refusal(self):
        ctx = self.context()
        with self.assertRaises(Denied): ctx.handle('_budget', {'sequence': 0})
        self.assertIsInstance(ctx.admission_exception, Denied)
        ctx.handle('_budget', {'sequence': 1})
        self.assertIsNone(ctx.admission_exception)
        self.assertIsNone(ctx.admission_error)
        self.assertFalse(ctx.admission_wait)

    def test_prior_dispatch_refusal_cannot_mask_a_provider_fault(self):
        ctx = self.context()
        with self.assertRaises(Denied): ctx.handle('_budget', {'sequence': 0})
        def callback(_): raise ModelFailure('pi', kind='error', message='actual provider error')
        with self.assertRaises(ModelFailure):
            Engine(self.config)._dispatch(ctx, WrappedDriver(callback), '{}')
        self.assertIsNone(ctx.admission_exception)
