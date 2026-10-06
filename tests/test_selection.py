"""Synthetic selection/state tests. No provider quota, payment or isolation claim."""
import copy
import dataclasses
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.budget import Budget
from mizu.cli import execute, parser
from mizu.config import load
from mizu.engine_config import effective, session_record
from mizu.errors import Cancelled, ConfigError, Denied, ModelFailure, ProtocolError
from mizu.fs import canonical, read_json, write_json
from mizu.runtime import Engine
from mizu.selection import (State, UNKNOWN, attributes, base_facts, evaluate,
                            rule_attributes, select, validate_condition, validate_selectors)
from mizu.usage import summarize


def cond(path, op, value):
    return {'path': path, 'op': op, 'value': value}


def specification():
    return {'retry_seconds': 30, 'rules': [{'candidates': [
        {'profile': 'primary', 'group': 'shared'}, {'profile': 'alternate', 'group': 'other'}]}],
        'on_error': [{'when': cond('error.code', 'eq', 'synthetic-exhausted'), 'scope': 'group', 'seconds': 100}]}


class PredicateTests(unittest.TestCase):
    def test_three_valued_conditions(self):
        missing = cond('absent', 'eq', 0)
        self.assertIs(evaluate(missing, {}), UNKNOWN)
        self.assertIs(evaluate({'not': missing}, {}), UNKNOWN)
        self.assertTrue(evaluate(cond('absent', 'exists', False), {}))
        self.assertFalse(evaluate({'all': [missing, cond('x', 'eq', 2)]}, {'x': 1}))
        self.assertTrue(evaluate({'any': [missing, cond('x', 'eq', 1)]}, {'x': 1}))
        for op, value in [('eq', 3), ('in', [2, 3]), ('gte', 3), ('lte', 3), ('lt', 4), ('gt', 2)]:
            c = cond('x', op, value)
            validate_condition(c)
            self.assertTrue(evaluate(c, {'x': 3}))
        self.assertTrue(evaluate(cond('task.goal', 'contains', 'fix'), {'task': {'goal': 'fix tests'}}))
        self.assertFalse(evaluate(cond('x', 'eq', True), {'x': 1}))

    def test_bounds_and_no_code_execution(self):
        for c in ({'eval': 'anything'}, cond('x', 'regex', '(a+)+'), cond('x', 'gt', '1'),
                  {'any': []}, {'not': {}, 'all': []}):
            with self.assertRaises(ConfigError): validate_condition(c)
        deep = {}
        for _ in range(14): deep = {'not': deep}
        with self.assertRaises(ConfigError): validate_condition(deep)
        for data in ({'x': float('nan')}, {'x': []}, {'x': 'x' * 4097}, {'x': None}):
            with self.assertRaises(ConfigError): attributes(data)


class SelectionFixture(Fixture):
    def setup_selection(self, spec=None, *, role_name='worker'):
        spec = copy.deepcopy(spec or specification())
        validate_selectors({'dynamic': spec}, self.config.profiles, self.file.parent)
        role = dataclasses.replace(self.config.roles[role_name], profile='', selector='dynamic', on_change=False)
        self.config = dataclasses.replace(self.config, selectors={'dynamic': spec}, roles={**self.config.roles, role_name: role})
        self.project.config = self.config
        return role, spec

    def decision(self, role, at=1000, explicit=None):
        facts, sources = base_facts(self.project, role, self.project.snapshots.get(), explicit)
        rule_attributes(self.config.selectors[role.selector], facts, sources)
        return select(self.config, role, facts, sources, at=at)


