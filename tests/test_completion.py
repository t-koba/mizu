"""Behavioral regressions for publication, cancellation and retained evidence.

Fake drivers are explicitly used; these tests make no live inference or OCI
isolation claim. Platform tests exercise the actual local process mechanism.
"""
import dataclasses
import datetime as dt
import errno
import io
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from support import Fixture, ROOT, ScriptDriver
from mizu.errors import Denied, ConfigError, Busy
from mizu.fs import atomic_write, read_json, write_json, page, MAX_FRAME, relative_parts
from mizu.runtime import Engine
from mizu.process import run


class CompletionTests(Fixture):
    def test_final_unrepresentable_snapshot_refuses_done(self):
        before = self.project.snapshots.get()["id"]
        def work(ctx, *_):
            captured = ctx.project.snapshots.capture_files(ctx.workspace)
            ctx.verification = {"passed": True, "code_digest": captured["code_digest"]}
            ctx.handle("finish", {"outcome": "done", "summary": "verified", "state": "done"})
            (ctx.workspace / "alias").symlink_to("app.py")
        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver(work)).run(self.project, "worker")
        self.assertEqual(self.project.snapshots.get()["id"], before)
        self.assertFalse((self.project.root / "active/worker.json").exists())

    def test_final_mode_change_refuses_done(self):
        if os.name == "nt":
            self.skipTest("POSIX execution bits")
        def work(ctx, *_):
            ctx.verification = {"passed": True, "code_digest": ctx.project.snapshots.capture_files(ctx.workspace)["code_digest"]}
            ctx.handle("finish", {"outcome": "done", "summary": "verified", "state": "done"})
            (ctx.workspace / "app.py").chmod(0o755)
        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver(work)).run(self.project, "worker")

    def test_final_hardlink_refuses_done(self):
        def work(ctx, *_):
            ctx.verification = {"passed": True, "code_digest": ctx.project.snapshots.capture_files(ctx.workspace)["code_digest"]}
            ctx.handle("finish", {"outcome": "done", "summary": "verified", "state": "done"})
            os.link(ctx.workspace / "app.py", ctx.workspace / "alias")
        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver(work)).run(self.project, "worker")

    def test_file_fsync_failure_propagates(self):
        target = self.root / "atomic"
        with patch("mizu.fs.os.fsync", side_effect=OSError(errno.EIO, "injected")):
            with self.assertRaises(OSError):
                atomic_write(target, b"data")
        self.assertFalse(target.exists())

    def test_directory_sync_distinguishes_unsupported_and_io(self):
        from mizu.fs import sync_dir
        if os.name != "posix":
            self.skipTest("directory fsync is POSIX-specific")
        with patch("mizu.fs.os.fsync", side_effect=OSError(errno.EINVAL, "unsupported")):
            sync_dir(self.root)
        with patch("mizu.fs.os.fsync", side_effect=OSError(errno.EIO, "injected")):
            with self.assertRaises(OSError):
                sync_dir(self.root)

    def next_snapshot(self):
        (self.project.workspace / "app.py").write_bytes(b"VALUE = 3\n")
        return self.project.snapshots.create(self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state="next", run=None, outcome="wait", summary="next")

    def test_history_failure_before_pointer_does_not_publish(self):
        from mizu import snapshot as module
        old = self.project.snapshots.get()["id"]
        new = self.next_snapshot()
        with patch.object(module, "publish_pointer", side_effect=OSError(errno.ENOSPC, "injected")):
            with self.assertRaises(OSError):
                self.project.snapshots.publish(new)
        self.assertEqual(self.project.snapshots.get()["id"], old)
        self.assertNotIn(new["id"], [s["id"] for s in self.project.snapshots.history(old)])

    def test_sync_failure_after_pointer_preserves_published_history(self):
        from mizu import snapshot as module
        new = self.next_snapshot()
        real = module.publish_pointer
        def publishing(*args, **kwargs):
            real(*args, **kwargs)
            raise OSError(errno.EIO, "injected after pointer replacement")
        with patch.object(module, "publish_pointer", side_effect=publishing):
            with self.assertRaises(OSError):
                self.project.snapshots.publish(new)
        self.assertEqual(self.project.snapshots.get()["id"], new["id"])
        self.assertEqual(self.project.snapshots.history(new["id"])[-1]["id"], new["id"])
        self.assertFalse((self.project.root / "history.json").exists())

    def test_report_failure_keeps_publication_fact(self):
        def work(ctx, *_):
            (ctx.workspace / "app.py").write_bytes(b"VALUE = 9\n")
            ctx.commentary = {"summary": "test"}
        with patch("mizu.runtime.publish", side_effect=OSError(errno.EIO, "injected")):
            with self.assertRaises(OSError):
                Engine(self.config, driver=ScriptDriver(work)).run(self.project, "worker")
        errors = list((self.project.root / "runs").glob("*/error.json"))
        error = read_json(errors[0])
        self.assertIn("publication", error)
        self.assertEqual(error["publication"], "published")
        self.assertEqual(error["durability"], "unconfirmed")
        self.assertEqual(self.project.snapshots.get()["run"], error["run"])

    def test_preparation_failure_cleans_active_and_input(self):
        with patch.object(type(self.project.snapshots), "materialize", side_effect=Denied("injected")):
            with self.assertRaises(Denied):
                Engine(self.config, driver=ScriptDriver()).run(self.project, "reviewer")
        self.assertFalse(list((self.project.root / "active").glob("*.json")))
        self.assertTrue(list((self.project.root / "runs").glob("*/error.json")))
        self.assertFalse(list((self.project.root / "runs").glob("*/input")))

    def test_insight_identity_survives_gc(self):
        args = dict(source="searcher", title="a", body="b", base_snapshot=None, insight_id="retained")
        self.project.insights.submit(**args)
        decision = self.project.insights.decide("retained", "reject", "no", "", "test", expected_rev=1)
        decision["created_at"] = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=40)).isoformat()
        write_json(self.project.root / "decisions/retained.json", decision)
        self.assertEqual(self.project.insights.gc_decided(), 1)
        self.assertFalse(self.project.insights.submit(**args)["retained"])
        with self.assertRaises(Denied):
            self.project.insights.submit(**{**args, "body": "different"})

    def test_missing_decision_identity_is_not_reused(self):
        write_json(self.project.root / "decisions/old.json", {"action": "reject"})
        with self.assertRaises(Denied):
            self.project.insights.submit(source="searcher", title="a", body="b", base_snapshot=None, insight_id="old")

    def test_old_proposal_new_decision_is_retained(self):
        proposal = self.project.insights.submit(source="searcher", title="a", body="b", base_snapshot=None)
        ancient = time.time()-40*86400
        os.utime(self.project.root / "inbox" / (proposal["id"]+".json"), (ancient, ancient))
        self.project.insights.decide(proposal["id"], "reject", "no", "", "test", expected_rev=1)
        self.assertEqual(self.project.insights.gc_decided(), 0)

    def test_consultation_records_count_once(self):
        from mizu.usage import summarize
        def work(ctx, *_):
            ctx.handle("finish", {"outcome": "wait", "summary": "observed"})
        parent = self.context()
        result = Engine(self.config, driver=ScriptDriver(work)).consult(parent, {"question": "review", "profiles": [self.config.consult_profiles[0]]})
        self.assertEqual(len(result["answers"]), 1)
        facts = summarize(self.project)
        self.assertEqual(facts["totals"]["runs"], 1)
        self.assertEqual(facts["totals"]["requests"], 1)

    def test_consultation_slot_refusal_before_request(self):
        config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, parallel_runs=1))
        driver = ScriptDriver()
        with self.assertRaises(Busy):
            Engine(config, driver=driver).consult(self.context(), {"question": "review", "profiles": [config.consult_profiles[0]]})
        self.assertEqual(driver.calls, [])

    def test_backup_retains_incomplete_ingest(self):
        from mizu.storage import backup
        import tarfile
        write_json(self.project.root / ".ingest/pending.json", {"title": "a", "body": "b", "base_snapshot": None})
        self.project.set_control(paused=True)
        archive = self.root / "backup.tar.gz"
        backup(self.project, archive)
        with tarfile.open(archive) as stream:
            self.assertIn(".ingest/pending.json", stream.getnames())

    def test_mode_only_diff_is_visible(self):
        if os.name == "nt":
            self.skipTest("POSIX execution bits")
        old = self.project.snapshots.get()
        (self.project.workspace / "app.py").chmod(0o755)
        captured = self.project.snapshots.capture_files(self.project.workspace)
        new = self.project.snapshots.create(captured, goal=self.project.goal, state="next", run=None, outcome="wait", summary="mode")
        self.project.snapshots.publish(new)
        self.assertNotEqual(old["code_digest"], new["code_digest"])
        self.assertIn("mode", self.project.snapshots.changes(new["id"])["diff"])

    def test_service_names_do_not_collide_and_unowned_survive(self):
        from mizu.services import install, _scheduled_roles
        names = [entry[2] for entry in _scheduled_roles(self.config, self.project, ROOT / "bin/mizu")]
        self.assertTrue(all(name.startswith("mizu-6-sample-") for name in names))
        directory = self.root / "units"
        directory.mkdir()
        other = directory / "mizu-sample-child-worker.service"
        other.write_text("other project")
        install(self.config, self.project, ROOT / "bin/mizu", directory, system="linux")
        self.assertEqual(other.read_text(), "other project")

    def test_service_validation_failure_preserves_existing(self):
        from mizu.services import install
        from mizu.process import Result
        directory = self.root / "units"
        install(dataclasses.replace(self.config, timezone="local"), self.project, ROOT / "bin/mizu", directory, system="windows")
        before = {p.name:p.read_bytes() for p in directory.glob("*.xml")}
        with patch("mizu.services.subprocess.run", return_value=__import__("subprocess").CompletedProcess([],1,"","invalid")), patch("mizu.services.shutil.which", return_value="validator"):
            with self.assertRaises(Denied):
                install(self.config, self.project, ROOT / "bin/mizu", directory, system="linux")
        self.assertEqual(before, {p.name:p.read_bytes() for p in directory.glob("*.xml")})


