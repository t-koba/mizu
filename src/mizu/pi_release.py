"""Operator-only Pi release signal. No adoption or package availability check."""
import datetime as dt
import re
import tempfile
from pathlib import Path

from . import operator_records
from .errors import Denied
from .fs import digest, loads_finite, safe_read
from .web import Web, match_host

SOURCE = 'https://api.github.com/repos/earendil-works/pi/releases/latest'
RELEASES = 'https://github.com/earendil-works/pi/releases/tag/'
STABLE = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _version(value):
    if not isinstance(value, str) or len(value) > 64 or not STABLE.fullmatch(value):
        raise ValueError('Expected an exact stable SemVer')
    return tuple(map(int, value.split('.')))


def pinned_version(root):
    """Require both manifests, root lock declarations and all installed Pi pins."""
    versions = set()
    try:
        for adapter, required in (('pi', 'pi-coding-agent'), ('pi-durable', 'pi-durable')):
            def read(name):
                return loads_finite(safe_read(root, f'adapters/{adapter}/{name}', 1024 * 1024),
                                    object_pairs_hook=_unique)
            manifest, lock = read('package.json'), read('package-lock.json')
            dependencies = manifest['dependencies']
            packages = lock['packages']
            if packages['']['dependencies'] != dependencies:
                raise ValueError('Lock root declarations differ from manifest')
            scoped = {name: version for name, version in dependencies.items()
                      if name.startswith('@earendil-works/')}
            for name in (required, 'pi-ai'):
                if '@earendil-works/' + name not in scoped:
                    raise ValueError('Missing required Pi dependency')
            for name, version in scoped.items():
                _version(version)
                if packages['node_modules/' + name]['version'] != version:
                    raise ValueError('Lock differs from manifest')
                versions.add(version)
            for path, package in packages.items():
                installed = path.rsplit('/node_modules/', 1)[-1]
                if installed.startswith('@earendil-works/') and installed.count('/') == 1:
                    _version(package['version'])
                    versions.add(package['version'])
        if len(versions) != 1:
            raise ValueError('Pi pins differ')
        return versions.pop()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Denied('Pi manifests and lockfiles must resolve one stable release') from exc


def _release(receipt):
    try:
        if (receipt['url'] != SOURCE or receipt['final_url'] != SOURCE
                or receipt['content_type'] != 'application/json'
                or receipt['trust'] != 'external-untrusted'):
            raise ValueError('Unexpected fetch source or representation')
        release = loads_finite(receipt['text'], object_pairs_hook=_unique)
        tag = release['tag_name']
        if not isinstance(tag, str) or not tag.startswith('v'):
            raise ValueError('Missing stable tag')
        _version(tag[1:])
        published = release['published_at']
        if (release['draft'] is not False or release['prerelease'] is not False
                or release['html_url'] != RELEASES + tag
                or not isinstance(published, str)
                or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', published)):
            raise ValueError('Incomplete stable release metadata')
        dt.datetime.strptime(published, '%Y-%m-%dT%H:%M:%SZ')
        return tag, published
    except (ValueError, KeyError, TypeError) as exc:
        raise Denied('Expected complete official stable Pi release metadata') from exc


def check(project, root, role):
    """One bounded configured fetch, then an exact-replay operator notice if newer."""
    policy = operator_records.configuration(project.config.operator_records)
    if not policy['operator.notice']['enabled']:
        raise Denied('operator.notice must be explicitly enabled')
    if role not in project.roles or role not in project.config.roles:
        raise Denied('Release notice requires an explicit configured project role')
    settings = project.config.web
    if match_host('api.github.com', settings['hosts']) is None:
        raise Denied('api.github.com must be web allowlisted')
    baseline = pinned_version(root)
    # Raw source receipts are run-scoped in the normal Web path. This host-only
    # check needs only the parsed fields in the durable notice, so keep its one
    # bounded receipt temporary rather than growing a permanent per-poll store.
    with tempfile.TemporaryDirectory(prefix='mizu-pi-release-') as receipt_dir:
        web = Web(settings, project.config.data / 'web-cache', Path(receipt_dir), role)
        receipt = web.fetch(SOURCE)
        tag, published = _release(receipt)
    result = {'baseline': baseline, 'tag': tag, 'source': SOURCE,
              'newer': _version(tag[1:]) > _version(baseline)}
    if result['newer']:
        body = (f'Pi release review: pinned baseline {baseline}; upstream {tag}; published {published}.\n'
                f'Source: {SOURCE}\nRelease: {RELEASES}{tag}\n'
                'external-untrusted; review only; no package availability or compatibility claim.\n'
                'Review this release for lockstep adoption; no dependency changes or adoption performed.')
        result['notice'] = operator_records.write(project, event_id='pi-release-' + digest(tag.encode())[:48],
            kind='operator.notice', addressee=role, body=body)
    return result