class SelectionTests(SelectionFixture):
    def test_exhaustion_alternate_recovery_and_restart(self):
        role, _ = self.setup_selection()
        first = self.decision(role)
        self.assertEqual(first['profile'], 'primary')
        action = State(self.config).result(first, run='a', at=1000,
            error=ModelFailure('synthetic', code='synthetic-exhausted', message='no capacity'))
        self.assertEqual(action['until'], 1100)
        self.assertEqual(self.decision(role, 1010)['profile'], 'alternate')
        # A newly constructed state reader has the same block, with an exact boundary.
        self.assertEqual(State(self.config).read()['blocks']['group:shared']['until'], 1100)
        self.assertEqual(self.decision(role, 1100)['profile'], 'primary')

    def test_shared_group_multiple_profiles_and_first_rule(self):
        spec = specification()
        spec['rules'][0]['candidates'][1]['group'] = 'shared'
        spec['rules'].append({'candidates': [{'profile': 'alternate'}]})
        role, _ = self.setup_selection(spec)
        decision = self.decision(role)
        State(self.config).result(decision, run='a', at=1000,
                                 error=ModelFailure('different-provider', code='synthetic-exhausted'))
        blocked = self.decision(role, 1001)
        self.assertIsNone(blocked['profile'])
        self.assertEqual(blocked['rule'], 0)  # no fallthrough to another matching rule
        self.assertEqual(len(blocked['excluded']), 2)

    def test_explicit_new_observation_can_recover_early(self):
        spec = specification()
        spec['rules'][0]['candidates'][0]['recover_when'] = cond('observations.shared.facts.available', 'eq', True)
        role, _ = self.setup_selection(spec)
        state = State(self.config)
        state.result(self.decision(role), run='a', at=1000, error=ModelFailure('any', code='synthetic-exhausted'))
        state.observe({'group': 'shared', 'observed_at': 999, 'expires_at': 1200, 'facts': {'available': True}})
        self.assertEqual(self.decision(role, 1002)['profile'], 'alternate')
        state.observe({'group': 'shared', 'observed_at': 1001, 'expires_at': 1050, 'facts': {'available': True}})
        self.assertEqual(self.decision(role, 1002)['profile'], 'primary')
        # Expired evidence cannot override a still-active block.
        self.assertEqual(self.decision(role, 1050)['profile'], 'alternate')

    def test_stale_unknown_conditions_and_observation_validation(self):
        spec = specification()
        spec['rules'][0]['candidates'][0]['when'] = cond('observations.shared.facts.remaining', 'gt', 0)
        role, _ = self.setup_selection(spec)
        self.assertEqual(self.decision(role)['profile'], 'alternate')
        state = State(self.config)
        observation = {'group': 'shared', 'observed_at': 999, 'expires_at': 1005, 'facts': {'remaining': 2}}
        state.observe(observation)
        self.assertEqual(self.decision(role)['profile'], 'primary')
        self.assertEqual(self.decision(role, 1005)['profile'], 'alternate')
        for bad in ({**observation, 'group': 'undeclared'}, {**observation, 'expires_at': 998},
                    {**observation, 'observed_at': 998}, {**observation, 'facts': {'remaining': 0}}):
            with self.assertRaises(ConfigError): state.observe(bad)
        state.observe(observation)  # idempotent
        state.delete('shared')
        self.assertEqual(self.decision(role)['profile'], 'alternate')

    def test_prior_results_and_attributes(self):
        spec = specification()
        spec['classify_rules'] = [
            {'when': cond('task.goal', 'contains', 'correctness'), 'attributes': {'difficulty': 'hard', 'method': 'first'}},
            {'attributes': {'method': 'second'}}]
        spec['rules'] = [{'when': cond('attributes.difficulty', 'eq', 'easy'), 'candidates': [{'profile': 'alternate'}]},
                         {'candidates': [{'profile': 'primary'}]}]
        role, _ = self.setup_selection(spec)
        self.project.settings['attributes'] = {'difficulty': 'project'}
        role = dataclasses.replace(role, attributes={'difficulty': 'role'})
        result = self.decision(role, explicit={'difficulty': 'easy'})
        self.assertEqual(result['profile'], 'alternate')
        self.assertEqual(result['facts']['attributes']['method'], 'first')
        self.assertEqual(result['attribute_sources']['difficulty'], 'run')
        State(self.config).result(result, run='done', at=1000)
        self.assertEqual(self.decision(role, 1001)['facts']['history']['alternate']['status'], 'completed')

    def test_error_rules_missing_time_and_unmatched(self):
        spec = specification()
        spec['on_error'] = [{'scope': 'profile', 'until': 'error.retry_at'},
                            {'scope': 'profile', 'seconds': 5}]
        role, _ = self.setup_selection(spec)
        decision = self.decision(role)
        state = State(self.config)
        self.assertEqual(state.result(decision, run='a', at=1000, error=ModelFailure('custom'))['rule'], 1)
        self.assertEqual(state.result(decision, run='b', at=1000, error=ModelFailure('custom', retry_at=1010))['rule'], 0)
        self.assertIsNone(state.result(decision, run='c', at=1001, error=ProtocolError('local invariant')))
        self.assertEqual(state.read()['blocks']['profile:primary']['until'], 1010)

    def test_concurrent_state_writes_and_corruption(self):
        role, _ = self.setup_selection()
        decision = self.decision(role)
        def update(index):
            return State(self.config).result(decision, run=str(index), at=1000+index,
                                            error=ModelFailure('custom', code='synthetic-exhausted'))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(update, range(8)))
        saved = State(self.config).read()
        self.assertEqual(saved['blocks']['group:shared']['until'], 1107)
        self.assertEqual(saved['history']['primary']['at'], 1007)
        State(self.config).path.write_text('{broken')
        with self.assertRaises(ConfigError): self.decision(role)

    def test_runtime_no_replay_and_no_failure_pause(self):
        role, _ = self.setup_selection()
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, max_failures=1))
        before = self.project.snapshots.get()['id']
        def callback(ctx, prompt, profile):
            if ctx.role.profile == 'primary':
                raise ModelFailure('custom', code='synthetic-exhausted')
        driver = ScriptDriver(callback)
        with patch('mizu.selection.time.time', return_value=1000):
            first = Engine(self.config, driver=driver).run(self.project, role.name)
        self.assertEqual(first['status'], 'deferred')
        self.assertEqual(len(driver.calls), 1)
        self.assertFalse(self.project.control()['paused'])
        self.assertEqual(self.project.snapshots.get()['id'], before)
        failure = read_json(self.project.root/'runs'/first['run']/'error.json')
        self.assertEqual(failure['model_failure']['code'], 'synthetic-exhausted')
        with patch('mizu.selection.time.time', return_value=1001):
            second = Engine(self.config, driver=driver).run(self.project, role.name)
        self.assertEqual(second['selection']['profile'], 'alternate')
        self.assertEqual(driver.calls[-1][0].role.capabilities, role.capabilities)
        self.assertEqual(driver.calls[-1][0].role.engine_tools, role.engine_tools)

    def test_local_invariants_and_cancellation_do_not_switch(self):
        for error in (Denied('no grant'), ProtocolError('bad handshake'), Cancelled('operator stop')):
            with self.subTest(error=type(error).__name__):
                role, _ = self.setup_selection()
                self.project.set_control(paused=False)
                def callback(*_): raise error
                with self.assertRaises(type(error)):
                    Engine(self.config, driver=ScriptDriver(callback)).run(self.project, role.name)
                self.assertEqual(State(self.config).read()['blocks'], {})

    def test_local_admission_wrapped_by_engine_cannot_trigger_switch(self):
        role, spec = self.setup_selection()
        spec['on_error'] = [{'scope': 'profile', 'seconds': 10}]
        def callback(ctx, *_):
            ctx.admission_error = 'synthetic local budget refusal'
            raise ModelFailure('pi', message=ctx.admission_error)
        with self.assertRaises(ProtocolError) as caught:
            Engine(self.config, driver=ScriptDriver(callback)).run(self.project, role.name)
        self.assertNotIsInstance(caught.exception, ModelFailure)
        self.assertEqual(State(self.config).read()['blocks'], {})

    def test_selector_schema_rejects_invalid_definitions(self):
        for update in ({'retry_seconds': 0}, {'rules': []}, {'unknown': True},
                       {'rules': [{'candidates': [{'profile': 'absent'}]}]},
                       {'on_error': [{'scope': 'group', 'seconds': 10, 'until': 'error.retry_at'}]}):
            spec = {**specification(), **update}
            with self.subTest(update=update), self.assertRaises(ConfigError):
                validate_selectors({'dynamic': spec}, self.config.profiles, self.file.parent)

    def test_all_unavailable_waits_without_dispatch_or_publication(self):
        spec = specification()
        for candidate in spec['rules'][0]['candidates']:
            candidate['when'] = cond('attributes.missing', 'eq', True)
        role, _ = self.setup_selection(spec)
        driver = ScriptDriver()
        before = self.project.snapshots.get()['id']
        result = Engine(self.config, driver=driver).run(self.project, role.name)
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(driver.calls, [])
        self.assertEqual(self.project.snapshots.get()['id'], before)
        self.assertFalse(self.project.control()['paused'])

    def test_session_identity_changes_with_profile(self):
        self.setup_selection()
        ctx = self.context()
        first, _ = session_record(ctx, 'primary', effective(self.config, ctx.role, 'primary'))
        changed = dataclasses.replace(self.config, profiles={**self.config.profiles, 'primary': {
            **self.config.profiles['primary'], 'model': 'different'}})
        ctx.config = changed
        second, _ = session_record(ctx, 'primary', effective(changed, ctx.role, 'primary'))
        self.assertNotEqual(first, second)

    def test_backup_restore_preserves_project_attributes_and_local_cache(self):
        from mizu.storage import backup, restore
        from mizu.project import Project
        self.setup_selection()
        path = self.project.root/'project.toml'
        path.write_text(path.read_text() + '\n[attributes]\ndifficulty = "hard"\n')
        write_json(self.project.root/'selection'/'worker-classification.json', {'synthetic': 'cache'})
        self.project.set_control(paused=True)
        archive = self.root/'backup.tar.gz'
        backup(self.project, archive, verify=True)
        restore(self.config, 'restored', archive)
        restored = Project(self.config, 'restored')
        self.assertEqual(restored.settings['attributes']['difficulty'], 'hard')
        self.assertTrue((restored.root/'selection'/'worker-classification.json').is_file())
        self.assertFalse(restored.control()['armed'])

    def test_toml_and_cli_preview_observe_delete(self):
        text = self.file.read_text().replace('[roles.worker]\nprofile = "primary"', '[roles.worker]\nselector = "dynamic"')
        text += '''\n[selectors.dynamic]
retry_seconds = 30
[[selectors.dynamic.rules]]
candidates = [{profile="primary", group="shared"}]
'''
        self.file.write_text(text)
        config = load(self.file)
        self.assertEqual(config.roles['worker'].selector, 'dynamic')
        def cli(*args):
            return execute(parser().parse_args(['--config', str(self.file), 'selection', *args]))
        before = set(self.project.root.rglob('*'))
        result = cli('preview', 'sample', '--role', 'worker')
        self.assertEqual(result['profile'], 'primary')
        self.assertEqual(before, set(self.project.root.rglob('*')))
        self.assertFalse((config.data/'selection').exists())
        file = self.root/'observation.json'
        file.write_text(json.dumps({'group': 'shared', 'observed_at': 1000, 'expires_at': 2000, 'facts': {'remaining': 10}}))
        cli('observe', '--file', str(file))
        self.assertIn('shared', cli('status')['observations'])
        cli('delete', 'shared')
        self.assertEqual(cli('status')['observations'], {})
        self.file.write_text(text.replace('selector = "dynamic"', 'selector = "dynamic"\nprofile = "primary"'))
        with self.assertRaises(ConfigError): load(self.file)


