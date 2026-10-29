"""Host-side Pi family ownership and authorization; SDK tests live in pi-reasoning."""
import copy
import dataclasses
import shutil
from unittest import mock

from support import Fixture
from mizu.classification import prepare
from mizu.engine_config import effective, save_session, session_record, session_generation
from mizu.errors import ConfigError, ProtocolError, ModelFailure, LimitExceeded
from mizu.fs import read_json, write_json
from mizu.pi_reselection import Reselection
from mizu.pi import PiDriver
from mizu.runtime import Engine
from mizu.selection import State, validate_selectors


class PiReselectionTests(Fixture):
    def setUp(self):
        super().setUp()
        for name in ('primary', 'alternate'):
            self.config.profiles[name].update(engine='pi', session='persistent', options={}, model=name)
        self.spec = {'retry_seconds': 30, 'reselection_interval_seconds': 10,
                     'rules': [{'candidates': [{'profile': 'primary'}, {'profile': 'alternate'}]}]}
        self.role = dataclasses.replace(self.config.roles['worker'], selector='pick')
        self.config.roles['worker'] = self.role
        self.config.selectors['pick'] = self.spec
        validate_selectors(self.config.selectors, self.config.profiles, self.file.parent)

    def bound(self, profile='alternate'):
        context = self.context('worker')
        decision = prepare(Engine(self.config), self.project, self.role, context.snapshot)
        decision.update(profile=profile, rule=0)
        context.role = dataclasses.replace(self.role, profile=profile, selector='')
        context.reselection_role, context.reselection_decision = self.role, decision
        context.model_evidence = {'profile': profile, **self.config.model(profile)}
        return context

    def record(self, context, profile):
        return session_record(context, profile, effective(self.config, context.role, profile))

    def test_absent_and_zero_preserve_separate_profile_histories(self):
        for interval in (None, 0):
            if interval is None:
                self.spec.pop('reselection_interval_seconds')
            else:
                self.spec['reselection_interval_seconds'] = interval
            context = self.bound()
            a, _ = self.record(context, 'primary')
            b, _ = self.record(context, 'alternate')
            self.assertNotEqual(a, b)
            self.assertFalse(Reselection(context).enabled)
            self.assertFalse((a.parent.parent / 'families').exists())

    def test_adopt_only_dispatch_candidate_and_resume_same_target(self):
        self.spec['reselection_interval_seconds'] = 0
        context = self.bound()
        a, _ = self.record(context, 'primary')
        b, _ = self.record(context, 'alternate')
        save_session(a, 'unrelated-history', {'tokens': 99})
        save_session(b, 'entry-history', {'tokens': 7})
        write_json(b.parent / 'generation.json', {'generation': 3})
        before_a, before_b = a.read_bytes(), b.read_bytes()
        self.spec['reselection_interval_seconds'] = 10
        context = self.bound()
        adopted, saved = self.record(context, 'alternate')
        self.assertEqual(adopted, b)
        self.assertEqual(saved['id'], 'entry-history')
        self.assertEqual(b.read_bytes(), before_b)
        ref = next((b.parent.parent / 'families').glob('*.json'))
        ref.unlink()  # recover an atomic-write interruption from the owner record
        next_unit = self.bound('primary')
        resumed, record = self.record(next_unit, 'primary')
        self.assertEqual(resumed, b)
        self.assertEqual(record, saved)
        self.assertEqual(session_generation(resumed.parent), 3)
        self.assertEqual(a.read_bytes(), before_a)
        self.spec['reselection_interval_seconds'] = 20
        next_unit = self.bound('primary')
        resumed, record = self.record(next_unit, 'primary')
        self.assertEqual(resumed, b, 'cadence tuning must not fork the conversation family')
        self.assertEqual(record, saved)

    def test_disabling_family_isolates_owned_profile_and_reenable_resumes_lineage(self):
        enabled = self.bound('alternate')
        family, _ = self.record(enabled, 'alternate')
        save_session(family, 'shared-family-history', {'tokens': 11})

        self.spec['reselection_interval_seconds'] = 0
        disabled = self.bound('alternate')
        isolated, saved = self.record(disabled, 'alternate')
        self.assertNotEqual(isolated, family)
        self.assertIsNone(saved)
        save_session(isolated, 'disabled-profile-history', {'tokens': 3})
        self.assertEqual(read_json(family)['id'], 'shared-family-history')

        self.spec['reselection_interval_seconds'] = 10
        reenabled = self.bound('primary')
        resumed, saved = self.record(reenabled, 'primary')
        self.assertEqual(resumed, family)
        self.assertEqual(saved['id'], 'shared-family-history')
        self.assertEqual(read_json(isolated)['id'], 'disabled-profile-history')

    def test_disabled_mode_isolates_pointer_only_family_when_owner_claim_is_missing(self):
        enabled = self.bound('alternate')
        family, _ = self.record(enabled, 'alternate')
        save_session(family, 'family-history', {'tokens': 5})
        (family.parent / 'family-owner.json').unlink()

        self.spec['reselection_interval_seconds'] = 0
        disabled = self.bound('alternate')
        isolated, saved = self.record(disabled, 'alternate')
        self.assertNotEqual(isolated, family)
        self.assertIsNone(saved)
        self.assertEqual(read_json(family)['id'], 'family-history')

        self.spec['reselection_interval_seconds'] = 10
        reenabled = self.bound('alternate')
        with self.assertRaisesRegex(ConfigError, 'unowned'):
            self.record(reenabled, 'alternate')

    def test_disabling_selector_by_switching_role_to_fixed_profile_isolates_owned_history(self):
        selected = self.bound('alternate')
        family, _ = self.record(selected, 'alternate')
        save_session(family, 'family-history', {'tokens': 5})
        fixed = dataclasses.replace(self.role, selector='', profile='alternate')
        self.config.roles['worker'] = fixed
        context = self.context('worker')
        context.role = fixed
        context.reselection_role = fixed
        context.reselection_decision = None
        isolated, saved = self.record(context, 'alternate')
        self.assertNotEqual(isolated, family)
        self.assertIsNone(saved)
        self.assertEqual(read_json(family)['id'], 'family-history')

    def test_invalid_dangling_and_unsafe_reference_fail_closed(self):
        context = self.bound()
        path, _ = self.record(context, 'alternate')
        ref = next((path.parent.parent / 'families').glob('*.json'))
        original = read_json(ref)
        for value in ({}, {**original, 'target': '../elsewhere'}, {**original, 'binding': '0'*64},
                      {**original, 'entry': 'primary'}):
            write_json(ref, value)
            with self.assertRaises(ConfigError):
                self.record(context, 'alternate')
        write_json(ref, original)
        (path.parent / 'family-owner.json').unlink()
        with self.assertRaisesRegex(ConfigError, 'unowned'):
            self.record(context, 'alternate')
        write_json(path.parent / 'family-owner.json', original)
        shutil.rmtree(path.parent)
        with self.assertRaises(ConfigError):
            self.record(context, 'alternate')
        ref.unlink()
        try:
            ref.symlink_to(context.run_dir / 'missing')
        except OSError:
            self.skipTest('symlink creation not supported')
        with self.assertRaises(ConfigError):
            self.record(context, 'alternate')

    def test_selector_redefinition_cannot_re_adopt_family_owned_history(self):
        original = self.bound('primary')
        path, _ = self.record(original, 'primary')
        save_session(path, 'family-history', {'tokens': 4})
        other = copy.deepcopy(self.spec)
        self.config.selectors['other'] = other
        new_role = dataclasses.replace(self.role, selector='other')
        decision = prepare(Engine(self.config), self.project, new_role, original.snapshot)
        context = self.context('worker')
        context.role = dataclasses.replace(new_role, profile=decision['profile'], selector='')
        context.reselection_role, context.reselection_decision = new_role, decision
        with self.assertRaisesRegex(ConfigError, 'reference|ownership'):
            self.record(context, 'primary')
        self.assertTrue(path.exists())

    def test_selector_definition_edit_fails_closed_without_forking_reference(self):
        original = self.bound('primary')
        path, _ = self.record(original, 'primary')
        save_session(path, 'family-history', {'tokens': 4})
        before = path.read_bytes()
        self.spec['rules'][0]['candidates'][0]['group'] = 'changed-group'
        validate_selectors(self.config.selectors, self.config.profiles, self.file.parent)
        changed = self.bound('primary')
        with self.assertRaisesRegex(ConfigError, 'reference'):
            self.record(changed, 'primary')
        self.assertEqual(path.read_bytes(), before)

    def test_incompatible_and_ephemeral_candidates_refused(self):
        for update in ({'engine': 'pi-durable'}, {'session': 'ephemeral'},
                       {'options': {'settings': {'transport': 'sse'}}}, {'mcp_servers': {'x': {}}}):
            old = copy.deepcopy(self.config.profiles['primary'])
            self.config.profiles['primary'].update(update)
            with self.assertRaises(ConfigError):
                self.record(self.bound(), 'alternate')
            self.config.profiles['primary'] = old
        context = self.bound()
        context.ephemeral = True
        with self.assertRaises(ConfigError):
            self.record(context, 'alternate')

    def test_selector_interval_validation_and_non_pi_rejection(self):
        for value in (-1, True, 31536001, 0.5):
            self.spec['reselection_interval_seconds'] = value
            with self.assertRaises(ConfigError):
                validate_selectors(self.config.selectors, self.config.profiles, self.file.parent)
        self.spec['reselection_interval_seconds'] = 10
        self.config.profiles['primary']['engine'] = 'claude'
        with self.assertRaises(ConfigError):
            validate_selectors(self.config.selectors, self.config.profiles, self.file.parent)

    def test_cadence_upgrade_commit_and_budget_are_host_owned(self):
        context = self.bound()
        clock = [100.0]
        control = Reselection(context, clock=lambda: clock[0])
        role_before = context.role
        context.handle = mock.Mock(return_value={'admitted': True})
        with mock.patch('mizu.selection.select', wraps=__import__('mizu.selection', fromlist=['select']).select) as select:
            clock[0] = 109
            self.assertEqual(control.handle('_pi_reselect', {}), {'switch': None})
            select.assert_not_called()
            clock[0] = 110
            switch = control.handle('_pi_reselect', {})['switch']
            self.assertEqual(switch['model'], self.config.model('primary')['model'])
            self.assertEqual(context.reselection_decision['profile'], 'alternate')
            self.assertEqual(context.role, role_before)
            self.assertEqual(context.request_count, 0)
            control.handle('_pi_reselect_applied', {**self.config.model('primary'), 'authorization': switch['authorization']})
            self.assertEqual(context.reselection_decision['profile'], 'primary')
            self.assertEqual(context.role.capabilities, role_before.capabilities)
            control.handle('_budget', {'sequence': 1})
            context.handle.assert_called_once_with('_budget', {'sequence': 1})
            clock[0] = 120
            self.assertEqual(control.handle('_pi_reselect', {}), {'switch': None})
            self.assertEqual(context.request_count, 0)

    def test_managed_pi_driver_wires_and_checkpoints_host_authorized_switch(self):
        context = self.bound('alternate')
        controls = []

        def make_control(value):
            control = Reselection(value, clock=lambda: 10)
            control.checked = 0
            controls.append(control)
            return control

        class FakeBridge:
            current = None

            def __init__(self, handler, *_args, **_kwargs):
                self.handler = handler
                self.config_file = str(context.run_dir / 'bridge.json')

            def __enter__(self):
                FakeBridge.current = self
                return self

            def __exit__(self, *_args):
                FakeBridge.current = None

        class FakeChannel:
            def __init__(self, value, *_args, **_kwargs):
                self.context = value
                model = self.context.config.model('alternate')
                self.events = [
                    {'id': 'state', 'success': True, 'data': {
                        'model': {'provider': model['provider'], 'id': model['model']},
                        'sessionFile': 'sdk-session-file'}},
                    {'id': 'work', 'type': 'response', 'success': True, 'data': {}},
                    {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'toolUse',
                     'provider': model['provider'], 'model': model['model']}},
                    {'type': 'agent_settled'},
                ]

            def send(self, event):
                if event.get('type') == 'prompt':
                    self.context.request_count = 1

            def receive(self, **_kwargs):
                event = self.events.pop(0)
                if event.get('id') == 'state':
                    self.context.hello.set()
                if event.get('type') == 'agent_settled':
                    response = FakeBridge.current.handler('_pi_reselect', {})
                    switch = response['switch']
                    if switch['model'] != self.context.config.model('primary')['model']:
                        raise AssertionError('host must choose the first newly eligible candidate')
                    saved = next((self.context.project.root / 'sessions' / 'worker').rglob('session.json'))
                    if read_json(saved).get('id') != 'sdk-session-file':
                        raise AssertionError('safe-point checkpoint must precede host model application')
                    FakeBridge.current.handler('_pi_reselect_applied', {
                        'provider': switch['provider'], 'model': switch['model'],
                        'authorization': switch['authorization']})
                return event

            def close(self):
                pass

        with (mock.patch('mizu.pi.Reselection', side_effect=make_control),
              mock.patch('mizu.pi.Bridge', FakeBridge),
              mock.patch('mizu.pi.Channel', FakeChannel),
              mock.patch('mizu.pi.record_startup')):
            result = PiDriver(self.config).execute(context, 'complete this unit', profile='alternate')
        self.assertEqual(len(controls), 1)
        self.assertEqual(context.model_evidence['profile'], 'primary')
        self.assertEqual(context.role.profile, 'primary')
        self.assertEqual(result['profile'], 'primary')
        session_files = list((self.project.root / 'sessions' / 'worker').rglob('session.json'))
        self.assertEqual(len(session_files), 1)
        self.assertEqual(read_json(session_files[0])['id'], 'sdk-session-file')
        self.assertEqual(read_json(context.run_dir / 'selection.json')['profile'], 'primary')
        self.assertTrue(read_json(context.run_dir / 'pi-effective.json')['reselection'])

    def test_due_reselection_rejects_live_profile_configuration_change(self):
        context = self.bound()
        clock = [100]
        control = Reselection(context, clock=lambda: clock[0])
        control.checked = 0
        self.config.profiles['primary']['options']['thinkingLevel'] = 'high'
        clock[0] = 10
        with mock.patch('mizu.selection.select', side_effect=AssertionError('changed config must fail before selection')):
            with self.assertRaisesRegex(ProtocolError, 'configuration changed'):
                control.handle('_pi_reselect', {})
        self.assertTrue(context.admission_error)
        self.assertEqual(context.reselection_decision['profile'], 'alternate')

    def test_completed_sdk_boundary_checkpoints_session_before_reselection(self):
        context = self.bound()
        path, _ = self.record(context, 'alternate')
        control = Reselection(context, clock=lambda: 10)
        control.checked = 0
        control.bind_session(path, 'sdk-session-file', {'id': 'old', 'usage': {'tokens': 5}})
        with mock.patch('mizu.pi._cumulative_tokens', return_value=17) as totals:
            with mock.patch('mizu.engine_config.save_session') as save:
                control.handle('_pi_reselect', {})
        totals.assert_called_once_with({'id': 'old', 'usage': {'tokens': 5}}, context)
        save.assert_called_once_with(path, 'sdk-session-file', {'tokens': 17})
        self.assertIsNotNone(control.pending)

    def test_uncertain_or_mismatched_application_poison_admission(self):
        for operation, args in (('_budget', {'sequence': 1}), ('_pi_reselect_applied', {'model': 'wrong'})):
            context = self.bound()
            control = Reselection(context, clock=lambda: 0)
            control.checked = -10
            control.handle('_pi_reselect', {})
            with self.assertRaises(ProtocolError):
                control.handle(operation, args)
            self.assertEqual(context.reselection_decision['profile'], 'alternate')
            with self.assertRaises(ProtocolError):
                control.handle('_budget', {'sequence': 2})
            self.assertTrue(context.admission_error)


    def test_expiry_recovers_without_classifier_or_probe_and_never_downgrades(self):
        self.spec['on_error'] = [{'scope': 'profile', 'seconds': 20, 'when': {}}]
        validate_selectors(self.config.selectors, self.config.profiles, self.file.parent)
        context = self.bound('primary')
        State(self.config).result(context.reselection_decision, run='expiry-test', at=1000,
                                  error=ModelFailure('pi', kind='error', message='fixture failure'))
        with mock.patch('mizu.selection.time.time', return_value=1001):
            context = self.bound('alternate')
        clock = [0]
        control = Reselection(context, clock=lambda: clock[0])
        with mock.patch('mizu.classification.classify', side_effect=AssertionError('extra inference')):
            with mock.patch('mizu.selection.time.time', return_value=1010):
                clock[0] = 10
                self.assertEqual(control.handle('_pi_reselect', {}), {'switch': None})
            with mock.patch('mizu.selection.time.time', return_value=1020):
                clock[0] = 20
                switch = control.handle('_pi_reselect', {})['switch']
                control.handle('_pi_reselect_applied', {**self.config.model('primary'),
                                                       'authorization': switch['authorization']})
            # An ordinary new block now selects fallback, but an active unit never downgrades.
            State(self.config).result(context.reselection_decision, run='second-block', at=1021,
                                      error=ModelFailure('pi', kind='error', message='fixture failure'))
            with mock.patch('mizu.selection.time.time', return_value=1030):
                clock[0] = 30
                self.assertEqual(control.handle('_pi_reselect', {}), {'switch': None})
        self.assertEqual(context.reselection_decision['profile'], 'primary')
        self.assertEqual(context.request_count, 0)

    def test_budget_refusals_preserve_existing_authority_and_exception(self):
        context = self.bound()
        control = Reselection(context)
        refused = LimitExceeded('fixture request limit')
        context.handle = mock.Mock(side_effect=refused)
        with self.assertRaises(LimitExceeded) as error:
            control.handle('_budget', {'sequence': 1})
        self.assertIs(error.exception, refused)
        self.assertFalse(control.failed)
        self.assertIsNone(context.admission_error)

    def test_narrowed_host_continuation_keeps_family_without_restoring_tools(self):
        self.role = dataclasses.replace(self.role, engine_tools=('read',))
        self.config.roles['worker'] = self.role
        context = self.bound()
        original, _ = self.record(context, 'alternate')
        save_session(original, 'completed-effects', {'tokens': 5})
        context.turn_authority_role = context.role
        context.turn_continuation = True
        context.session_identity_capabilities = context.role.capabilities
        context.role = dataclasses.replace(context.role, capabilities=('finish',), engine_tools=())
        resumed, record = self.record(context, 'alternate')
        self.assertEqual(resumed, original)
        self.assertEqual(record['id'], 'completed-effects')
        control = Reselection(context, clock=lambda: 100)
        control.checked = 0
        self.assertEqual(control.handle('_pi_reselect', {}), {'switch': None})
        self.assertEqual(context.role.engine_tools, ())
        self.assertEqual(context.role.capabilities, ('finish',))
