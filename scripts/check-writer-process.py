from pathlib import Path
import argparse,json,os,subprocess,sys,time
parser = argparse.ArgumentParser(description='Separate-OS-process single-writer publication check; model is synthetic.')
parser.add_argument('root', type=Path, help='Repository root under test')
parser.add_argument('--report', type=Path, help='Write the writer receipt (regenerable evidence, e.g. private-validation/writer.json)')
args = parser.parse_args()
ROOT=args.root.resolve()
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from support import Fixture
CHILD='''
import dataclasses,json,sys,time
from pathlib import Path
from mizu.config import load
from mizu.project import Project
from mizu.runtime import Engine
from mizu.errors import Busy
from support import ScriptDriver
config=load(Path(sys.argv[1]));directory=Path(sys.argv[2]);which=sys.argv[3]
roles=dict(config.roles);roles['reviewer']=dataclasses.replace(roles['worker'],name='reviewer');config=dataclasses.replace(config,roles=roles)
def hold(ctx,*_):
    (directory/'entered').write_text(ctx.run_dir.name)
    deadline=time.monotonic()+10
    while not (directory/'release').exists():
        if time.monotonic()>deadline:raise RuntimeError('release timed out')
        time.sleep(.01)
try:
    result=Engine(config,driver=ScriptDriver(hold if which=='worker' else None)).run(Project(config,'sample'),which)
    print(json.dumps({'status':'published','snapshot':result['snapshot']}))
except Busy as exc:
    print(json.dumps({'status':'busy','reason':str(exc)}));sys.exit(20)
'''
fixture=Fixture();fixture.setUp();child=None
try:
    initial=fixture.project.snapshots.get()['id'];history=len(fixture.project.snapshots.history(initial,100))
    env={**os.environ,'PYTHONPATH':os.pathsep.join([str(ROOT/'src'),str(ROOT/'tests')])}
    argv=[sys.executable,'-c',CHILD,str(fixture.file),str(fixture.root)]
    child=subprocess.Popen([*argv,'worker'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    deadline=time.monotonic()+5
    while not (fixture.root/'entered').exists():
        assert child.poll() is None,'first writer failed'
        assert time.monotonic()<deadline,'first writer not ready'
        time.sleep(.01)
    second=subprocess.run([*argv,'reviewer'],env=env,capture_output=True,text=True,timeout=5)
    assert second.returncode==20,(second.returncode,second.stderr,second.stdout)
    denied=json.loads(second.stdout);assert denied['status']=='busy';assert 'workspace' in denied['reason'],denied
    assert fixture.project.snapshots.get()['id']==initial
    (fixture.root/'release').write_text('release')
    stdout,stderr=child.communicate(timeout=5);assert child.returncode==0,(stderr,stdout)
    published=json.loads(stdout);current=fixture.project.snapshots.get()
    assert published['status']=='published' and current['id']==published['snapshot'] and current['id']!=initial
    recent=fixture.project.snapshots.history(current['id'],100);assert len(recent)==history+1
    assert current['run']==(fixture.root/'entered').read_text()
    assert not (fixture.project.root/'active'/'worker.json').exists()
    assert len(list((fixture.project.root/'runs').iterdir()))==1
    receipt={'status':'pass','kind':'separate-os-process-writer-publication','os':sys.platform,'writers':2,'distinct_roles':True,'different_processes':True,'second_writer':'busy-workspace-before-driver','published_snapshots':1,'pointer_history_consistent':True,'active_cleaned':True,'model_driver':'synthetic','real_inference':'not_run'}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(receipt)+'\n')
    print(json.dumps(receipt))
finally:
    if child is not None:
        if child.poll() is None:child.kill()
        child.communicate()
    fixture.doCleanups()