class ClassificationTests(SelectionFixture):
    def classifier(self, on_failure='continue'):
        policy = self.root/'classifier.md'
        policy.write_text('Classify the supplied task. Finish with summary containing only attribute JSON.')
        spec = specification()
        spec['classifier'] = {'profile': 'alternate', 'policy': str(policy), 'on_failure': on_failure,
            'retry_seconds': 60, 'attributes': {'difficulty': {'type': 'string', 'values': ['hard', 'easy']}}}
        return self.setup_selection(spec)

    def classifying_calls(self, driver):
        return [call for call in driver.calls if call[0].role.workspace == 'none']

    def test_explicit_labels_skip_inference_and_keep_precedence(self):
        role, _ = self.classifier()
        def callback(ctx, prompt, profile):
            if ctx.role.workspace == 'none' and ctx.role.profile == 'alternate':
                self.assertEqual(ctx.role.capabilities, ('finish',))
                self.assertEqual(ctx.role.engine_tools, ())
                self.assertTrue(ctx.ephemeral)
                with self.assertRaises(Denied): ctx.handle('exec', {'command': 'not executed'})
                ctx.handle('finish', {'outcome': 'done', 'summary': '{"difficulty":"hard"}'})
        driver = ScriptDriver(callback)
        engine = Engine(self.config, driver=driver)
        before = Budget(self.config.data/'budget', 100).usage()['used']
        first = engine.run(self.project, role.name, attributes={'difficulty': 'easy'})
        self.assertEqual(first['selection']['facts']['attributes']['difficulty'], 'easy')
        self.assertEqual(first['selection']['attribute_sources']['difficulty'], 'run')
        self.assertEqual(first['selection']['classification']['status'], 'explicit')
        self.assertEqual(first['selection']['classification']['source'], 'explicit')
        # No inference work unit ran: only the main execution consumed a request.
        self.assertEqual(self.classifying_calls(driver), [])
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(Budget(self.config.data/'budget', 100).usage()['used'] - before, 1)
        engine.run(self.project, role.name, attributes={'difficulty': 'easy'})
        self.assertEqual(self.classifying_calls(driver), [])
        result = engine.run(self.project, role.name, attributes={'difficulty': 'hard'})
        self.assertEqual(result['selection']['facts']['attributes']['difficulty'], 'hard')
        self.assertEqual(result['selection']['classification']['status'], 'explicit')
        self.assertEqual(self.classifying_calls(driver), [])

    def test_inference_cache_budget_records_and_single_use_between_units(self):
        role, _ = self.classifier()
        prompts = []
        def callback(ctx, prompt, profile):
            if ctx.role.workspace == 'none':
                prompts.append(json.loads(prompt))
                ctx.handle('finish', {'outcome': 'done', 'summary': '{"difficulty":"hard"}'})
        driver = ScriptDriver(callback)
        engine = Engine(self.config, driver=driver)
        before = Budget(self.config.data/'budget', 100).usage()['used']
        first = engine.run(self.project, role.name)
        classification = first['selection']['classification']
        self.assertEqual(classification['status'], 'completed')
        self.assertEqual(classification['source'], 'inference')
        self.assertIn('material inputs', classification['reason'])
        self.assertEqual(classification['model']['requests'], 1)
        self.assertEqual(first['selection']['facts']['attributes']['difficulty'], 'hard')
        self.assertEqual(len(self.classifying_calls(driver)), 1)
        self.assertEqual(Budget(self.config.data/'budget', 100).usage()['used'] - before, 2)
        saved = read_json(self.project.root/'selection'/f'{role.name}-classification.json')
        self.assertEqual(saved['key'], classification['key'])
        # An unchanged second unit reuses the cache without new inference.
        second = engine.run(self.project, role.name)
        self.assertEqual(second['selection']['classification']['status'], 'cached')
        self.assertEqual(second['selection']['classification']['source'], 'cache')
        self.assertEqual(len(self.classifying_calls(driver)), 1)
        runs = [path.parent for path in self.project.root.glob('runs/*/started.json')
                if read_json(path).get('kind') == 'classification']
        self.assertEqual(len(runs), 1)

    def test_idle_state_churn_reuses_cache_while_new_work_reclassifies(self):
        role, _ = self.classifier()
        inferred = []
        def callback(ctx, prompt, profile):
            if ctx.role.workspace == 'none':
                inferred.append(json.loads(prompt))
                ctx.handle('finish', {'outcome': 'done', 'summary': '{"difficulty":"hard"}'})
        driver = ScriptDriver(callback)
        engine = Engine(self.config, driver=driver)
        engine.run(self.project, role.name)
        self.assertEqual(len(inferred), 1)
        self.assertEqual(inferred[0]['proposals'], [])
        # Bookkeeping state churn from an unrelated worker unit keeps the key.
        Engine(self.config, driver=ScriptDriver()).run(self.project, 'worker')
        result = engine.run(self.project, role.name)
        self.assertEqual(result['selection']['classification']['status'], 'cached')
        self.assertEqual(len(inferred), 1)
        # A newly actionable proposal revision is material: inference runs again.
        proposal = self.project.insights.submit(source='worker', title='Fresh risk', body='evidence',
                                                base_snapshot=self.project.snapshots.get()['id'])
        result = engine.run(self.project, role.name)
        classification = result['selection']['classification']
        self.assertEqual(classification['status'], 'completed')
        self.assertEqual(len(inferred), 2)
        self.assertEqual(inferred[1]['proposals'][0]['id'], proposal['id'])
        self.assertEqual(inferred[1]['proposals'][0]['rev'], proposal['rev'])
        # Fresh review evidence routed to the role is material as well.
        self.project.insights.decide(proposal['id'], 'reject', 'not yet', '', role.name)
        evidence = engine.run(self.project, role.name)['selection']['classification']
        self.assertEqual(evidence['status'], 'completed')
        self.assertEqual(len(inferred), 3)
        self.assertEqual(inferred[2]['evidence']['decisions'][0]['insight'], proposal['id'])
        self.assertIn('verified', inferred[2]['evidence']['snapshot'])

    def test_max_bounds_shed_oldest_instead_of_failing(self):
        from mizu.selection import MAX_WORK_BYTES, work_inputs
        role, spec = self.classifier()
        spec['classifier']['inputs'] = {'max_proposals': 64, 'max_decisions': 64,
                                         'max_text_bytes': 65536}
        validate_selectors({'dynamic': spec}, self.config.profiles, self.file.parent)
        bounds = spec['classifier']['inputs']
        for index in range(3):
            write_json(self.project.root/'inbox'/f'heavy-{index}.json',
                       {'id': f'heavy-{index}', 'source': 'worker', 'title': f'{index}-' + 'x' * 50000,
                        'body': 'bulk', 'base_snapshot': self.project.snapshots.get()['id'],
                        'created_at': f'2026-10-06T0{index}:00:00+00:00', 'rev': 1})
        snapshot = self.project.snapshots.get()
        work = work_inputs(self.project, role, snapshot, bounds)
        self.assertLessEqual(len(canonical(work)), MAX_WORK_BYTES)
        # Oldest-first shedding retains the newest workload.
        self.assertEqual([item['id'] for item in work['proposals']], ['heavy-1', 'heavy-2'])
        def callback(ctx, *_):
            if ctx.role.workspace == 'none':
                ctx.handle('finish', {'outcome': 'done', 'summary': '{"difficulty":"hard"}'})
        result = Engine(self.config, driver=ScriptDriver(callback)).run(self.project, role.name)
        self.assertEqual(result['selection']['classification']['status'], 'completed')

    def test_shedding_measures_returned_shape(self):
        from mizu.selection import MAX_WORK_BYTES, work_inputs
        role, spec = self.classifier()
        spec['classifier']['inputs'] = {'max_proposals': 64, 'max_decisions': 64,
                                         'max_text_bytes': 65536}
        validate_selectors({'dynamic': spec}, self.config.profiles, self.file.parent)
        bounds = spec['classifier']['inputs']
        sizes = (65536, 65331)
        for index, size in enumerate(sizes):
            write_json(self.project.root/'inbox'/f'edge-{index}.json',
                       {'id': f'edge-{index}', 'source': 'worker', 'title': f'{index}-' + 'x' * size,
                        'body': 'bulk', 'base_snapshot': self.project.snapshots.get()['id'],
                        'created_at': f'2026-10-06T0{index}:00:00+00:00', 'rev': 1})
        work = work_inputs(self.project, role, self.project.snapshots.get(), bounds)
        self.assertLessEqual(len(canonical(work)), MAX_WORK_BYTES)
        self.assertEqual([item['id'] for item in work['proposals']], ['edge-1'])

    def test_inputs_bounds_validated_defaulted_and_missing_classifier(self):
        role, spec = self.classifier()
        from mizu.selection import DEFAULT_CLASSIFIER_INPUTS
        self.assertEqual(spec['classifier']['inputs'], DEFAULT_CLASSIFIER_INPUTS)
        spec['classifier']['inputs'] = {'max_proposals': 0, 'max_decisions': 2, 'max_text_bytes': 256}
        validate_selectors({'dynamic': spec}, self.config.profiles, self.file.parent)
        self.assertEqual(spec['classifier']['inputs']['max_proposals'], 0)
        for bad in ({'max_proposals': 65}, {'max_text_bytes': 100}, {'unknown': 1},
                    {'max_decisions': 'many'}):
            broken = copy.deepcopy(spec)
            broken['classifier']['inputs'] = bad
            with self.assertRaises(ConfigError):
                validate_selectors({'dynamic': broken}, self.config.profiles, self.file.parent)
        plain = specification()
        role, _ = self.setup_selection(plain)
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, role.name)
        self.assertIsNone(result['selection']['classification'])

    def test_classifier_invalid_output_wait_retry_and_preview(self):
        role, _ = self.classifier('wait')
        driver = ScriptDriver(lambda ctx, *_: ctx.handle('finish', {'outcome': 'done', 'summary': '{"undeclared":true}'}))
        engine = Engine(self.config, driver=driver)
        before = set(self.project.root.rglob('*'))
        preview = engine.preview(self.project, role.name)
        self.assertTrue(preview['provisional'])
        self.assertEqual(driver.calls, [])
        self.assertEqual(before, set(self.project.root.rglob('*')))
        result = engine.run(self.project, role.name)
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(len(driver.calls), 1)
        engine.run(self.project, role.name)
        self.assertEqual(len(driver.calls), 1)  # no retry before configured interval
        self.assertFalse(self.project.control()['paused'])

    def test_classifier_failure_continue_and_local_error_propagates(self):
        role, _ = self.classifier()
        def callback(ctx, *_):
            if ctx.role.workspace == 'none':
                raise ModelFailure('custom', message='unavailable')
        result = Engine(self.config, driver=ScriptDriver(callback)).run(self.project, role.name)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['selection']['classification']['status'], 'failed')
        (self.project.root/'selection'/f'{role.name}-classification.json').unlink()
        def broken(*_): raise ProtocolError('handshake mismatch')
        with self.assertRaises(ProtocolError): Engine(self.config, driver=ScriptDriver(broken)).run(self.project, role.name)

    def test_rule_labels_override_inference_and_classifier_output_is_checked(self):
        role, spec = self.classifier()
        spec['classify_rules'] = [{'attributes': {'difficulty': 'easy'}}]
        def callback(ctx, *_):
            if ctx.role.workspace == 'none':
                ctx.handle('finish', {'outcome': 'done', 'summary': '{"difficulty":"hard"}'})
        driver = ScriptDriver(callback)
        result = Engine(self.config, driver=driver).run(self.project, role.name)
        self.assertEqual(result['selection']['facts']['attributes']['difficulty'], 'easy')
        self.assertEqual(result['selection']['attribute_sources']['difficulty'], 'rule:0')
        self.assertEqual(result['selection']['classification']['status'], 'explicit')
        self.assertEqual(self.classifying_calls(driver), [])

    def test_classifier_and_work_share_daily_budget(self):
        role, _ = self.classifier()
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, daily_requests=1))
        def callback(ctx, *_):
            if ctx.role.workspace == 'none':
                ctx.handle('finish', {'outcome': 'done', 'summary': '{"difficulty":"hard"}'})
        from mizu.errors import LimitExceeded
        with self.assertRaises(LimitExceeded):
            Engine(self.config, driver=ScriptDriver(callback)).run(self.project, role.name)
        self.assertEqual(Budget(self.config.data/'budget', 1).usage()['used'], 1)
        self.assertEqual(State(self.config).read()['blocks'], {})

    def test_classifier_cancellation(self):
        role, _ = self.classifier()
        stop = threading.Event()
        def callback(ctx, *_): stop.set()
        with self.assertRaises(Cancelled):
            Engine(self.config, driver=ScriptDriver(callback), stop=stop).run(self.project, role.name)
        self.assertFalse((self.project.root/'selection'/f'{role.name}-classification.json').exists())
        self.assertEqual(State(self.config).read()['blocks'], {})


