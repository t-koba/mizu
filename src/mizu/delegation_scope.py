"""Private, read-only Linux empty-scope proof over a trusted saved job binding.

No launch, cleanup, ledger mutation or container proof lives here. The host must
have fenced new launches and settled its supervisor before using the result.
Only the existing mizu-delegation-<run_id>.service identity is supported. A caller
supplies a bounded read-only systemctl-show query (unit -> process.Result), its
read budget, and, for offline fixtures only, an alternate cgroup v2 root. The
query owns timeout policy; this module never spawns or signals a process.
Unknown evidence raises; False means observed live/nonempty state, not absence.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import re
import stat
import sys

from .delegation_jobs import _binding
from .errors import Denied


def _scope_empty(binding, *, query, maximum, cgroup_root=Path('/sys/fs/cgroup')):
    """Prove exact inactive unit + empty cgroup, or consistent unit/group absence.

    Loaded units must report the exact saved ControlGroup when it still exists.
    An absent group is proof only with an empty ControlGroup and an inactive
    loaded/not-found unit, after observing cgroup v2 and every exact user-manager
    ancestor.
    Missing evidence files/ancestors, aliases and partial reads are never absence.
    The host protects the saved binding and cgroup hierarchy from replacement;
    dirfd/no-follow reads prevent following substituted symlinks.
    """
    if sys.platform != 'linux':
        raise Denied('Linux delegation scope evidence required')
    run_id = binding.get('run_id') if type(binding) is dict else None
    if not isinstance(run_id, str) or re.fullmatch(r'[0-9a-f]{32}', run_id) is None:
        raise Denied('Invalid delegation scope run identity')
    _binding(binding, run_id)
    scope = binding['scope']
    if type(maximum) is not int or maximum <= 0:
        raise Denied('Delegation scope read budget required')

    result = query(scope['unit'])  # identity validated before any host query
    if (result.reason != 'exited' or type(result.exit_code) is not int
            or result.exit_code not in (0, 1) or result.survivors
            or len(result.stdout.encode('utf-8')) > maximum):
        raise Denied('Unobservable delegation systemd scope')
    fields = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition('=')
        if not sep or key in fields:
            raise Denied('Malformed delegation systemd scope')
        fields[key] = value
    if (set(fields) != {'LoadState', 'ActiveState', 'ControlGroup'}
            or fields['LoadState'] not in ('loaded', 'not-found')
            or fields['ActiveState'] not in ('active', 'activating', 'deactivating',
                                              'reloading', 'failed', 'inactive')
            or fields['ControlGroup'] not in ('', scope['cgroup'])
            or (result.exit_code == 1 and fields['LoadState'] != 'not-found')
            or (fields['LoadState'] == 'not-found'
                and (fields['ActiveState'] != 'inactive' or fields['ControlGroup']))):
        raise Denied('Unknown or mismatched delegation systemd scope')
    if fields['ActiveState'] != 'inactive':
        return False

    # Open each component, including ancestors of an injected test root, without
    # following links. Missing mount/user-manager ancestors are not an empty run.
    root = Path(cgroup_root)
    if not root.is_absolute() or '..' in root.parts:
        raise Denied('Invalid cgroup root')
    with ExitStack() as stack:
        def directory(name, parent=None):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd

        def read(parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise Denied('Unsafe delegation cgroup evidence')
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    raw = stream.read(maximum + 1)
                if len(raw) > maximum:
                    raise Denied('Delegation cgroup evidence exceeds read budget')
                return raw.decode('ascii')
            finally:
                os.close(fd)

        current = directory(root.anchor)
        for component in root.parts[1:]:
            current = directory(component, current)
        controllers = read(current, 'cgroup.controllers').split()
        if (not controllers or len(set(controllers)) != len(controllers)
                or any(re.fullmatch(r'[a-z][a-z0-9_]*', item) is None for item in controllers)):
            raise Denied('Missing or malformed cgroup v2 evidence')
        parent = current
        for component in scope['cgroup'][1:].split('/')[:-1]:
            parent = directory(component, parent)
        try:
            group = directory(scope['unit'], parent)
        except FileNotFoundError:
            if fields['ControlGroup']:
                raise Denied('Systemd reports a missing delegation cgroup')
            return True
        if fields['ControlGroup'] != scope['cgroup']:
            raise Denied('Delegation cgroup exists without matching systemd identity')
        events = {}
        for line in read(group, 'cgroup.events').splitlines():
            pair = line.split()
            if (len(pair) != 2 or pair[0] in events
                    or re.fullmatch(r'[a-z_]+', pair[0]) is None
                    or pair[1] not in ('0', '1')):
                raise Denied('Malformed delegation cgroup events')
            events[pair[0]] = pair[1]
        if 'populated' not in events:
            raise Denied('Missing delegation cgroup populated evidence')
        procs = read(group, 'cgroup.procs')
        if any(re.fullmatch(r'[1-9][0-9]*', line) is None for line in procs.splitlines()):
            raise Denied('Malformed delegation cgroup process evidence')
        return events['populated'] == '0' and not procs.strip()
