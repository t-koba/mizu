"""Recovery contract with synthetic drivers only; no live provider/auth calls."""
import copy
import dataclasses
import datetime as dt
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_selection import SelectionFixture, specification
from mizu.admission import _Queue, admission
from mizu.budget import Budget
from mizu.cli import execute, parser
from mizu.drivers import admit_invocation
from mizu.errors import Cancelled, ConfigError, Denied, ModelFailure, ProtocolError
from mizu.fs import canonical, lock, read_json, write_json
from mizu.probes import _exact_result, next_deadline, service_due, sponsor_binding, target_binding
from mizu.runtime import Engine, daemon
from mizu.selection import State, active, project_status, validate_selectors


class ProbeDriver:
    def __init__(self, callback=None, *, admitted=True, settled=True, challenge=True, target=True):
        self.callback = callback
        self.admitted, self.settled, self.challenge, self.target = admitted, settled, challenge, target
        self.calls = []

    def execute(self, context, prompt, *, profile):
        self.calls.append((context, prompt, profile))
        context.handle('_hello', {})
        if self.admitted:
            context.handle('_budget', {'sequence': 1})
            context.handle('_model_usage', {'sequence': 1, 'usage': {'input': 3, 'output': 2}})
        if self.callback:
            self.callback(context)
        challenge = json.loads(prompt)['challenge'] if self.challenge else 'wrong'
        context.handle('finish', {'outcome': 'done', 'summary': challenge})
        return {'profile': profile, 'engine': context.config.engine(profile),
                **context.config.model(profile), 'settled': self.settled, 'usage_known': True,
                'usage': [{'input': 3, 'output': 2}], 'model': context.config.model(profile)['model'] if self.target else 'other'}