class AttributionTests(Fixture):
    def test_multiple_actual_models_and_unattributed_admissions(self):
        directory = self.project.root/'runs'/'synthetic'
        write_json(directory/'result.json', {'run': 'synthetic', 'status': 'completed',
            'finished_at': '2026-10-03T00:00:00+00:00', 'model': {
                'engine': 'pi', 'provider': 'virtual', 'model': 'router', 'requests': 3,
                'request_unit': 'model_request', 'usage_known': True,
                'usage_observations': [
                    {'provider': 'provider-a', 'model': 'free', 'usage': {'input': 10, 'output': 2}},
                    {'provider': 'provider-b', 'model': 'paid', 'usage': {'input': 20, 'output': 4}},
                    {'provider': None, 'model': None, 'usage': {'input': 5, 'output': 1}}]}})
        summary = summarize(self.project)
        self.assertEqual(summary['totals']['runs'], 1)
        self.assertEqual(summary['totals']['requests'], 3)
        self.assertEqual(summary['totals']['input_tokens'], 35)
        self.assertEqual(summary['totals']['unknown_request_runs'], 0)
        self.assertEqual(summary['totals']['unknown_usage_runs'], 0)
        groups = {(g['provider'], g['model']): g for g in summary['groups']}
        self.assertNotIn(('virtual', 'router'), groups)
        self.assertEqual(groups['provider-a', 'free']['input_tokens'], 10)
        self.assertEqual(groups['provider-b', 'paid']['input_tokens'], 20)
        self.assertEqual(groups['unknown', 'unknown']['input_tokens'], 5)
        self.assertEqual(groups['unknown', 'unknown']['requests'], 3)
