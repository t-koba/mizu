"""Local completion checks; no provider, container or native service claim."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
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
        # The descendant holds the pipe for 30 seconds, the run gives up after
        # 5, and the outer call must return well inside the hold. The margins
        # only have to keep the two bounds apart: a slow runner's launch of the
        # descendant must not decide the outcome. The descendant writes its
        # pid as soon as it is running, so the file is a ready handshake:
        # without it the child never started, and the child's own exit status
        # and output say why.
        code="import subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True); Path(sys.argv[1]).write_text(str(p.pid))"
        outer="from mizu.process import run; import sys; print(run([sys.executable,'-c',sys.argv[1],sys.argv[2]],timeout=5,maximum=1024))"
        try:
            started=time.monotonic()
            result=subprocess.run([sys.executable,'-c',outer,code,str(pid_file)],
                env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')},capture_output=True,text=True,timeout=15)
            # A failure here must say which hypothesis it was: the child
            # never became ready, or the run did not return the timeout.
            self.assertTrue(pid_file.exists(),
                f"descendant never became ready: exit={result.returncode} "
                f"stdout={result.stdout!r} stderr={result.stderr!r}")
            self.assertIn("reason='timeout'",result.stdout,
                f"run did not report the timeout: exit={result.returncode} "
                f"stdout={result.stdout!r} stderr={result.stderr!r}")
            self.assertLess(time.monotonic()-started,12)
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

    def test_input_failure_survives_a_late_io_thread(self):
        from mizu import process
        original=process._input_thread
        def late(stream,data,stop,errors,close=False):
            writer=original(stream,data,stop,errors,close=close)
            def linger():
                writer.join()
                time.sleep(.6)
            thread=threading.Thread(target=linger,daemon=True,name='mizu-input')
            thread.start()
            return thread
        with patch.object(process,'_input_thread',late):
            result=process.run([sys.executable,'-c','import os,time; os.close(0); time.sleep(.1)'],
                               timeout=2,maximum=1024,input_data=b'x'*1048576)
        self.assertEqual(result.reason,'input_error')

    def test_stop_uses_one_ceiling_and_reports_a_survivor(self):
        from mizu.process import _stop_io
        def linger():
            time.sleep(.6)
        thread=threading.Thread(target=linger,daemon=True)
        thread.start()
        self.assertEqual(_stop_io(threading.Event(),(thread,),ceiling=2.0),[])
        self.assertFalse(thread.is_alive())
        # A survivor is named, not raised: the stop reports which thread
        # outlived the bound, so a fault is told from a slow thread.
        thread=threading.Thread(target=linger,daemon=True, name='mizu-input')
        thread.start()
        self.assertEqual(_stop_io(threading.Event(),(thread,),ceiling=.05),['mizu-input'])

    def test_the_stop_waits_once_for_every_thread(self):
        from mizu.process import _stop_io
        def linger():
            time.sleep(30)
        threads=[threading.Thread(target=linger,daemon=True,name=f"mizu-{index}") for index in range(3)]
        for thread in threads:
            thread.start()
        started=time.monotonic()
        survivors=_stop_io(threading.Event(),tuple(threads),ceiling=1.0)
        elapsed=time.monotonic()-started
        # One ceiling for the whole stop: the three threads that outlive it
        # are all named, and the wait is that one ceiling, not one per
        # thread. A per-thread wait would take three times as long.
        self.assertEqual(survivors,['mizu-0','mizu-1','mizu-2'])
        self.assertLess(elapsed,2.0)

    def test_a_stopped_sibling_is_never_named_a_survivor(self):
        from mizu.process import _stop_io
        def linger():
            time.sleep(30)
        def finish():
            return None
        lingerer=threading.Thread(target=linger,daemon=True,name='mizu-linger')
        sibling=threading.Thread(target=finish,daemon=True,name='mizu-sibling')
        lingerer.start()
        sibling.start()
        sibling.join()
        survivors=_stop_io(threading.Event(),(lingerer,sibling),ceiling=.05)
        # Only the thread that outlived the bound is named.
        self.assertEqual(survivors,['mizu-linger'])

    def test_the_timeout_path_keeps_its_outcome_and_names_the_survivor(self):
        from mizu import process
        original=process._input_thread
        def late(stream,data,stop,errors,close=False):
            writer=original(stream,data,stop,errors,close=close)
            def linger():
                writer.join()
                time.sleep(30)
            thread=threading.Thread(target=linger,daemon=True,name='mizu-input')
            thread.start()
            return thread
        started=time.monotonic()
        with patch.object(process,'_input_thread',late), \
             patch.object(process,'STOP_IO_TIMEOUT_SECONDS',.2):
            result=process.run([sys.executable,'-c','import time; time.sleep(30)'],
                               timeout=.05,maximum=1024,input_data=b'x'*1048576)
        # The deadline expired, so the run decided "timeout"; the host
        # fault that followed is recorded with it, never raised in its
        # place, and the stop waited its own bound rather than the
        # exhausted deadline.
        self.assertEqual(result.reason,'timeout')
        self.assertEqual(result.survivors,('mizu-input',))
        self.assertLess(time.monotonic()-started,3)

    def test_a_clean_exit_reports_no_survivors(self):
        from mizu import process
        result=run([sys.executable,'-c','print("ok")'],timeout=5,maximum=1024)
        # "exited" is reported only after every pipe thread stopped, so the
        # output is complete and there is no thread left to name. The
        # decision is the run's own, not a reason invented after the stop.
        self.assertEqual(result.reason,'exited')
        self.assertEqual(result.survivors,())
        self.assertEqual(result.stdout,'ok\n')
        # The reason that used to stand for this state is gone: the
        # outcome and the survivors are the evidence of a stopped run.
        self.assertFalse(hasattr(process,'STOP_SURVIVOR_REASON'))

    def test_a_decided_failure_keeps_its_reason_with_the_survivors(self):
        from mizu import process
        with patch.object(process,'_stop_io',return_value=['mizu-input']):
            result=process.run([sys.executable,'-c','import os,time; os.close(0); time.sleep(.1)'],
                               timeout=2,maximum=1024,input_data=b'x'*1048576)
        # The reason the run had already decided is kept; the survivors
        # are recorded beside it.
        self.assertEqual(result.reason,'input_error')
        self.assertEqual(result.survivors,('mizu-input',))

    def test_the_stop_fault_does_not_replace_the_exception_in_flight(self):
        from mizu import process
        from mizu.errors import LimitExceeded
        original=process._input_thread
        def late(stream,data,stop,errors,close=False):
            writer=original(stream,data,stop,errors,close=close)
            def linger():
                writer.join()
                time.sleep(30)
            thread=threading.Thread(target=linger,daemon=True,name='mizu-input')
            thread.start()
            return thread
        child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],
                               stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            with patch.object(process,'_input_thread',late), \
                 patch.object(process,'STOP_IO_TIMEOUT_SECONDS',.2):
                with self.assertRaises(LimitExceeded) as caught:
                    process.send_bounded(child,b'x'*4096,deadline=time.monotonic()+.05,
                                         cancel=lambda: False)
            # The exception that was already propagating survives; the stop
            # fault is attached to it instead of replacing it.
            notes=getattr(caught.exception,'__notes__',[])
            self.assertTrue(any('Pipe operation did not stop' in note for note in notes),notes)
        finally:
            child.kill()
            child.wait(timeout=10)
            with contextlib.suppress(OSError, ValueError):
                child.stdin.close()

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
        required = {'write', 'fsync', 'windows-replace' if os.name == 'nt' else 'replace',
                    'rename' if os.name == 'nt' else 'exclusive-rename'}
        self.assertTrue(required <= {e[0] for e in events})
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
        from mizu.errors import Cancelled
        result=None
        engine=Engine(self.config,driver=ScriptDriver(work),stop=stop)
        def caller(ctx,*_):
            nonlocal result
            result=engine.consult(ctx,{'question':'review'})
        with self.assertRaises(Cancelled):
            Engine(self.config,driver=ScriptDriver(caller),stop=stop).run(self.project,'reporter')
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
        def work(ctx,*_):
            entered.set()
            # Hold the first writer until the competing admission is checked.
            # The deadline bounds a broken test; it is not a scheduling assertion.
            if not release.wait(30):
                raise AssertionError("competing admission did not release the writer")
        driver=ScriptDriver(work)
        def first():
            try:Engine(self.config,driver=driver).run(self.project,'worker')
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=first);thread.start()
        try:
            self.assertTrue(entered.wait(30), "writer did not reach its held section")
            with self.assertRaises(Busy):Engine(self.config,driver=driver).run(self.project,'worker')
        finally:release.set();thread.join(30)
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
        def work(ctx,*_):ctx.handle('finish',{'outcome':'wait','summary':'\x01'*12000})
        names=tuple(f'test-{n}' for n in range(8))
        base=self.config.roles['consult']
        roles=dict(self.config.roles)
        for n in names:
            roles[n]=dataclasses.replace(base,name=n)
        config=dataclasses.replace(self.config,roles=roles)
        result=self.admitted_call(Engine(config,driver=ScriptDriver(work)), 'consult',{'question':'\x01'*8000,'roles':list(names)})
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

    def test_feed_search_truncation_flag_at_per_feed_bound(self):
        import tempfile
        from mizu.web import SEARCH_FEED_ITEM_LIMIT, Web
        def feed_xml(count, trailing=""):
            items = "".join("<item><title>hi test</title><link>https://example.org/x%d</link>"
                            "<description>d</description></item>" % n for n in range(count))
            return "<rss><channel>%s%s</channel></rss>" % (items, trailing)
        def make_web(xml):
            tmp = Path(tempfile.mkdtemp())
            self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
            feed = "https://example.org/feed"
            web = Web({**self.config.web, "hosts": ["example.org"], "feeds": [feed],
                       "cache_seconds": 0, "timeout_seconds": 1, "max_bytes": 1048576,
                       "search_command": [], "intranet": False}, tmp / "c", tmp / "r")
            web.fetch = lambda url: {"id": "r", "text": xml}
            return web
        # Exactly at the bound: no overflow, so not truncated.
        web = make_web(feed_xml(SEARCH_FEED_ITEM_LIMIT))
        out = web.search("hi")
        self.assertEqual(len(out["results"]), SEARCH_FEED_ITEM_LIMIT)
        self.assertFalse(out["truncated"])
        # Trailing non-eligible rows (wrong tag, non-HTTPS) never set overflow.
        trailing = ("<notanitem><title>hi</title><link>https://example.org/no</link></notanitem>"
                    "<item><title>hi</title><link>http://example.org/plain</link>"
                    "<description>d</description></item>")
        web = make_web(feed_xml(SEARCH_FEED_ITEM_LIMIT, trailing))
        out = web.search("hi")
        self.assertEqual(len(out["results"]), SEARCH_FEED_ITEM_LIMIT)
        self.assertFalse(out["truncated"])
        # One more eligible entry overflows the per-feed bound and flags truncation.
        web = make_web(feed_xml(SEARCH_FEED_ITEM_LIMIT + 1))
        out = web.search("hi")
        self.assertEqual(len(out["results"]), SEARCH_FEED_ITEM_LIMIT)
        self.assertTrue(out["truncated"])
        ctx = self.context("searcher")
        with patch("mizu.runtime.bounded", return_value=out):
            response = ctx._op_search({"query": "hi", "limit": 1000})
        self.assertTrue(response["source_truncated"])

    def test_feed_overflow_forces_truncated_with_room_remaining(self):
        import json
        import tempfile
        from mizu.process import Result
        from mizu.web import SEARCH_FEED_ITEM_LIMIT, Web
        import mizu.web as webmod
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        feed = "https://example.org/feed"
        web = Web({**self.config.web, "hosts": ["example.org"], "feeds": [feed],
                   "cache_seconds": 0, "timeout_seconds": 5, "max_bytes": 1048576,
                   "search_command": ["python3", "adapter"], "intranet": False},
                  tmp / "c", tmp / "r")
        items = "".join("<item><title>hi</title><link>https://example.org/x%d</link>"
                        "<description>d</description></item>" % n
                        for n in range(SEARCH_FEED_ITEM_LIMIT + 1))
        web.fetch = lambda url: {"id": "r", "text": "<rss><channel>%s</channel></rss>" % items}
        adapted = [{"title": "t", "url": "https://x/", "summary": "s"}]
        with patch.object(webmod, "run",
                          return_value=Result(0, json.dumps({"results": adapted}), "", "exited", 0.1)):
            out = web.search("hi")
        # Only one adapter result, so room remains, yet the feed overflow still flags truncation.
        self.assertEqual(len(out["results"]), 1 + SEARCH_FEED_ITEM_LIMIT)
        self.assertTrue(out["truncated"])

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
