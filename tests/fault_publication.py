"""Subprocess-only write-boundary fault injector; never imported by Mizu."""
import errno
import json
import os
from pathlib import Path
import sys
from mizu import fs
from mizu.snapshot import Snapshots


def main():
    root, mode, point = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
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
    print(json.dumps(events))


if __name__ == '__main__':main()
