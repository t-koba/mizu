"""Subprocess-only write-boundary fault injector; never imported by Mizu."""
import errno
import json
import os
from pathlib import Path
import sys
from mizu import fs
from mizu.snapshot import Snapshots


def run_case(root, mode, point):
    """One publish attempt with a fault at the point-th boundary.

    Returns the boundary events observed. Crash mode never returns: the
    process exits mid-publish to simulate death between boundaries.
    """
    events = []
    def boundary(name, phase):
        events.append([name, phase])
        if len(events) == point:
            if mode == 'crash':
                os._exit(86)
            raise OSError(errno.ENOSPC if mode == 'enospc' else errno.EIO, 'injected')
    originals = {name:getattr(fs.os,name) for name in ('fsync','replace','link')}
    def wrapper(name):
        def call(*args,**kwargs):
            boundary(name,'before')
            result=originals[name](*args,**kwargs)
            boundary(name,'after')
            return result
        return call
    fdopen=fs.os.fdopen
    class Stream:
        def __init__(self,stream):self.stream=stream
        def __enter__(self):self.stream.__enter__();return self
        def __exit__(self,*args):return self.stream.__exit__(*args)
        def __getattr__(self,name):return getattr(self.stream,name)
        def write(self,data):
            boundary('write','before')
            result=self.stream.write(data)
            boundary('write','after')
            return result
    for name in originals:setattr(fs.os,name,wrapper(name))
    fs.os.fdopen=lambda *a,**k:Stream(fdopen(*a,**k))
    try:
        store=Snapshots(root/'store',excludes=(),max_file=1024,max_bytes=4096,max_files=4)
        captured=store.capture_files(root/'workspace')
        new=store.create(captured,goal='g',state='s',run='new',outcome='wait',summary='new')
        store.publish(new)
    except OSError:
        if mode == 'trace':raise
    finally:
        for name,value in originals.items():setattr(fs.os,name,value)
        fs.os.fdopen=fdopen
    return events


def main():
    root, mode, point = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    if point == 'batch':
        # Recoverable-fault points over pre-made pristine case trees, one
        # spawn per mode instead of one per point. Crash mode is never
        # batched: each point needs its own process death. Cases run in
        # numeric order on disjoint trees with fresh state per point.
        if mode == 'crash':
            raise SystemExit('batch supports only recoverable fault modes')
        points = sorted(int(p.name) for p in root.iterdir()
                        if p.name.isdigit() and not p.is_symlink() and p.is_dir())
        print(json.dumps([run_case(root/str(point), mode, point) for point in points]))
        return
    print(json.dumps(run_case(root, mode, int(point))))


if __name__ == '__main__':main()