class RecoveryProbeTests(SelectionFixture):
    def setup_probe(self, *, upper=None, shared=False):
        spec = specification()
        spec['on_error'] = [{'scope': 'group', 'probe_seconds': 10}]
        if upper is not None:
            spec['on_error'][0]['seconds'] = upper
        if shared:
            spec['rules'][0]['candidates'][1]['group'] = 'shared'
        self.role, self.spec = self.setup_selection(spec)
        self.store = State(self.config)
        self.first = self.decision(self.role)
        return self.record_failure(self.first)

    def record_failure(self, decision=None, *, run='failure', attempt=0, at=1000):
        return self.store.result(decision or self.first, run=run, attempt=attempt, at=at,
                                 error=ModelFailure('pi', code='failed'))

    def service(self, driver=None, *, at=1010, config=None, reload_config=None, stop=None):
        config = config or self.config
        driver = driver or ProbeDriver()
        result = service_due(config, 'sample', stop or threading.Event(),
                             resolve=lambda profile: driver, reload_config=reload_config or (lambda: config), at=at)
        return result, driver

    def contribution(self, action):
        return self.store.read()['contributions'][action['contribution']]

    def fill_state_near_limit(self, state, *, margin=64):
        from mizu.selection import MAX_BYTES
        blocks = state['blocks']
        size = len(canonical(state))
        target = MAX_BYTES - margin
        index = 0
        while True:
            key = f'group:capacity-{index:06d}'
            block = {'created_at': 1000, 'until': 2000, 'run': 'capacity',
                     'selector': 'dynamic', 'rule': 0, 'error_rule': 0,
                     'sponsor': 'sample', 'role': 'worker'}
            entry = len(canonical({key: block})) - 3
            delta = entry + bool(blocks)
            if size + delta > target:
                break
            blocks[key] = block
            size += delta
            index += 1
        self.assertEqual(len(canonical(state)), size)
        self.assertGreater(size, MAX_BYTES - 512)
        self.store.save(state)
        self.assertEqual(self.store.path.stat().st_size, size)
        return size

    def test_schema_and_legacy_migration(self):
        spec = specification()
        for fields in ({'probe_seconds': 0, 'scope': 'group'}, {'probe_seconds': 31536001, 'scope': 'group'},
                       {'probe_seconds': 10}, {'probe_seconds': 10, 'scope': 'profile', 'until': 'error.retry_at'},
                       {'scope': 'profile', 'seconds': 2, 'counts_as_failure': 'false'}):
            spec['on_error'] = [fields]
            with self.assertRaises(ConfigError):
                validate_selectors({'dynamic': spec}, self.config.profiles, self.file.parent)
        self.role, self.spec = self.setup_selection()
        self.store = State(self.config)
        legacy = {'version': 1, 'observations': {}, 'history': {},
                  'blocks': {'group:shared': {'created_at': 1000, 'until': 1200, 'run': 'legacy'}}}
        write_json(self.store.path, legacy)
        self.assertEqual(self.store.read()['version'], 2)
        self.assertEqual(self.store.read()['contributions'], {})
        self.assertEqual(read_json(self.store.path)['version'], 1)  # reads do not migrate on disk
        self.first = self.decision(self.role)
        self.assertEqual(self.first['profile'], 'alternate')
        self.store.result(self.first, run='completed', at=1001)
        self.assertEqual(read_json(self.store.path)['version'], 2)
        self.assertEqual(self.store.read()['blocks'], legacy['blocks'])

    def test_fail_fast_probe_skips_a_resource_blocked_caller(self):
        action = self.setup_probe()
        stop = threading.Event()
        queued = threading.Event()
        release_waiter = threading.Event()
        config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, idle_seconds=0.02))
        resource = self.project.root / "locks/run-pending.lock"
        original_attempt = _Queue.attempt

        def attempt(queue, *, enqueue):
            try:
                return original_attempt(queue, enqueue=enqueue)
            finally:
                if threading.current_thread() is thread and queue.alive is not None:
                    # The queue transaction has ended. Keep the live blocked
                    # ticket, but avoid racing the probe's fail-fast queue lock
                    # against unrelated waiter polling (especially on Windows).
                    queued.set()
                    release_waiter.wait()

        def wait():
            try:
                with admission(config, (resource,), wait=True, stop=stop):
                    pass
            except Exception:
                pass
        thread = threading.Thread(target=wait)
        try:
            with patch.object(_Queue, "attempt", attempt), lock(resource, blocking=False):
                thread.start()
                self.assertTrue(queued.wait(5), "caller did not enqueue")
                result, driver = self.service(config=config)
                self.assertEqual(result['status'], 'success', result)
                self.assertEqual(len(driver.calls), 1)
                self.assertEqual(self.contribution(action)['status'], 'recovered')
                self.assertTrue(thread.is_alive())
                self.assertEqual(len(read_json(config.data / 'slots/queue.json')['tickets']), 1)
        finally:
            stop.set()
            release_waiter.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(read_json(config.data / "slots/queue.json")["tickets"], [])

    def test_fail_fast_probe_cannot_bypass_a_ready_caller(self):
        action = self.setup_probe()
        head = _Queue(self.config, ())
        try:
            # A live, ready ticket kept between attempts without a sleep race.
            with lock(self.config.data / 'slots/queue.lock', blocking=False):
                head._enqueue([])
            result, driver = self.service()
            self.assertEqual(result['status'], 'deferred')
            self.assertEqual(result['reason'], 'locks_unavailable')
            self.assertEqual(driver.calls, [])
            self.assertTrue(active(self.contribution(action), 1011))
        finally:
            head.close()
        self.assertEqual(read_json(self.config.data / 'slots/queue.json')['tickets'], [])

    def test_paid_success_has_no_work_unit_side_effects(self):
        action = self.setup_probe()
        before = {p.relative_to(self.project.root): p.read_bytes() for name in ('runs', 'health', 'routing', 'snapshots')
                  for p in (self.project.root / name).rglob('*') if p.is_file()}
        snapshot = self.project.snapshots.get()
        health = self.project.status()['health']
        result, driver = self.service()
        self.assertEqual(result['status'], 'success')
        self.assertEqual(self.contribution(action)['status'], 'recovered')
        self.assertEqual(self.project.snapshots.get(), snapshot)
        self.assertEqual(self.project.status()['health'], health)
        after = {p.relative_to(self.project.root): p.read_bytes() for name in ('runs', 'health', 'routing', 'snapshots')
                 for p in (self.project.root / name).rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        context, prompt, target = driver.calls[0]
        self.assertEqual(target, 'primary')
        self.assertTrue(context.ephemeral)
        self.assertEqual(context.goal, '')
        self.assertEqual(context.role.capabilities, ('finish',))
        self.assertEqual(context.role.engine_tools, ())
        self.assertNotIn('Preserve correctness', prompt)
        event = self.store.read()['events'][result['id']]
        self.assertTrue(event['admitted'] and event['settled'] and event['challenge_ok'])
        self.assertEqual(event['model']['tokens']['input_tokens'], 3)
        self.assertEqual(Budget(self.config.data / 'budget', 100).usage('sample')['used'], 1)
        self.assertEqual(self.decision(self.role, 1011)['profile'], 'primary')
        self.assertFalse(context.run_dir.exists())
        self.assertNotIn(b'mizu-recovery-', canonical(event))

    def test_finish_seal_without_driver_settlement_does_not_recover(self):
        action = self.setup_probe()
        class FailingSettlement(ProbeDriver):
            def execute(self, context, prompt, *, profile):
                super().execute(context, prompt, profile=profile)
                raise ProtocolError('synthetic post-seal settlement failure')
        result, driver = self.service(FailingSettlement())
        self.assertIsNotNone(driver.calls[0][0].finished)
        self.assertEqual(result['status'], 'failure')
        self.assertEqual(result['reason'], 'driver_failed')
        self.assertTrue(active(self.contribution(action), 1011))
        self.assertFalse(self.store.read()['events'][result['id']]['settled'])

    def test_fixed_and_probe_overlap_both_orders(self):
        for fixed_first in (False, True):
            with self.subTest(fixed_first=fixed_first):
                self.setup_probe()
                # Restore clean state and make both failures from one captured decision.
                self.store.path.unlink()
                self.spec['on_error'] = [{'scope': 'group', 'seconds': 100}]
                if fixed_first:
                    self.record_failure(run='fixed')
                self.spec['on_error'] = [{'scope': 'group', 'probe_seconds': 10}]
                action = self.record_failure(run='probe')
                self.spec['on_error'] = [{'scope': 'group', 'seconds': 100}]
                if not fixed_first:
                    self.record_failure(run='fixed')
                self.service()
                self.assertEqual(self.contribution(action)['status'], 'recovered')
                self.assertEqual(self.decision(self.role, 1011)['profile'], 'alternate')
                self.assertEqual(self.decision(self.role, 1100)['profile'], 'primary')
                self.store.path.unlink()

    def test_group_profiles_and_newer_failures_are_independent(self):
        a = self.setup_probe(shared=True)
        alternate = copy.deepcopy(self.first)
        alternate.update(profile='alternate', probe_target=target_binding(self.config, 'alternate'))
        alternate['facts']['candidate']['profile'] = 'alternate'
        b = self.record_failure(alternate, run='second', at=1001)
        self.assertIsNone(self.decision(self.role, 1002)['profile'])
        result, _ = self.service(at=1010)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(self.contribution(a)['status'], 'recovered')
        self.assertTrue(active(self.contribution(b), 1010))
        self.assertIsNone(self.decision(self.role, 1010)['profile'])
        self.service(at=1011)
        self.assertEqual(self.decision(self.role, 1012)['profile'], 'primary')
        newer = self.record_failure(run='new', at=1020)
        old = self.contribution(a)
        self.assertFalse(self.store.probe_update(a['contribution'], revision=2,
                                                claim=old['last_event'], recover=True, at=1021))
        self.assertTrue(active(self.contribution(newer), 1021))

    def test_replay_does_not_advance_or_resurrect_and_conflict_is_denied(self):
        action = self.setup_probe()
        saved = self.store.path.read_bytes()
        self.assertEqual(self.record_failure(at=1005), action)
        self.assertEqual(self.store.path.read_bytes(), saved)
        self.service()
        saved = self.store.path.read_bytes()
        self.assertEqual(self.record_failure(at=2000), action)
        self.assertEqual(self.store.path.read_bytes(), saved)
        with self.assertRaises(ConfigError):
            self.store.result(self.first, run='failure', at=2001, error=ModelFailure('pi', code='different'))
        other = copy.deepcopy(self.first)
        other['profile'] = 'alternate'
        distinct = self.record_failure(other, run='failure', attempt=1, at=2001)
        self.assertNotEqual(distinct['contribution'], action['contribution'])

    def test_upper_expiry_and_temporary_candidate_override(self):
        action = self.setup_probe(upper=20, shared=True)
        self.spec['rules'][0]['candidates'][0]['recover_when'] = {'path': 'observations.shared.facts.ok', 'op': 'eq', 'value': True}
        self.store.observe({'group': 'shared', 'observed_at': 1001, 'expires_at': 1005, 'facts': {'ok': True}})
        self.assertEqual(self.decision(self.role, 1002)['profile'], 'primary')
        self.assertEqual(self.contribution(action)['status'], 'active')
        self.assertIsNone(self.decision(self.role, 1006)['profile'])
        self.service(ProbeDriver(challenge=False), at=1010)
        result, driver = self.service(at=1020)
        self.assertEqual(result['status'], 'expired')
        self.assertEqual(driver.calls, [])
        self.assertEqual(self.decision(self.role, 1021)['profile'], 'primary')
        self.assertEqual(self.record_failure(at=1030), action)
        self.assertEqual(self.contribution(action)['status'], 'expired')

    def test_non_success_results_keep_the_block(self):
        action = self.setup_probe()
        for index, driver in enumerate((ProbeDriver(admitted=False), ProbeDriver(settled=False),
                                        ProbeDriver(challenge=False), ProbeDriver(target=False),
                                        ProbeDriver(lambda c: (_ for _ in ()).throw(ModelFailure('pi'))),
                                        ProbeDriver(lambda c: (_ for _ in ()).throw(Cancelled('cancelled'))),
                                        ProbeDriver(lambda c: (_ for _ in ()).throw(ProtocolError('uncertain'))))):
            result, _ = self.service(driver, at=1010 + index * 10)
            self.assertNotEqual(result['status'], 'success')
            self.assertTrue(active(self.contribution(action), 1010 + index * 10))
        self.assertEqual(len(self.store.read()['events']), 7)

    def test_contrary_assistant_identity_without_usage_never_recovers(self):
        action = self.setup_probe()
        class ObservingDriver(ProbeDriver):
            def execute(driver, context, prompt, *, profile):
                result = super().execute(context, prompt, profile=profile)
                result.update(usage=[], usage_known=False, usage_observations=[],
                              observed_models=[{'provider': 'other-provider', 'model': 'other-model'}])
                return result
        result, _ = self.service(ObservingDriver())
        self.assertEqual(result['reason'], 'wrong_target')
        self.assertEqual(result['status'], 'failure')
        self.assertTrue(active(self.contribution(action), 1011))
        # Both supported evidence arrays reject explicit contradictions. Absent
        # or unknown identity never becomes a fabricated target observation.
        expected = {'profile': 'primary', 'engine': self.config.engine('primary'), **self.config.model('primary')}
        for field in ('usage_observations', 'observed_models'):
            for key in ('provider', 'model'):
                with self.subTest(field=field, key=key):
                    self.assertFalse(_exact_result(self.config, self.contribution(action),
                                                   {**expected, field: [{key: 'other'}]}))
            for observation in ({}, {'provider': None, 'model': None}, self.config.model('primary')):
                self.assertTrue(_exact_result(self.config, self.contribution(action),
                                              {**expected, field: [observation]}))
            for malformed in (None, {}, [None]):
                self.assertFalse(_exact_result(self.config, self.contribution(action),
                                               {**expected, field: malformed}))
        self.assertTrue(_exact_result(self.config, self.contribution(action), expected))

    def test_engine_directory_and_credentials_locator_changes_defer(self):
        for engine in ('pi', 'pi-durable', 'codex'):
            with self.subTest(engine=engine):
                self.config.profiles['primary']['engine'] = engine
                if engine == 'codex':
                    self.config.engines['codex']['command'] = (sys.executable,)
                    self.config.profiles['primary']['options'] = {
                        'model_providers': {'test-provider': {'env_key': 'MIZU_SYNTHETIC_AUTH'}}}
                action = self.setup_probe()
                engines = copy.deepcopy(self.config.engines)
                engines[engine]['directory'] = self.root / 'other-engine-directory'
                changed = dataclasses.replace(self.config, engines=engines)
                result, driver = self.service(config=changed)
                self.assertEqual(result['reason'], 'target_changed')
                self.assertEqual(driver.calls, [])
                # Only the source locator changes: profiles, executable bytes,
                # engine directory and non-secret native content stay identical.
                changed = dataclasses.replace(self.config, file=self.root / 'other-config/config.toml')
                result, driver = self.service(config=changed, at=1020)
                self.assertEqual(result['reason'], 'target_changed')
                self.assertEqual(driver.calls, [])
                self.assertTrue(active(self.contribution(action), 1021))
                self.assertEqual(Budget(self.config.data / 'budget', 100).usage('sample')['used'], 0)
                self.assertNotIn(str(self.root).encode(), canonical(self.store.read()))
                self.store.path.unlink()

    def test_codex_selected_auth_locator_and_same_source_refresh(self):
        self.config.profiles['primary']['engine'] = 'codex'
        self.config.engines['codex']['command'] = (sys.executable,)
        source = self.root / 'synthetic-auth.json'
        # The binding must not open an auth/credentials file, even one whose
        # content is deliberately not a usable credential document.
        source.write_bytes(b'synthetic fixture, not credentials')
        with patch.dict(os.environ, {'CODEX_AUTH_FILE': str(source)}):
            action = self.setup_probe()
            binding = self.contribution(action)['target']
            with patch.dict(os.environ, {'CODEX_AUTH_FILE': str(self.root / 'other-auth.json')}):
                result, driver = self.service()
            self.assertEqual(result['reason'], 'target_changed')
            self.assertEqual(driver.calls, [])
            source.write_bytes(b'refreshed synthetic fixture')
            self.assertEqual(target_binding(self.config, 'primary'), binding)
            result, _ = self.service(at=1020)
            self.assertEqual(result['status'], 'success')
        self.assertNotIn(str(source).encode(), canonical(self.store.read()))

    def test_pi_source_content_refresh_does_not_change_binding(self):
        for engine in ('pi', 'pi-durable'):
            with self.subTest(engine=engine):
                self.config.profiles['primary']['engine'] = engine
                self.setup_probe()
                directory = self.config.agent_dir(engine)
                directory.mkdir(parents=True, exist_ok=True)
                auth = directory / 'auth.json'
                env = self.config.file.parent / 'credentials.env'
                binding = target_binding(self.config, 'primary')
                auth.write_bytes(b'synthetic fixture, not credentials')
                env.write_bytes(b'synthetic fixture, not credentials')
                self.assertEqual(target_binding(self.config, 'primary'), binding)
                auth.write_bytes(b'refreshed synthetic fixture')
                env.write_bytes(b'refreshed synthetic fixture')
                self.assertEqual(target_binding(self.config, 'primary'), binding)
                result, _ = self.service()
                self.assertEqual(result['status'], 'success')
                self.store.path.unlink()

    def test_fresh_config_revocation_before_launch_has_no_admission(self):
        action = self.setup_probe()
        profiles = copy.deepcopy(self.config.profiles)
        profiles['primary']['model'] = 'changed'
        deleted = copy.deepcopy(self.config.profiles)
        deleted.pop('primary')
        changes = (
            (dataclasses.replace(self.config, profiles=profiles), 'target_changed'),
            (dataclasses.replace(self.config, profiles=deleted), 'target_missing'),
            (dataclasses.replace(self.config, data=self.root / 'other-data'), 'sponsor_changed'),
            (dataclasses.replace(self.config, roles={k: v for k, v in self.config.roles.items() if k != 'worker'}),
             'sponsor_missing'),
            (dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, daily_requests=0)),
             'budget_unavailable'),
            )
        at = 1010
        for fresh, reason in changes:
            # Exercise both reload points: immediately after the durable claim,
            # and after all launch locks. Neither may use the stale authority.
            for stage in (1, 2):
                with self.subTest(reason=reason, stage=stage):
                    configs = [self.config] * (stage - 1) + [fresh]
                    with patch.object(Budget, 'take') as take:
                        result, driver = self.service(at=at, reload_config=lambda: configs.pop(0) if configs else fresh)
                    self.assertEqual(result['status'], 'deferred')
                    self.assertEqual(result['reason'], reason)
                    self.assertEqual(driver.calls, [])
                    take.assert_not_called()
                    self.assertEqual(result['requests'], 0)
                    self.assertTrue(active(self.contribution(action), at))
                    self.assertEqual(self.store.read()['events'][result['id']]['admitted'], False)
                    at += 10
        self.assertEqual(Budget(self.config.data / 'budget', 100).usage('sample')['used'], 0)

    def test_fresh_parallel_limit_change_after_slot_defers_before_admission(self):
        action = self.setup_probe()
        fresh = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, parallel_runs=self.config.limits.parallel_runs + 1))
        configs = [self.config, fresh]
        with patch.object(Budget, 'take') as take:
            result, driver = self.service(at=1010, reload_config=lambda: configs.pop(0))
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(result['reason'], 'configuration_changed')
        self.assertEqual(driver.calls, [])
        take.assert_not_called()
        self.assertEqual(result['requests'], 0)
        self.assertTrue(active(self.contribution(action), 1011))

    def test_fresh_launch_limits_and_budget_reach_context_and_driver(self):
        self.setup_probe()
        # This test covers fresh-config propagation, not host tzdata or deadline
        # expiry. Use a deterministic Tokyo offset and a long non-default wall
        # so it runs on Windows without IANA data or timing out on slow workers.
        test_zone = dt.timezone(dt.timedelta(hours=9), 'Asia/Tokyo')
        with patch('mizu.config.ZoneInfo', return_value=test_zone):
            fresh = dataclasses.replace(self.config, timezone='Asia/Tokyo',
                                       limits=dataclasses.replace(self.config.limits, requests_per_run=1,
                                                                  daily_requests=7, shared_daily_requests=8, run_seconds=60))
            driver = ProbeDriver()
            with patch('mizu.probes.driver_for', return_value=driver) as choose:
                result = service_due(self.config, 'sample', threading.Event(),
                                     reload_config=lambda: fresh, at=1010)
        self.assertEqual(result['status'], 'success')
        launched_config = choose.call_args.args[0]
        context = driver.calls[0][0]
        for config in (launched_config, context.config):
            self.assertEqual(config.limits.requests_per_run, 1)
            self.assertEqual(config.limits.daily_requests, 7)
            self.assertEqual(config.limits.shared_daily_requests, 8)
            self.assertEqual(config.limits.run_seconds, 60)
            self.assertEqual(config.timezone, 'Asia/Tokyo')
        budget_files = list((self.config.data / 'budget').glob('????-??-??.json'))
        self.assertEqual(len(budget_files), 1)
        self.assertEqual(read_json(budget_files[0])['zone'], 'Asia/Tokyo')

    def test_revocation_at_context_and_invocation_admission(self):
        action = self.setup_probe()
        profiles = copy.deepcopy(self.config.profiles)
        profiles['primary']['model'] = 'changed'
        changes = ((dataclasses.replace(self.config, profiles=profiles), 'target_changed'),
                   (dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, daily_requests=0)),
                    'budget_unavailable'))
        at = 1010
        for fresh, reason in changes:
            for invocation in (False, True):
                with self.subTest(reason=reason, invocation=invocation):
                    configs = [self.config, self.config, fresh]
                    driver = (ProbeDriver(lambda c: admit_invocation(c, unit='query'), admitted=False)
                              if invocation else ProbeDriver())
                    with patch.object(Budget, 'take') as take:
                        result, _ = self.service(driver, at=at, reload_config=lambda: configs.pop(0))
                    self.assertEqual(len(driver.calls), 1)  # launch preceded this later revocation
                    self.assertEqual(result['reason'], reason)
                    self.assertEqual(result['requests'], 0)
                    take.assert_not_called()
                    self.assertTrue(active(self.contribution(action), at))
                    at += 10

    def test_postflight_revocation_retains_payment_and_block(self):
        action = self.setup_probe()
        profiles = copy.deepcopy(self.config.profiles)
        profiles['primary']['model'] = 'changed'
        changed = dataclasses.replace(self.config, profiles=profiles)
        configs = [self.config, self.config, self.config, changed]
        result, driver = self.service(reload_config=lambda: configs.pop(0))
        self.assertEqual(result['reason'], 'target_changed')
        self.assertEqual(result['status'], 'failure')
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(result['requests'], 1)
        self.assertEqual(Budget(self.config.data / 'budget', 100).usage('sample')['used'], 1)
        self.assertTrue(active(self.contribution(action), 1011))

    def test_writer_reservation_or_unreadable_store_defers_without_spend(self):
        from mizu.delegation_jobs import DelegationJobs
        self.setup_probe()
        store = DelegationJobs(self.project.root)
        with lock(self.project.root / 'locks/workspace.lock'):
            store.start('worker', 'probe-gate', {
                'role': 'helper', 'task': 'Reservation fixture', 'profile': None,
                'source_snapshot': 'a' * 64})
        # No process holds workspace.lock: the durable reservation alone fences.
        for instant, corrupt in ((1010, False), (1020, True)):
            with self.subTest(corrupt=corrupt):
                if corrupt:
                    (store.root / 'state.json').write_bytes(b'{')
                result, driver = self.service(at=instant)
                self.assertEqual(result['status'], 'deferred')
                self.assertEqual(result['reason'], 'configuration_denied')
                self.assertEqual(result['requests'], 0)
                self.assertEqual(driver.calls, [])
        self.assertEqual(Budget(self.config.data / 'budget', 100).usage('sample')['used'], 0)

    def test_budget_denial_locks_and_controls_do_not_spend(self):
        action = self.setup_probe()
        config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, daily_requests=0))
        result, _ = self.service(config=config)
        self.assertEqual(result['reason'], 'budget_unavailable')
        self.assertEqual(Budget(config.data / 'budget', 100).usage('sample')['used'], 0)
        with lock(self.project.root / 'locks' / 'workspace.lock', blocking=False):
            result, driver = self.service(at=1020)
        self.assertEqual(result['reason'], 'locks_unavailable')
        self.assertEqual(driver.calls, [])
        for at, controls, reason in ((1030, {'armed': False}, 'unarmed'), (1040, {'armed': True, 'paused': True}, 'paused'),
                                      (1050, {'paused': False, 'draining': True}, 'draining')):
            self.project.set_control(**controls)
            result, driver = self.service(at=at)
            self.assertEqual(result['reason'], reason)
            self.assertEqual(driver.calls, [])
        stop = threading.Event(); stop.set()
        result, driver = self.service(at=1060, stop=stop)
        self.assertEqual(result['reason'], 'stopped')
        self.assertEqual(driver.calls, [])
        self.assertTrue(active(self.contribution(action), 1061))

    def test_changed_deleted_target_and_sponsor_and_postflight_fence(self):
        action = self.setup_probe()
        profiles = copy.deepcopy(self.config.profiles)
        profiles['primary']['model'] = 'changed-model'
        changed = dataclasses.replace(self.config, profiles=profiles)
        result, driver = self.service(config=changed)
        self.assertEqual(result['reason'], 'target_changed')
        self.assertEqual(driver.calls, [])
        deleted = copy.deepcopy(profiles)
        deleted.pop('primary')
        result, driver = self.service(config=dataclasses.replace(self.config, profiles=deleted), at=1020)
        self.assertEqual(result['reason'], 'target_missing')
        result, _ = self.service(at=1030, reload_config=lambda: changed)
        self.assertEqual(result['reason'], 'target_changed')
        sponsor_path = self.project.root / 'project.toml'
        original = sponsor_path.read_bytes()
        sponsor_path.write_bytes(original + b'\n# changed\n')
        result, driver = self.service(at=1040)
        self.assertEqual(result['reason'], 'sponsor_changed')
        sponsor_path.unlink()
        result, driver = self.service(at=1050)
        self.assertEqual(result['reason'], 'sponsor_missing')
        self.assertTrue(active(self.contribution(action), 1051))

    def test_concurrent_claim_and_new_failure_during_inference(self):
        action = self.setup_probe()
        entered, release = threading.Event(), threading.Event()
        new = []
        def in_driver(context):
            c = self.contribution(action)
            intent = self.store.read()['events'][c['claim']]
            self.assertEqual(intent['status'], 'intent')
            self.assertEqual(intent['requests'], 1)
            self.assertEqual(intent['model']['tokens']['input_tokens'], 3)
            new.append(self.record_failure(run='newer', at=1010))  # selection lock not held
            entered.set()
            self.assertTrue(release.wait(5))
        driver = ProbeDriver(in_driver)
        with ThreadPoolExecutor(2) as pool:
            future = pool.submit(self.service, driver)
            self.assertTrue(entered.wait(5))
            second, second_driver = self.service()
            self.assertIsNone(second)
            self.assertEqual(second_driver.calls, [])
            release.set()
            result, _ = future.result(5)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(self.contribution(action)['status'], 'recovered')
        self.assertTrue(active(self.contribution(new[0]), 1011))
        self.assertEqual(Budget(self.config.data / 'budget', 100).usage('sample')['used'], 1)

    def test_terminal_evidence_precedes_recovery_and_crash_is_conservative(self):
        action = self.setup_probe()
        original = State.probe_update
        def crash_after_terminal(store, identity, **kwargs):
            if kwargs.get('recover'):
                raise RuntimeError('synthetic crash after terminal evidence')
            return original(store, identity, **kwargs)
        with patch.object(State, 'probe_update', crash_after_terminal):
            with self.assertRaises(RuntimeError): self.service()
        c = self.contribution(action)
        self.assertTrue(active(c, 1011))
        e = self.store.read()['events'][c['claim']]
        self.assertEqual(e['status'], 'success')
        result, driver = self.service(at=1100)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(driver.calls, [])
        self.assertEqual(self.contribution(action)['next_probe_at'], 1110)
        self.service(at=1110)
        self.assertEqual(self.contribution(action)['status'], 'recovered')

    def test_intent_interruption_is_not_immediately_replayed(self):
        action = self.setup_probe()
        # Simulate a hard interruption: intent persisted, no terminal event.
        def interrupt(context):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.service(ProbeDriver(interrupt))
        c = self.contribution(action)
        self.assertEqual(self.store.read()['events'][c['claim']]['status'], 'intent')
        result, driver = self.service(at=1011)
        self.assertIsNone(result)
        self.assertEqual(driver.calls, [])
        result, driver = self.service(at=1100)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.contribution(action)['next_probe_at'], 1110)
        self.assertTrue(active(self.contribution(action), 1100))

    def test_state_and_event_capacity_fail_closed_without_eviction(self):
        action = self.setup_probe()
        with patch('mizu.selection.MAX_STATE_ITEMS', 1):
            with self.assertRaises(ConfigError): self.record_failure(run='second')
        self.assertTrue(self.store.read()['exhausted'])
        self.assertTrue(active(self.contribution(action), 1011))
        self.assertIsNone(self.decision(self.role, 1011)['profile'])
        self.assertTrue(self.project.status()['selection']['exhausted'])
        self.assertIsNone(next_deadline(self.config, 'sample', at=1011))
        self.assertEqual(len(self.store.read()['receipts']), 1)
        # Independent event exhaustion retains every prior record and obligation.
        state = self.store.read(); state['exhausted'] = False; self.store.save(state)
        self.service(ProbeDriver(challenge=False))
        with patch('mizu.selection.MAX_STATE_ITEMS', 1):
            with self.assertRaises(ConfigError): self.service(at=1020)
        self.assertEqual(len(self.store.read()['events']), 1)
        self.assertTrue(active(self.contribution(action), 1021))

    def test_status_deadlines_and_no_unrelated_task_data(self):
        action = self.setup_probe(upper=50)
        self.assertEqual(next_deadline(self.config, 'sample', at=1001), 1010)
        self.assertEqual(self.decision(self.role, 1001)['next_evaluation_at'], 1010)
        # Controlled times make this active for project status.
        with patch('mizu.selection.time.time', return_value=1001):
            status = self.project.status()['selection']
        block = status['probe_blocks'][0]
        self.assertEqual(block['selector'], 'dynamic')
        self.assertEqual(block['rule'], 0)
        self.assertEqual(block['until'], 1050)
        self.assertEqual(block['sponsor'], 'sample')
        self.assertEqual(status['next_probe_at'], 1010)
        self.assertNotIn('facts', block)
        self.assertNotIn('task', block)
        with patch('mizu.cli.load', return_value=self.config):
            result = execute(parser().parse_args(['--config', str(self.file), 'selection', 'status']))
        self.assertEqual(result['version'], 2)
        self.service(ProbeDriver(challenge=False))
        with patch('mizu.selection.time.time', return_value=1011):
            status = self.project.status()['selection']
        self.assertEqual(status['probe_blocks'][0]['last_result']['status'], 'failure')
        self.assertEqual(status['probe_blocks'][0]['reason'], 'wrong_challenge')

    def test_status_deduplicates_bindings_only_within_one_read(self):
        action = self.setup_probe()
        for i in range(64):
            self.record_failure(run=f'failure-{i}')
        before = self.store.path.read_bytes()
        with patch('mizu.probes.target_binding', wraps=target_binding) as target, \
             patch('mizu.probes.sponsor_binding', wraps=sponsor_binding) as sponsor:
            status = project_status(self.config, self.project, at=1001)
            self.assertEqual(len(status['probe_blocks']), 65)
            self.assertEqual(target.call_count, 1)
            self.assertEqual(sponsor.call_count, 1)
            self.assertEqual(self.store.path.read_bytes(), before)
            # Different pinned identities cannot borrow the cached authority.
            state = self.store.read()
            state['contributions'][action['contribution']]['target'] = 'a' * 64
            self.store.save(state)
            changed = project_status(self.config, self.project, at=1001)
            self.assertEqual(target.call_count, 3)
            self.assertEqual(sponsor.call_count, 2)
            for block in changed['probe_blocks']:
                expected = 'target_changed' if block['contribution'] == action['contribution'] else None
                self.assertEqual(block['authorization_reason'], expected)
            # A new read must evaluate bindings again, not reuse old config or control.
            self.project.set_control(paused=True)
            paused = project_status(self.config, self.project, at=1001)
            self.assertEqual(sponsor.call_count, 3)
            self.assertTrue(all(c['authorization_reason'] == 'paused' for c in paused['probe_blocks']))
        self.project.set_control(paused=False)
        profiles = copy.deepcopy(self.config.profiles)
        profiles['primary']['model'] = 'changed'
        result, driver = self.service(reload_config=lambda: dataclasses.replace(self.config, profiles=profiles))
        self.assertEqual(result['reason'], 'target_changed')
        self.assertEqual(driver.calls, [])  # display cache never authorizes spending

    def test_daemon_services_completed_and_waiting_before_readiness(self):
        for outcome in ('done', 'wait'):
            with self.subTest(outcome=outcome):
                self.setup_probe()
                def readiness(*args, **kwargs):
                    self.assertEqual(next(iter(self.store.read()['contributions'].values()))['status'], 'recovered')
                    return False
                real_service = service_due
                driver = ProbeDriver()
                def run_probe(config, project_name, stop):
                    result = real_service(config, project_name, stop, resolve=lambda p: driver,
                                          reload_config=lambda: self.config, at=1010)
                    stop.set()
                    return result
                contribution = next(iter(self.store.read()['contributions'].values()))
                self.assertEqual(contribution['role'], 'worker')
                snapshot = {**self.project.snapshots.get(), 'outcome': outcome}
                # The resident loop belongs to a different role. It must still
                # service this project's worker contribution using the stored
                # worker authorization and locks.
                observer = dataclasses.replace(self.config.roles['worker'], name='observer',
                                               daemon=True, workspace='write')
                daemon_config = dataclasses.replace(self.config,
                                                    roles={**self.config.roles, 'observer': observer})
                with patch('mizu.runtime.load', return_value=daemon_config), patch('mizu.runtime.vcs_poll_enabled', return_value=False), \
                     patch('mizu.probes.service_due', run_probe), patch('mizu.runtime.should_run', side_effect=readiness), \
                     patch.object(type(self.project.snapshots), 'get', return_value=snapshot), patch('builtins.print'), \
                     patch.object(Engine, 'run') as run:
                    daemon(daemon_config, 'sample', 'observer')
                self.assertEqual(len(driver.calls), 1)
                run.assert_not_called()
                self.store.path.unlink()

    def test_oneshot_run_services_due_probe_without_daemon(self):
        from support import ScriptDriver
        action = self.setup_probe()
        self.assertEqual(self.contribution(action)['role'], 'worker')
        probe_driver = ProbeDriver()
        def main(context, prompt, profile):
            context.handle('finish', {'outcome': 'wait', 'summary': 'one-shot work',
                                      'state': 'Next step is explicit.'})
        # No daemon exists for the project in this test. The one-shot worker
        # run must still service the worker-sponsored due contribution using
        # the stored authorization and locks.
        with patch('mizu.probes.driver_for', return_value=probe_driver):
            result = Engine(self.config, driver=ScriptDriver(main)).run(self.project, 'worker')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(probe_driver.calls), 1)
        self.assertEqual(self.contribution(action)['status'], 'recovered')

    def test_ready_daemon_services_only_one_probe_before_run(self):
        self.setup_probe()
        # Second independently due contribution with a distinct run identity.
        self.record_failure(self.first, run='failure-second', attempt=0, at=1000)
        self.assertEqual(len(self.store.read()['contributions']), 2)
        real_service = service_due
        driver = ProbeDriver()
        service_calls = []
        def run_probe(config, project_name, stop):
            service_calls.append(project_name)
            result = real_service(config, project_name, stop, resolve=lambda profile: driver,
                                  reload_config=lambda: self.config, at=1010)
            stop.set()
            return result
        # A ready daemon iteration must service exactly one due contribution
        # before the ordinary run and mark the attempt so Engine.run does not
        # service a second one in the same top-level iteration.
        with patch('mizu.runtime.load', return_value=self.config), \
             patch('mizu.runtime.vcs_poll_enabled', return_value=False), \
             patch('mizu.probes.service_due', run_probe), \
             patch('mizu.runtime.should_run', return_value=True), \
             patch('builtins.print'), \
             patch.object(Engine, 'run', return_value={'status': 'completed'}) as run:
            daemon(self.config, 'sample', 'worker')
        self.assertEqual(service_calls, ['sample'])
        self.assertEqual(len(driver.calls), 1)
        statuses = sorted(c['status'] for c in self.store.read()['contributions'].values())
        self.assertEqual(statuses, ['active', 'recovered'])
        self.assertEqual(run.call_count, 1)
        _, kwargs = run.call_args
        self.assertTrue(kwargs.get('probes_serviced') is True)

    def test_engine_run_probes_serviced_flag_skips_duplicate_service(self):
        from support import ScriptDriver
        import mizu.probes as probes_module
        action = self.setup_probe()
        probe_driver = ProbeDriver()
        def main(context, prompt, profile):
            context.handle('finish', {'outcome': 'wait', 'summary': 'one-shot work',
                                      'state': 'Next step is explicit.'})
        # Default (standalone/one-shot) still services exactly one due probe.
        with patch('mizu.probes.driver_for', return_value=probe_driver), \
             patch('mizu.probes.service_due', wraps=probes_module.service_due) as spy:
            result = Engine(self.config, driver=ScriptDriver(main)).run(self.project, 'worker')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(len(probe_driver.calls), 1)
        self.assertEqual(self.contribution(action)['status'], 'recovered')
        # A daemon that already attempted service passes the flag so the
        # Engine skips only the duplicate without changing ordinary work.
        followup = self.record_failure(self.first, run='failure-followup', attempt=0, at=1000)
        skipped_driver = ProbeDriver()
        with patch('mizu.probes.driver_for', return_value=skipped_driver), \
             patch('mizu.probes.service_due', wraps=probes_module.service_due) as skipped_spy:
            skipped = Engine(self.config, driver=ScriptDriver(main)).run(
                self.project, 'worker', probes_serviced=True)
        self.assertEqual(skipped['status'], 'completed')
        self.assertEqual(skipped_spy.call_count, 0)
        self.assertEqual(skipped_driver.calls, [])
        self.assertEqual(self.store.read()['contributions'][followup['contribution']]['status'], 'active')

    def test_oneshot_run_settles_expired_probe_upper_bound(self):
        from support import ScriptDriver
        action = self.setup_probe(upper=60)
        def main(context, prompt, profile):
            context.handle('finish', {'outcome': 'wait', 'summary': 'one-shot work',
                                      'state': 'Next step is explicit.'})
        # The expired upper bound settles without launching any driver.
        result = Engine(self.config, driver=ScriptDriver(main)).run(self.project, 'worker')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.contribution(action)['status'], 'expired')

    def test_only_finish_and_hard_request_tool_bounds(self):
        action = self.setup_probe()
        def bounds(context):
            for op, args in (('_engine_tool', {'name': 'read', 'invocation_id': 'sdk-read'}), ('read', {'path': 'PROJECT.md'}),
                             ('finish', {'outcome': 'done', 'summary': 'wrong', 'next_profile': 'alternate'})):
                with self.assertRaises(Denied): context.handle(op, args)
            context.handle('_budget', {'sequence': 2})
            with self.assertRaises(Exception): context.handle('_budget', {'sequence': 3})
        result, _ = self.service(ProbeDriver(bounds))
        self.assertEqual(result['status'], 'failure')
        self.assertEqual(result['requests'], 2)
        self.assertEqual(self.contribution(action)['status'], 'active')

    def test_runtime_failure_captures_probe_and_preserves_fallback_accounting(self):
        from support import ScriptDriver
        self.setup_probe()
        self.store.path.unlink()
        def main(context, prompt, profile):
            if context.role.profile == 'primary':
                raise ModelFailure('pi', code='failed')
        result = Engine(self.config, driver=ScriptDriver(main)).run(self.project, 'worker')
        self.assertEqual(result['status'], 'completed')
        state = self.store.read()
        self.assertEqual(len(state['contributions']), 1)
        c = next(iter(state['contributions'].values()))
        self.assertEqual(c['profile'], 'primary')
        self.assertEqual(len(state['receipts']), 1)  # only the matched probe failure needs a receipt
        health = self.project.status()['health']
        runs = list((self.project.root / 'runs').iterdir())
        snapshot = self.project.snapshots.get()
        event, _ = self.service(at=c['next_probe_at'])
        self.assertEqual(event['status'], 'success')
        self.assertEqual(self.project.status()['health'], health)
        self.assertEqual(list((self.project.root / 'runs').iterdir()), runs)
        self.assertEqual(self.project.snapshots.get(), snapshot)
        self.assertEqual(Budget(self.config.data / 'budget', 100).usage('sample')['used'], 3)

    def test_invocation_admission_and_shared_budget_are_real(self):
        self.setup_probe()
        result, _ = self.service(ProbeDriver(lambda c: admit_invocation(c, unit='query'), admitted=False))
        self.assertEqual(result['status'], 'success')
        e = self.store.read()['events'][result['id']]
        self.assertEqual(e['requests'], 1)
        self.assertTrue(e['admitted'])
        self.record_failure(run='next', at=1020)
        config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, shared_daily_requests=1))
        result, _ = self.service(config=config, at=1030)
        self.assertEqual(result['reason'], 'budget_unavailable')
        self.assertEqual(Budget(config.data / 'budget', 100, shared_daily=1).usage('sample')['used'], 1)

    def test_native_surfaces_and_command_edit_defer_without_substitution(self):
        self.setup_probe()
        self.store.path.unlink()
        self.config.profiles['primary']['options']['codemode'] = True
        self.first = self.decision(self.role)
        action = self.record_failure()
        result, driver = self.service()
        self.assertEqual(result['reason'], 'native_tools_incompatible')
        self.assertEqual(driver.calls, [])
        engines = copy.deepcopy(self.config.engines)
        engines['pi']['command'] = (*engines['pi']['command'], '--changed')
        changed = dataclasses.replace(self.config, engines=engines)
        result, driver = self.service(config=changed, at=1020)
        self.assertEqual(result['reason'], 'target_changed')
        self.assertEqual(driver.calls, [])
        self.assertTrue(active(self.contribution(action), 1021))

    def test_only_matched_probe_failures_persist_attempt_receipts(self):
        default_action = self.setup_probe()
        self.assertFalse(default_action['counts_as_failure'])
        self.assertNotIn('counts_as_failure',
                         self.store.read()['receipts'][default_action['contribution']]['action'])
        self.store.path.unlink()
        self.spec['on_error'] = [{'scope': 'group', 'seconds': 10}]
        self.store.result(self.first, run='fixed', error=ModelFailure('pi'), at=1000)
        self.store.result(self.first, run='ordinary-success', at=1001)
        self.assertEqual(self.store.read()['receipts'], {})
        self.spec['on_error'] = [{'scope': 'group', 'probe_seconds': 10,
                                  'when': {'path': 'error.code', 'op': 'eq', 'value': 'matched'}}]
        self.store.result(self.first, run='unmatched', error=ModelFailure('pi', code='other'), at=1002)
        self.assertEqual(self.store.read()['receipts'], {})
        action = self.store.result(self.first, run='matched', error=ModelFailure('pi', code='matched'), at=1003)
        state = self.store.read()
        self.assertEqual(set(state['receipts']), {action['contribution']})
        saved = self.store.path.read_bytes()
        self.assertEqual(self.store.result(self.first, run='matched', error=ModelFailure('pi', code='matched'), at=2000), action)
        self.assertEqual(self.store.path.read_bytes(), saved)

    def test_opted_in_probe_failure_policy_survives_receipt_replay(self):
        spec = specification()
        spec['on_error'] = [{'scope': 'group', 'probe_seconds': 10,
                             'counts_as_failure': True}]
        role, _ = self.setup_selection(spec)
        self.store = State(self.config)
        self.first = self.decision(role)
        action = self.record_failure()
        self.assertTrue(action['counts_as_failure'])
        receipt = self.store.read()['receipts'][action['contribution']]['action']
        self.assertTrue(receipt['counts_as_failure'])
        saved = self.store.path.read_bytes()
        self.assertEqual(self.record_failure(), action)
        self.assertEqual(self.store.path.read_bytes(), saved)

    def test_long_running_probe_events_and_ordinary_successes_stay_bounded(self):
        action = self.setup_probe()
        # One overflow beyond the eight-event history bound exercises pruning;
        # extra retries repeat the same disk-heavy path without adding coverage.
        for index in range(8):
            result, _ = self.service(ProbeDriver(admitted=False, challenge=False), at=1010 + index * 10)
            self.assertEqual(result['status'], 'failure')
        seen = []
        def inspect_active_claim(_context):
            state = self.store.read()
            c = state['contributions'][action['contribution']]
            seen.append((len([e for e in state['events'].values()
                              if e['contribution'] == action['contribution']]),
                         state['events'][c['claim']]['status']))
            status_event = self.project.status()['selection']['probe_blocks'][0]['last_result']
            self.assertNotIn('reserve', status_event)
        result, _ = self.service(ProbeDriver(inspect_active_claim, admitted=False, challenge=False), at=1210)
        self.assertEqual(result['status'], 'failure')
        self.assertEqual(seen, [(8, 'intent')])
        before = self.store.path.stat().st_size
        for index in range(100):
            self.store.result(self.first, run=f'ordinary-success-{index}', at=5000 + index)
        state = self.store.read()
        c = state['contributions'][action['contribution']]
        self.assertEqual(len(state['receipts']), 1)
        self.assertEqual(len(state['contributions']), 1)
        self.assertEqual(len(state['events']), 8)
        self.assertEqual(state['events'][c['last_event']]['status'], 'failure')
        self.assertLessEqual(self.store.path.stat().st_size, before + 256)
        self.assertLess(self.store.path.stat().st_size, 32768)

    def test_terminal_contribution_retention_keeps_active_and_latest_receipts(self):
        from mizu.selection import MAX_TERMINAL_CONTRIBUTIONS
        action = self.setup_probe()
        state = self.store.read()
        active_id = action['contribution']
        active_contribution = state['contributions'][active_id]
        active_receipt = state['receipts'][active_id]
        base = copy.deepcopy(active_contribution)
        state['contributions'] = {active_id: active_contribution}
        state['receipts'] = {active_id: active_receipt}
        state['events'] = {}
        oldest = None
        for index in range(MAX_TERMINAL_CONTRIBUTIONS):
            identity = f'{index + 1:064x}'
            if identity == active_id:
                identity = f'{index + 1001:064x}'
            claim = f'{index + 10000:064x}'
            created_at = index
            c = copy.deepcopy(base)
            c.update(created_at=created_at, next_probe_at=created_at + 10, status='recovered',
                     revision=2, claim=None, last_event=claim)
            state['contributions'][identity] = c
            receipt = copy.deepcopy(active_receipt)
            receipt['payload'] = f'{index + 1:064x}'
            receipt['action']['contribution'] = identity
            state['receipts'][identity] = receipt
            state['events'][claim] = {
                'contribution': identity, 'revision': 1, 'target': c['target'],
                'started_at': created_at, 'finished_at': created_at, 'status': 'success',
                'admitted': True, 'settled': True, 'challenge_ok': True,
                'requests': 1, 'model': None, 'reason': None}
            if index == 0:
                oldest = identity
        self.store.save(state)
        self.assertEqual(len(self.store.read()['contributions']), MAX_TERMINAL_CONTRIBUTIONS + 1)
        # The active claim remains present while the ledger already holds its
        # terminal retention limit.
        state = self.store.read()
        c = state['contributions'][active_id]
        claim = 'f' * 64
        c.update(claim=claim, last_event=claim)
        state['events'][claim] = {
            'contribution': active_id, 'revision': c['revision'], 'target': c['target'],
            'started_at': c['created_at'], 'finished_at': c['created_at'], 'status': 'success',
            'admitted': True, 'settled': True, 'challenge_ok': True,
            'requests': 1, 'model': None, 'reason': None}
        self.store.save(state)
        self.assertTrue(self.store.probe_update(active_id, revision=c['revision'], claim=claim,
                                                recover=True, at=c['created_at'] + 1))
        saved = self.store.read()
        self.assertEqual(len(saved['contributions']), MAX_TERMINAL_CONTRIBUTIONS)
        self.assertEqual(len(saved['receipts']), MAX_TERMINAL_CONTRIBUTIONS)
        self.assertEqual(len(saved['events']), MAX_TERMINAL_CONTRIBUTIONS)
        self.assertNotIn(oldest, saved['contributions'])
        self.assertNotIn(oldest, saved['receipts'])
        self.assertNotIn(oldest, {event['contribution'] for event in saved['events'].values()})
        self.assertEqual(saved['contributions'][active_id]['status'], 'recovered')
        self.assertEqual(saved['events'][saved['contributions'][active_id]['last_event']]['status'], 'success')

    def test_intent_reservation_covers_maximum_terminal_event(self):
        from mizu.probes import _event
        from mizu.selection import INTENT_RESERVE_BYTES, MAX_BYTES, validate_event
        action = self.setup_probe()
        identity = action['contribution']
        c = self.contribution(action)
        model = {'usage_known': True, 'tokens': {
            'input_tokens': 2**53, 'output_tokens': 2**53, 'cache_read_tokens': 2**53,
            'cache_write_tokens': 2**53, 'other_tokens': 2**53}}
        intent = _event(identity, c, 0)
        intent.update(requests=4, admitted=True, settled=True, challenge_ok=True, model=model)
        terminal = {**intent, 'status': 'cancelled', 'finished_at': 253402300799,
                    'reason': 'x' * 63}
        terminal.pop('reserve')
        validate_event('e' * 64, intent)
        validate_event('e' * 64, terminal)
        without_reserve = {**intent}
        without_reserve.pop('reserve')
        growth = len(canonical(terminal)) - len(canonical(without_reserve))
        self.assertLessEqual(growth, INTENT_RESERVE_BYTES)
        self.assertLessEqual(len(canonical(terminal)), len(canonical(intent)))
        self.assertLess(len(canonical(intent)), MAX_BYTES)

    def test_near_limit_admitted_probe_persists_terminal_and_recovery(self):
        from mizu.selection import MAX_BYTES
        action = self.setup_probe()
        store = self.store
        test = self

        class NearLimitDriver:
            def __init__(self):
                self.saturated = 0

            def execute(driver, context, prompt, *, profile):
                context.handle('_hello', {})
                state = store.read()
                blocks = state['blocks']
                size = len(canonical(state))
                target = MAX_BYTES - 64
                index = 0
                while True:
                    key = f'group:capacity-{index:06d}'
                    block = {'created_at': 1000, 'until': 2000, 'run': 'capacity',
                             'selector': 'dynamic', 'rule': 0, 'error_rule': 0,
                             'sponsor': 'sample', 'role': 'worker'}
                    entry = len(canonical({key: block})) - 3
                    delta = entry + bool(blocks)
                    if size + delta > target:
                        break
                    blocks[key] = block
                    size += delta
                    index += 1
                test.assertEqual(len(canonical(state)), size)
                store.save(state)
                driver.saturated = store.path.stat().st_size
                context.handle('_budget', {'sequence': 1})
                context.handle('_model_usage', {'sequence': 1, 'usage': {'input': 3, 'output': 2}})
                challenge = json.loads(prompt)['challenge']
                context.handle('finish', {'outcome': 'done', 'summary': challenge})
                return {'profile': profile, 'engine': context.config.engine(profile),
                        **context.config.model(profile), 'settled': True, 'usage_known': True,
                        'usage': [{'input': 3, 'output': 2}]}

        driver = NearLimitDriver()
        result, _ = self.service(driver)
        self.assertGreater(driver.saturated, MAX_BYTES - 512)
        self.assertLessEqual(driver.saturated, MAX_BYTES)
        self.assertEqual(result['status'], 'success')
        state = self.store.read()
        c = state['contributions'][action['contribution']]
        self.assertEqual(c['status'], 'recovered')
        terminal = state['events'][c['last_event']]
        self.assertEqual(terminal['status'], 'success')
        self.assertTrue(terminal['admitted'] and terminal['settled'] and terminal['challenge_ok'])
        self.assertNotIn('reserve', terminal)
        self.assertLessEqual(self.store.path.stat().st_size, MAX_BYTES)

    def test_byte_capacity_and_corrupt_event_fail_closed(self):
        from mizu.selection import MAX_BYTES
        action = self.setup_probe()
        state = self.store.read()
        self.fill_state_near_limit(state)
        with self.assertRaises(ConfigError): self.record_failure(run='too-large', at=1050)
        self.assertLessEqual(self.store.path.stat().st_size, MAX_BYTES)
        saved = self.store.read()
        self.assertTrue(saved['exhausted'])
        self.assertTrue(active(self.contribution(action), 1051))
        self.assertEqual(len(saved['contributions']), 1)  # the new active obligation was not evicted
        self.assertEqual(len(saved['receipts']), 1)
        # A malformed event is never silently reset or accepted as recovery.
        saved['events']['c' * 64] = {'status': 'success'}
        write_json(self.store.path, saved)
        with self.assertRaises(ConfigError): self.store.read()

    def test_durable_store_is_disposable_event_local(self):
        from mizu.pi_durable import PiDurableDriver
        self.config.profiles['primary']['engine'] = 'pi-durable'
        self.setup_probe()
        driver = PiDurableDriver(self.config)
        def settle(context, settings, model, prompt, profile, run_key, store_path):
            self.assertTrue(store_path.is_relative_to(context.run_dir))
            self.assertTrue(store_path.exists())
            context.handle('_budget', {'sequence': 1})
            context.handle('finish', {'outcome': 'done', 'summary': json.loads(prompt)['challenge']})
            return {'profile': profile, 'engine': 'pi-durable', **model, 'requests': 1, 'settled': True}
        driver._run_once = settle
        result, _ = self.service(driver)
        self.assertEqual(result['status'], 'success')
        self.assertFalse((self.config.data / 'durable').exists())
        self.assertEqual(list((self.config.data / 'selection' / 'work').iterdir()), [])

    def test_daemon_wait_uses_earlier_probe_deadline(self):
        from threading import Event
        event = Event()
        waits = []
        def wait(seconds):
            waits.append(seconds)
            event.set()
        event.wait = wait
        with patch('mizu.runtime.threading.Event', return_value=event), patch('mizu.runtime.load', return_value=self.config), \
             patch('mizu.runtime.vcs_poll_enabled', return_value=False), patch('mizu.probes.service_due', return_value=None), \
             patch('mizu.runtime.should_run', return_value=False), patch('mizu.runtime.time.time', return_value=1000), \
             patch('mizu.probes.next_deadline', return_value=1000.5), patch('builtins.print'):
            daemon(self.config, 'sample', 'worker')
        self.assertEqual(waits, [0.5])

    def test_recreated_sponsor_with_same_name_is_not_authorized(self):
        import shutil
        action = self.setup_probe()
        retired = self.project.root.with_name('retired')
        self.project.root.rename(retired)
        shutil.copytree(retired, self.project.root)
        result, driver = self.service()
        self.assertEqual(result['reason'], 'sponsor_changed')
        self.assertEqual(driver.calls, [])
        self.assertTrue(active(self.contribution(action), 1011))
        self.assertEqual(Budget(self.config.data / 'budget', 100).usage('sample')['used'], 0)

    def test_status_filters_unrelated_contributions_and_private_history(self):
        action = self.setup_probe()
        state = self.store.read()
        identity = 'a' * 64
        unrelated = {**self.contribution(action), 'key': 'group:unrelated', 'sponsor': 'another'}
        state['contributions'][identity] = unrelated
        state['receipts'][identity] = {'payload': 'b' * 64,
                                        'action': {'rule': 0, 'until': None, 'scope': 'group',
                                                   'probe_seconds': 10, 'contribution': identity}}
        state['history']['alternate'] = {'at': 1000, 'run': 'other', 'status': 'failed',
                                         'error': {'message': 'unrelated private task message'}}
        self.store.save(state)
        with patch('mizu.selection.time.time', return_value=1001):
            status = self.project.status()['selection']
        self.assertEqual(len(status['probe_blocks']), 1)
        self.assertNotIn(b'unrelated', canonical(status))
        self.assertNotIn('history', status)

    def test_daemon_probe_ticks_do_not_accelerate_ordinary_cooldown(self):
        from threading import Event
        event = Event()
        ticks = [1000.0]
        waits = []
        def wait(seconds):
            waits.append(seconds)
            ticks[0] += seconds
            if len(waits) >= 3:
                event.set()
        event.wait = wait
        config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits,
                                     idle_seconds=10, cooldown_seconds=100))
        with patch('mizu.runtime.threading.Event', return_value=event), patch('mizu.runtime.load', return_value=config), \
             patch('mizu.runtime.vcs_poll_enabled', return_value=False), patch('mizu.probes.service_due', return_value=None) as service, \
             patch('mizu.runtime.should_run', return_value=True), patch('mizu.runtime.time.monotonic', side_effect=lambda: ticks[0]), \
             patch('mizu.probes.next_deadline', return_value=None), patch('builtins.print'), \
             patch.object(Engine, 'run', return_value={'status': 'completed'}) as run:
            daemon(config, 'sample', 'worker')
        self.assertEqual(waits, [10, 10, 10])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(service.call_count, 3)