class BoundaryTests(Fixture):
    def test_stdin_deadline_is_enforced(self):
        started=time.monotonic()
        result=run([sys.executable,"-c","import time; time.sleep(5)"], timeout=.05, maximum=1024, input_data=b"x"*1048576)
        self.assertEqual(result.reason,"timeout")
        self.assertLess(time.monotonic()-started, .75)

    def test_combined_output_is_bounded(self):
        result=run([sys.executable,"-c","import os; os.write(1,b'a'*65536); os.write(2,b'b'*65536)"],timeout=5,maximum=4096)
        self.assertEqual(result.reason,"output_limit")
        self.assertLessEqual(len(result.stdout.encode())+len(result.stderr.encode()),4096)

    def test_cancel_at_start(self):
        result=run([sys.executable,"-c","import time; time.sleep(5)"],timeout=5,maximum=1024,cancel=lambda:True,input_data=b"x"*1048576)
        self.assertEqual(result.reason,"cancelled")

    def test_child_holding_output_obeys_deadline(self):
        result=run([sys.executable,"-c","import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)'])"], timeout=.2, maximum=1024)
        self.assertEqual(result.reason,"timeout")
        self.assertLess(result.seconds,1)

    def test_mcp_notification_cannot_execute(self):
        from mizu.mcp_loop import serve_stdio
        messages=[{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},{"jsonrpc":"2.0","method":"tools/call","params":{"name":"write","arguments":{}}}]
        calls=[];output=io.BytesIO()
        serve_stdio(input_stream=io.BytesIO(b''.join(json.dumps(m).encode()+b'\n' for m in messages)),output_stream=output,server_name="test",list_tools=lambda:[],call_tool=lambda *args:calls.append(args))
        self.assertEqual(calls,[])
        self.assertEqual(len(output.getvalue().splitlines()),1)

    def test_mcp_response_bound_includes_json_escaping(self):
        from mizu.mcp_loop import serve_stdio
        messages=[{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},{"jsonrpc":"2.0","id":2,"method":"tools/list"}]
        output=io.BytesIO()
        serve_stdio(input_stream=io.BytesIO(b''.join(json.dumps(m).encode()+b'\n' for m in messages)),output_stream=output,server_name="test",list_tools=lambda:[{"name":"a","description":"\x01"*(MAX_FRAME//4)}],call_tool=lambda *args:{})
        for wire in output.getvalue().splitlines(keepends=True):
            self.assertLessEqual(len(wire),MAX_FRAME)
        self.assertIn("error",json.loads(output.getvalue().splitlines()[-1]))

    def test_paging_is_deterministic_and_byte_bounded(self):
        items=[{"path":str(i),"text":"水"*50} for i in range(20)]
        a=page(items,limit=20,maximum=1000)
        self.assertTrue(a["truncated"])
        b=page(items,offset=a["next_offset"],limit=20,maximum=1000)
        self.assertEqual(a["items"]+b["items"],items[:len(a["items"])+len(b["items"])])
        self.assertLess(len(json.dumps(a,ensure_ascii=False).encode()),1100)

    def test_drive_relative_paths_refused_on_all_hosts(self):
        for value in ("C:escape", "C:/escape", "../escape"):
            with self.assertRaises(Denied):relative_parts(value)

    def test_nonfinite_usage_is_isolated(self):
        from mizu.usage import normalize
        self.assertTrue(normalize({"input_tokens":float('inf')})["unknown_shape"])
        self.assertEqual(normalize({"arbitrary_count":100})["other_tokens"],0)

    def test_distribution_excludes_private_root_and_local_settings(self):
        from mizu.distribution import source_files
        root=self.root / "source-copy";root.mkdir()
        (root/"README.md").write_text("public")
        (root/"secrets.txt").write_text("private")
        (root/"config").mkdir();(root/"config/config.local.toml").write_text("private")
        self.assertEqual([p.as_posix() for p,_ in source_files(root)],["README.md"])

    def test_native_model_and_effort_are_explicit(self):
        from mizu.engine_config import effective
        settings=effective(self.config,self.config.roles['worker'],'primary')
        self.assertEqual(settings['model'],'test-model')
        self.assertEqual(settings['options']['thinkingLevel'],'off')
        self.assertNotIn('thinking',settings)

    def test_native_calendar_refuses_unrepresentable_timezone(self):
        from mizu.services import render
        import mizu.config as config_mod
        from unittest.mock import patch as _patch
        real_zoneinfo = config_mod.ZoneInfo
        def zoneinfo_or_fixed(name):
            try:
                return real_zoneinfo(name)
            except Exception:
                # render() only inspects the timezone string, so a fixed
                # offset stands in where the host ships no tz database and
                # the refusal branch stays covered on every platform.
                return dt.timezone(dt.timedelta(hours=9), name)
        with _patch.object(config_mod, "ZoneInfo", side_effect=zoneinfo_or_fixed):
            config=dataclasses.replace(self.config,timezone="Asia/Tokyo")
        for system in ("macos","windows"):
            with self.assertRaises(Denied):render(config,self.project,ROOT / "bin/mizu",system=system)
        self.assertTrue(render(config,self.project,ROOT / "bin/mizu",system="linux"))

    def test_windows_task_executes_python_and_valid_restart_interval(self):
        from mizu.services import render
        import xml.etree.ElementTree as ET
        units=render(dataclasses.replace(self.config,timezone="local"),self.project,ROOT / "bin/mizu",system="windows")
        worker=ET.fromstring(next(value for name,value in units.items() if name.endswith('-worker.xml')))
        ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(worker.find('t:Actions/t:Exec/t:Command',ns).text,sys.executable)
        self.assertEqual(worker.find('t:Settings/t:RestartOnFailure/t:Interval',ns).text,'PT1M')
        self.assertEqual(worker.find('t:Settings/t:ExecutionTimeLimit',ns).text,'PT0S')

    def test_every_publication_sync_failure_has_observable_commit_state(self):
        if os.name != "posix":
            self.skipTest("directory synchronization is POSIX-specific")
        from mizu.snapshot import Snapshots
        from mizu import fs
        probe = Snapshots(self.root / "sync-probe", excludes=(".git",), max_file=1024, max_bytes=1024, max_files=2)
        captured = probe.capture_files(self.project.workspace)
        probe.publish(probe.create(captured, goal="g", state="s", run=None, outcome="wait", summary="old"))
        next_record = probe.create(captured, goal="g", state="s", run="new", outcome="wait", summary="new")
        with patch("mizu.fs.os.fsync", wraps=fs.os.fsync) as syncing_probe:
            probe.publish(next_record)
            boundaries = syncing_probe.call_count
        self.assertGreaterEqual(boundaries, 4)
        for failure in range(1, boundaries + 1):
            with self.subTest(fsync_boundary=failure):
                root=self.root / f"store-{failure}"
                store=Snapshots(root,excludes=(".git",),max_file=1024,max_bytes=1024,max_files=2)
                captured=store.capture_files(self.project.workspace)
                old=store.create(captured,goal="g",state="s",run=None,outcome="wait",summary="old")
                store.publish(old)
                new=store.create(captured,goal="g",state="s",run="new",outcome="wait",summary="new")
                calls=0; real=fs.os.fsync
                def syncing(fd):
                    nonlocal calls
                    calls+=1
                    if calls==failure:raise OSError(errno.EIO,"injected")
                    return real(fd)
                with patch("mizu.fs.os.fsync",side_effect=syncing):
                    with self.assertRaises(OSError):store.publish(new)
                current=store.get()
                self.assertIn(current["id"],(old["id"],new["id"]))
                history=store.history(current["id"])
                self.assertEqual(history[-1]["id"],current["id"])
                if current["id"]==old["id"]:
                    self.assertNotIn(new["id"],[v["id"] for v in history])

    def test_service_removes_only_previously_owned_observer_files(self):
        from mizu.services import install
        directory=self.root / "owned"
        install(self.config,self.project,ROOT / "bin/mizu",directory,system="linux")
        roles=dict(self.config.roles)
        roles['reporter']=dataclasses.replace(roles['reporter'],calendar=(),interval_seconds=0,daemon=False)
        install(dataclasses.replace(self.config,roles=roles),self.project,ROOT / "bin/mizu",directory,system="linux")
        self.assertFalse((directory / 'mizu-6-sample-reporter.timer').exists())
        self.assertTrue((directory / 'mizu-6-sample-worker.service').exists())

    def test_invalid_restore_goal_never_exposes_project(self):
        from mizu.storage import backup, restore
        import tarfile
        self.project.set_control(paused=True)
        original=self.root / 'original.tar.gz';backup(self.project,original)
        broken=self.root / 'broken.tar.gz'
        with tarfile.open(original) as source, tarfile.open(broken,'w:gz') as target:
            for member in source:
                if member.isfile():
                    content=source.extractfile(member).read()
                    if member.name=='PROJECT.md':content=b''
                    member.size=len(content);target.addfile(member,io.BytesIO(content))
                else:target.addfile(member)
        with self.assertRaises(ConfigError):restore(self.config,'broken',broken)
        self.assertFalse((self.config.data / 'projects/broken').exists())

    def test_windows_utc_calendar_is_encoded_explicitly(self):
        from mizu.services import render
        units=render(dataclasses.replace(self.config,timezone="UTC"),self.project,ROOT / "bin/mizu",system="windows")
        import xml.etree.ElementTree as ET
        task=ET.fromstring(units['mizu-6-sample-reporter.xml'])
        ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertTrue(all(item.text.endswith('+00:00') for item in task.findall('t:Triggers/t:CalendarTrigger/t:StartBoundary',ns)))

    def test_unreadable_pointer_after_failure_records_unknown_publication(self):
        from mizu import runtime
        real=runtime.read_json
        def reading(path,*args,**kwargs):
            if path.name=='current.json':raise OSError(errno.EIO,'injected pointer read failure')
            return real(path,*args,**kwargs)
        def work(ctx,*_):ctx.commentary={'summary':'test'}
        with patch('mizu.runtime.publish',side_effect=Denied('injected report failure')), patch('mizu.runtime.read_json',side_effect=reading):
            with self.assertRaises(Denied):Engine(self.config,driver=ScriptDriver(work)).run(self.project,'worker')
        error=read_json(next((self.project.root / 'runs').glob('*/error.json')))
        self.assertEqual(error['publication'],'unknown')
        self.assertEqual(error['durability'],'unconfirmed')
