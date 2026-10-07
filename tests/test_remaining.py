"""Local completion checks; no provider, container or native service claim."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import patch
from types import SimpleNamespace

from support import Fixture
from mizu.fs import write_json
from mizu.process import run, Result
from mizu.usage import summarize


class RemainingTests(Fixture):
    def test_detached_descendant_cannot_hold_return_or_reader(self):
        if os.name != 'posix':
            self.skipTest('POSIX detached process group; Windows uses a Job')
        pid_file=self.root/'descendant.pid'
        code="import subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'],start_new_session=True); Path(sys.argv[1]).write_text(str(p.pid))"
        outer="from mizu.process import run; import sys; print(run([sys.executable,'-c',sys.argv[1],sys.argv[2]],timeout=.5,maximum=1024))"
        try:
            started=time.monotonic()
            result=subprocess.run([sys.executable,'-c',outer,code,str(pid_file)],
                env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')},capture_output=True,text=True,timeout=2)
            self.assertTrue(pid_file.exists())
            self.assertIn("reason='timeout'",result.stdout)
            self.assertLess(time.monotonic()-started,1.5)
        finally:
            if pid_file.exists():
                try:os.kill(int(pid_file.read_text()),signal.SIGKILL)
                except ProcessLookupError:pass

    def test_unrecognized_usage_is_unknown_not_zero_known(self):
        path=self.project.root/'runs'/'unknown';path.mkdir(parents=True)
        write_json(path/'result.json',{'finished_at':'2026-10-03T00:00:00+00:00','model':{'engine':'codex','provider':'test','model':'test','usage_known':True,'usage':[{'unrecognized_tokens':123}]}})
        facts=summarize(self.project)
        self.assertTrue(facts['recent_entries'])
        self.assertIn('usage_known',facts['recent_entries'][0])
        self.assertFalse(facts['recent_entries'][0]['usage_known'])
        self.assertEqual(facts['totals']['unknown_usage_runs'],1)

    def test_input_failure_cannot_return_success(self):
        result=run([sys.executable,'-c','import os,time; os.close(0); time.sleep(.1)'],timeout=2,maximum=1024,input_data=b'x'*1048576)
        self.assertEqual(result.reason,'input_error')

    def test_received_usage_survives_event_log_failure(self):
        from test_drivers import SyntheticEngineTests
        from mizu import engine_channel
        original=engine_channel.atomic_write
        contexts=[]
        from mizu.codex import CodexDriver
        execute=CodexDriver.execute
        def capture(driver,ctx,*args,**kwargs):
            contexts.append(ctx)
            return execute(driver,ctx,*args,**kwargs)
        def writing(path,*args,**kwargs):
            if path.name.endswith('-events.jsonl'):raise OSError('injected evidence failure')
            return original(path,*args,**kwargs)
        helper=SyntheticEngineTests();helper.setUp()
        try:
            with patch.object(engine_channel,'atomic_write',side_effect=writing), patch.object(CodexDriver,'execute',capture):
                with self.assertRaises(OSError): helper.execute('codex')
            self.assertEqual(contexts[0].model_evidence['usage'][0]['input_tokens'],10)
            self.assertTrue(contexts[0].model_evidence['usage_known'])
        finally: helper.doCleanups()

    def test_rpc_send_obeys_deadline_and_precancel(self):
        from mizu.process import send_bounded
        from mizu import platform
        from mizu.errors import Cancelled, LimitExceeded
        child=platform.spawn([sys.executable,'-c','import time; time.sleep(5)'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,bufsize=0,**platform.popen_kwargs())
        try:
            with self.assertRaises(Cancelled):send_bounded(child,b'x',deadline=time.monotonic()+1,cancel=lambda:True)
            started=time.monotonic()
            with self.assertRaises(LimitExceeded):send_bounded(child,b'x'*1048576,deadline=started+.1,cancel=lambda:False)
            self.assertLess(time.monotonic()-started,.75)
        finally:
            platform.terminate_process(child,grace=0)
            child.stdin.close()

    def test_write_crash_and_capacity_matrix_recovers_published_state(self):
        import shutil
        from mizu.snapshot import Snapshots
        base=self.root/'baseline';workspace=base/'workspace';workspace.mkdir(parents=True)
        (workspace/'a').write_bytes(b'old')
        store=Snapshots(base/'store',excludes=(),max_file=1024,max_bytes=4096,max_files=4)
        old=store.create(store.capture_files(workspace),goal='g',state='s',run=None,outcome='wait',summary='old')
        store.publish(old)
        (workspace/'a').write_bytes(b'new')
        script=Path(__file__).with_name('fault_publication.py')
        env={**os.environ,'PYTHONPATH':str(script.parents[1]/'src')}
        trace=self.root/'trace';shutil.copytree(base,trace)
        result=subprocess.run([sys.executable,str(script),str(trace),'trace','0'],env=env,capture_output=True,text=True,check=True,timeout=5)
        events=json.loads(result.stdout)
        self.assertTrue({'write','fsync','replace','link'} <= {e[0] for e in events})
        def check_recovered(root):
            restarted=Snapshots(root/'store',excludes=(),max_file=1024,max_bytes=4096,max_files=4)
            current=restarted.get()
            self.assertIn(current['summary'],('old','new'))
            history=restarted.history(current['id'])
            self.assertEqual(history[-1]['id'],current['id'])
            for snapshot in history:
                for name in snapshot['files']:restarted.read(snapshot,name)
            if current['summary']=='old':self.assertEqual(len(history),1)
        for mode in ('eio','enospc'):
            # One spawn per recoverable mode over pristine per-point trees
            # instead of one spawn per point; every boundary is still
            # injected and every recovery still asserted.
            batch=self.root/mode;batch.mkdir()
            for point in range(1,len(events)+1):shutil.copytree(base,batch/str(point))
            result=subprocess.run([sys.executable,str(script),str(batch),mode,'batch'],env=env,capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stderr)
            batches=json.loads(result.stdout)
            self.assertEqual(len(batches),len(events))
            for point,case in enumerate(batches,start=1):
                with self.subTest(mode=mode,boundary=events[point-1],point=point):
                    self.assertEqual(len(case),point)
                    check_recovered(batch/str(point))
        for point in range(1,len(events)+1):
            # Crash points keep one process death per point.
            with self.subTest(mode='crash',boundary=events[point-1],point=point):
                root=self.root/f'crash-{point}';shutil.copytree(base,root)
                result=subprocess.run([sys.executable,str(script),str(root),'crash',str(point)],env=env,capture_output=True,text=True,timeout=5)
                self.assertIn(result.returncode,(0,86))
                check_recovered(root)

    def test_special_file_after_finish_refuses_done(self):
        if not hasattr(os,'mkfifo'):self.skipTest('POSIX FIFO')
        from support import ScriptDriver
        from mizu.runtime import Engine
        from mizu.errors import Denied
        old=self.project.snapshots.get()['id']
        def work(ctx,*_):
            ctx.verification={'passed':True,'code_digest':ctx.project.snapshots.capture_files(ctx.workspace)['code_digest']}
            ctx.handle('finish',{'outcome':'done','summary':'verified','state':'done'})
            os.mkfifo(ctx.workspace/'special')
        with self.assertRaises(Denied):Engine(self.config,driver=ScriptDriver(work)).run(self.project,'worker')
        self.assertEqual(self.project.snapshots.get()['id'],old)

    def test_comparison_fixture_detects_bug_and_accepts_contract_fix(self):
        import importlib.util
        script=Path(__file__).resolve().parents[1]/'scripts/comparison.py'
        spec=importlib.util.spec_from_file_location('comparison',script)
        comparison=importlib.util.module_from_spec(spec);spec.loader.exec_module(comparison)
        destination=self.root/'task'
        comparison.prepare(destination)
        failed=subprocess.run([sys.executable,'-m','unittest','-q'],cwd=destination,capture_output=True)
        self.assertNotEqual(failed.returncode,0)
        (destination/'calculator.py').write_text('def mean(values):\n    if not values:raise ValueError("empty")\n    return sum(values)/len(values)\n')
        passed=subprocess.run([sys.executable,'-m','unittest','-q'],cwd=destination,capture_output=True)
        self.assertEqual(passed.returncode,0)
        record={'product':'mizu','version':'test','scenario':'normal','trial':1,
                'fixture_sha256':comparison.fixture_manifest()['sha256'],
                'condition':{'provider':'synthetic','model':'synthetic','request_limit':0,'permissions':'task only'},
                'status':'not_run','started_at':'','finished_at':'','evidence':[],
                'metrics':dict.fromkeys(comparison.METRICS),'limitations':['fixture validation only']}
        self.assertEqual(comparison.validate(record)['status'],'not_run')
        record['metrics']['cost']=float('nan')
        with self.assertRaises(ValueError):comparison.validate(record)

    def test_pipe_failure_refuses_model_success(self):
        import queue
        from mizu.engine_channel import Channel
        from mizu.errors import ProtocolError
        ctx=self.context();ctx.finished={'outcome':'wait'}
        channel=Channel.__new__(Channel);channel.context=ctx;channel.records=queue.Queue()
        channel.records.put(ProtocolError('Partial input delivery'))
        with self.assertRaises(ProtocolError):channel.receive()

    def test_installer_recovers_interrupted_link_before_retry(self):
        import importlib.util
        script=Path(__file__).resolve().parents[1]/'scripts/install.py'
        spec=importlib.util.spec_from_file_location('install_recovery',script)
        install=importlib.util.module_from_spec(spec);spec.loader.exec_module(install)
        old=self.root/'old';old.mkdir()
        link=self.root/'current';recovery=self.root/'.current.recovery'
        recovery.symlink_to(old,target_is_directory=True)
        install.recover_link(link)
        self.assertEqual(link.resolve(), old.resolve())
        self.assertFalse(recovery.is_symlink())
        link.unlink();link.write_text('unrelated')
        recovery.symlink_to(old,target_is_directory=True)
        from mizu.errors import Denied
        with self.assertRaises(Denied):install.recover_link(link)
        self.assertEqual(link.read_text(),'unrelated')

    def test_consultation_cancel_preserves_usage_and_cleans_input(self):
        import threading
        from support import ScriptDriver
        from mizu.runtime import Engine
        stop=threading.Event()
        def work(ctx,*_):
            ctx.model_evidence={'engine':'synthetic','requests':ctx.request_count,'usage_known':True,'usage':[{'input_tokens':9}]}
            stop.set()
        parent=self.context();parent.stop=stop
        result=Engine(self.config,driver=ScriptDriver(work),stop=stop).consult(parent,{'question':'review','profiles':[self.config.consult_profiles[0]]})
        self.assertIn('error',result['answers'][0])
        run_dir=self.project.root/'runs'/result['answers'][0]['run']
        from mizu.fs import read_json
        self.assertEqual(read_json(run_dir/'error.json')['model']['usage'][0]['input_tokens'],9)
        self.assertFalse((run_dir/'input').exists())

    def test_parallel_writers_have_one_publication(self):
        import threading
        from support import ScriptDriver
        from mizu.runtime import Engine
        from mizu.errors import Busy
        entered=threading.Event();release=threading.Event();errors=[]
        def work(ctx,*_):entered.set();release.wait(2)
        driver=ScriptDriver(work)
        def first():
            try:Engine(self.config,driver=driver).run(self.project,'worker')
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=first);thread.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(Busy):Engine(self.config,driver=driver).run(self.project,'worker')
        finally:release.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertFalse(errors)
        self.assertEqual(len(driver.calls),1)
        self.assertEqual(self.project.snapshots.get()['run'],driver.calls[0][0].run_dir.name)

    def test_engine_admission_crosses_utc_day_without_resetting_old_evidence(self):
        import dataclasses
        import datetime as dt
        from mizu import budget
        from mizu.runtime import Engine
        from support import ScriptDriver
        from mizu.errors import LimitExceeded
        config=dataclasses.replace(self.config,limits=dataclasses.replace(self.config.limits,daily_requests=1))
        class Clock:
            current=dt.datetime(2026,10,3,23,59,59,tzinfo=dt.timezone.utc)
            @classmethod
            def now(cls,*args):return cls.current
        fake=SimpleNamespace(datetime=Clock,timezone=dt.timezone,date=dt.date)
        with patch.object(budget,'dt',fake):
            Engine(config,driver=ScriptDriver()).run(self.project,'worker')
            with self.assertRaises(LimitExceeded):Engine(config,driver=ScriptDriver()).run(self.project,'worker')
            Clock.current=dt.datetime(2026,10,4,0,0,1,tzinfo=dt.timezone.utc)
            Engine(config,driver=ScriptDriver()).run(self.project,'worker')
        from mizu.fs import read_json
        self.assertEqual(len(read_json(config.data/'budget/2026-10-03.json')['requests']),1)
        self.assertEqual(len(read_json(config.data/'budget/2026-10-04.json')['requests']),1)

    def test_bridge_close_stops_and_collects_active_operation(self):
        import threading
        from mizu.bridge import Bridge,connect
        from mizu.fs import read_json,canonical
        entered=threading.Event();stopped=threading.Event();finished=threading.Event()
        def handle(*_):
            entered.set();stopped.wait(2);finished.set();return {'done':True}
        bridge=Bridge(handle,[],timeout=2,on_close=stopped.set)
        with bridge:
            client=connect(read_json(bridge.config_file))
            client.sendall(canonical({'token':bridge.token,'operation':'read','arguments':{}}))
            self.assertTrue(entered.wait(1))
        client.close()
        self.assertTrue(finished.is_set())
        self.assertFalse(bridge.thread.is_alive())

    def test_custom_provider_is_preserved_without_fixed_allowlist(self):
        from mizu.engine_config import effective
        ctx=self.context()
        settings=effective(self.config,ctx.role,ctx.role.profile)
        self.assertEqual(settings['provider'],'test-provider')
        self.assertEqual(ctx.request_count,0)

    def test_portable_read_is_bounded_and_detects_identity_changes(self):
        from mizu import fs
        from mizu.errors import Denied
        target=self.root/'portable';target.write_bytes(b'old')
        original=Path.open;calls=[]
        changed=False
        class Stream:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):self.stream.__enter__();return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def __getattr__(self,name):return getattr(self.stream,name)
            def read(self,maximum=-1):
                calls.append(maximum)
                data=self.stream.read(maximum)
                if changed:
                    target.write_bytes(b'new')
                    os.utime(target,(time.time()+10,time.time()+10))
                return data
        def opening(path,*args,**kwargs):
            stream=original(path,*args,**kwargs)
            return Stream(stream) if path==target and args and args[0]=='rb' else stream
        with patch.object(fs._platform,'HAS_OPENAT',False),patch.object(Path,'open',opening):
            self.assertEqual(fs.safe_read(self.root,'portable',32),b'old')
            self.assertEqual(calls,[33])
            changed=True
            with self.assertRaises(Denied):fs.safe_read(self.root,'portable',32)

    def test_policy_read_and_driver_start_failures_leave_terminal_evidence(self):
        import dataclasses
        from mizu.runtime import Engine
        from mizu.fs import read_json
        from support import ScriptDriver
        missing=dataclasses.replace(self.config.roles['reviewer'],policy=(self.root/'absent-policy.md',))
        config=dataclasses.replace(self.config,roles={**self.config.roles,'reviewer':missing})
        from mizu.errors import ConfigError as _ConfigError
        with self.assertRaises(_ConfigError):Engine(config,driver=ScriptDriver()).run(self.project,'reviewer')
        def work(*_):raise OSError('driver start failed')
        with self.assertRaises(OSError):Engine(self.config,driver=ScriptDriver(work)).run(self.project,'reviewer')
        records=list((self.project.root/'runs').glob('*/error.json'))
        self.assertEqual(len(records),2)
        self.assertTrue(all(read_json(p)['status']=='interrupted' for p in records))
        self.assertFalse(list((self.project.root/'active').glob('*.json')))
        self.assertFalse(list((self.project.root/'runs').glob('*/input')))

    def test_mutating_tool_responses_fit_escaped_mcp_frame(self):
        from mizu.fs import canonical,MAX_FRAME
        from mizu.protocol import validate,DEFINITIONS
        ctx=self.context()
        record={'id':'a'*32,'kind':'command','exit_code':0,'reason':'exited','seconds':1,
                'writable':True,'script':'\x01'*65536,'stdout':'\x01'*1048576,'stderr':'\x01'*1048576}
        with patch.object(ctx.sandbox,'execute',return_value=record):
            response=ctx._op_exec({'script':'test'})
        wire=canonical({'jsonrpc':'2.0','id':1,'result':{'content':[{'type':'text','text':canonical(response).decode()}]}})
        self.assertLess(len(wire),MAX_FRAME)
        self.assertEqual(response['exit_code'],0)
        self.assertTrue(response['stdout_truncated'])
        self.project.verify=tuple('x'*65536 for _ in range(32))
        with patch.object(ctx.sandbox,'execute',return_value=record):
            verification=ctx._op_verify({})
        self.assertTrue(verification['passed'])
        self.assertTrue(verification['commands'][0]['script_truncated'])
        self.assertLess(len(canonical({'content':[{'text':canonical(verification).decode()}]})),MAX_FRAME)
        validate({'offset':0,'limit':1},DEFINITIONS['insights'][1])

    def test_large_consultation_answers_have_bounded_reply_and_full_evidence(self):
        import dataclasses
        from support import ScriptDriver
        from mizu.runtime import Engine
        from mizu.fs import canonical,MAX_FRAME,read_json
        profile=self.config.consult_profiles[0]
        def work(ctx,*_):ctx.handle('finish',{'outcome':'wait','summary':'\x01'*12000})
        parent=self.context()
        names=tuple(f'test-{n}' for n in range(8))
        config=dataclasses.replace(self.config,consult_profiles=names,profiles={**self.config.profiles,**{name:self.config.profiles[profile] for name in names}})
        result=Engine(config,driver=ScriptDriver(work)).consult(parent,{'question':'\x01'*8000,'profiles':list(names)})
        self.assertTrue(all(len(a['answer'])==12000 for a in result['answers']))
        self.assertLess(len(canonical({'content':[{'text':canonical(result).decode()}]})),MAX_FRAME)
        child=self.project.root/'runs'/result['answers'][0]['run']/'consultation.json'
        self.assertEqual(len(read_json(child)['answer']),12000)

    def test_large_read_results_have_progressing_pages(self):
        from mizu.fs import canonical,MAX_FRAME
        from mizu.protocol import validate,DEFINITIONS
        ctx=self.context()
        with patch('mizu.runtime.bounded',return_value={'text':'\x01'*65536,'id':'x'}):
            result=ctx._op_fetch({'url':'https://example.invalid','offset':8192,'limit':8192})
        self.assertEqual(result['next_offset'],16384)
        self.assertTrue(result['truncated'])
        self.assertLess(len(canonical({'content':[{'text':canonical(result).decode()}]})),MAX_FRAME)
        records={'results':[{'title':'\x01'*10000,'url':'https://example.invalid','summary':'\x01'*10000} for _ in range(30)],'scope':'synthetic','trust':'external-untrusted'}
        with patch('mizu.runtime.bounded',return_value=records):
            result=ctx._op_search({'query':'test','limit':2})
        self.assertEqual(len(result['results']),2)
        self.assertEqual(result['next_offset'],2)
        self.assertTrue(result['results'][0]['summary_truncated'])
        self.assertLess(len(canonical({'content':[{'text':canonical(result).decode()}]})),MAX_FRAME)
        validate({'query':'test','offset':2,'limit':2},DEFINITIONS['search'][1])

    def test_search_pages_adapter_results_beyond_old_barrier(self):
        import json
        import tempfile
        from mizu.process import Result
        from mizu.web import Web
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        web = Web({**self.config.web, "search_command": ["python3", "adapter"]},
                  tmp / "c", tmp / "r")
        import mizu.web as webmod
        adapted = [{"title": f"t{n}", "url": "https://x/", "summary": "s"} for n in range(35)]
        with patch.object(webmod, "run", return_value=Result(0, json.dumps({"results": adapted}), "", "exited", 0.1)):
            records = web.search("test")
        self.assertEqual(len(records["results"]), 35)
        ctx = self.context("searcher")
        with patch("mizu.runtime.bounded", return_value=records):
            first = ctx._op_search({"query": "test", "offset": 0, "limit": 30})
            overflow = ctx._op_search({"query": "test", "offset": 30, "limit": 1000})
        self.assertEqual(len(first["results"]), 30)
        self.assertEqual(first["next_offset"], 30)
        self.assertTrue(first["truncated"])
        self.assertEqual(len(overflow["results"]), 5)
        self.assertEqual(overflow["results"][0]["title"], "t30")
        self.assertIsNone(overflow["next_offset"])
        self.assertFalse(overflow["truncated"])

    def test_invalid_engine_json_cannot_poison_saved_usage(self):
        from mizu.drivers import parse_event
        with self.assertRaises(ValueError): parse_event('{"usage":{"input_tokens":NaN}}')
        with self.assertRaises(UnicodeEncodeError):parse_event('{"usage":{"bad":"\\ud800"}}')

    def test_admission_receipt_failure_keeps_accepted_count(self):
        from mizu import drivers
        ctx=self.context();ctx.model_evidence={}
        original=drivers.write_json
        def writing(path,*args,**kwargs):
            if path.name=='admission.json':raise OSError('receipt failure')
            return original(path,*args,**kwargs)
        with patch.object(drivers,'write_json',side_effect=writing):
            with self.assertRaises(OSError):drivers.admit_invocation(ctx,unit='turn')
        self.assertEqual(ctx.model_evidence['requests'],1)
        self.assertEqual(ctx.request_count,1)

    def test_uncertain_budget_write_is_not_reported_as_known_zero(self):
        from mizu.drivers import admit_invocation
        from mizu.usage import summarize
        ctx=self.context();ctx.model_evidence={'engine':'codex','requests':0,'usage':[],'usage_known':False}
        with patch('mizu.drivers.Budget.take',side_effect=OSError('sync failed after replace')):
            with self.assertRaises(OSError):admit_invocation(ctx,unit="turn")
        write_json(ctx.run_dir/'error.json',{'finished_at':'2026-10-03T00:00:00+00:00','model':ctx.model_evidence})
        facts=summarize(self.project)
        self.assertFalse(facts['recent_entries'][0]['requests_known'])
        self.assertEqual(facts['totals']['unknown_request_runs'],1)

    def test_corrupt_session_never_silently_starts_fresh(self):
        from mizu.engine_config import effective,session_record
        from mizu.errors import ConfigError
        ctx=self.context()
        settings=effective(self.config,ctx.role,ctx.role.profile)
        path,_=session_record(ctx,ctx.role.profile,settings)
        write_json(path,{'id':None})
        with self.assertRaises(ConfigError): session_record(ctx,ctx.role.profile,settings)

    def test_publication_pointer_requires_current_history_contract(self):
        from mizu.errors import Denied
        snapshot = self.project.snapshots.get()
        write_json(self.project.root / 'current.json', {'snapshot': snapshot['id']})
        with self.assertRaises(Denied):
            self.project.snapshots.get()
        with self.assertRaises(Denied):
            self.project.snapshots.history(snapshot['id'])

    def test_activation_removes_replaced_source_only_after_switch(self):
        import contextlib
        import importlib.util
        spec = importlib.util.spec_from_file_location('current_installer', Path(__file__).resolve().parents[1] / 'scripts/install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        prefix = self.root / 'installed'
        old, new = prefix / 'releases/old', prefix / 'releases/new'
        for release in (old, new):
            (release / 'bin').mkdir(parents=True)
            (release / 'bin/mizu').write_text('source')
            write_json(release / 'installation.json', {'validation': 'local-install-checks-passed', 'mode': 'core-only'})
        (prefix / 'current').symlink_to(old, target_is_directory=True)
        args = SimpleNamespace(prefix=prefix, bin_dir=self.root / 'bin', config=self.file)
        with patch.object(installer, 'verify_release'), patch.object(installer, 'command'), patch.object(installer, 'stopped', return_value=contextlib.nullcontext()):
            installer.promote(args, new)
        self.assertEqual((prefix / 'current').resolve(), new.resolve())
        self.assertEqual((args.bin_dir / 'mizu').resolve(), (new / 'bin/mizu').resolve())
        self.assertFalse(old.exists())
        self.assertFalse((prefix / 'previous').exists())
        self.assertTrue(new.exists())
