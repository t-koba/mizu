"""Upstream-sync trusted argv adapter plus read-only ref injection (M1).

Schema: operator-owned ``[vcs] command`` argv receives one JSON object on
stdin (``{"op": ...}``) and must print one JSON object on stdout.
Bounds: request canonical bytes <= ``max_bytes``; stdout capped at
``max_bytes`` via ``process.run``; at most 4096 refs; ref names 1-512 chars.
Trust: operator-owned host program only; never model-provided argv, never run
in the sandbox. Injected refs are external-untrusted until merged and
verified. Retry/cancellation: single invocation under ``timeout_seconds``;
no shell retry. Evidence: caller binds results to receipts/digests.
Failure: ``Denied`` on unconfigured adapter, oversize request, timeout,
oversize output, nonzero exit, malformed JSON, invalid refs, or unsafe paths.

Ref injection (host side only): ``inject_refs`` writes validated refs as
read-only files under the reserved workspace subtree
``refs/remotes/upstream/*``. ``Snapshots.excluded`` always excludes that
subtree, so injected refs never affect ``code_digest`` even if the model
rewrites them; only a host-side fetch refreshes them. ``files``/``read``
serve them read-only from the project workspace.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .errors import Denied
from .fs import atomic_write, canonical, safe_read
from .process import run

#: Reserved workspace subtree for injected upstream refs. Operators must not
#: keep project source here; snapshots always exclude it (see
#: ``Snapshots.excluded``) so refs never affect ``code_digest``.
REF_PREFIX = ("refs", "remotes", "upstream")
REF_PREFIX_PATH = "/".join(REF_PREFIX)
#: Maximum refs per fetch/injection (matches the adapter shape bound).
MAX_REFS = 4096
#: Ref file content bound for reads (a hex sha plus newline is far smaller).
REF_CONTENT_MAX = 4096

_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")


def invoke(settings: dict, request: dict) -> dict:
    """Invoke the configured adapter once and return its JSON object.

    Schema: ``request`` is a JSON object with a string ``op`` field;
    the adapter must return a JSON object. Bounds: canonical request bytes
    and adapter stdout are each capped at ``max_bytes``; the call is bounded
    by ``timeout_seconds``. Trust: ``settings`` comes from operator config,
    not model output. Failure: ``Denied`` with a short reason; adapter
    output is capped at ``max_bytes`` (combined stdout+stderr via
    ``process.run``) so adapter chatter cannot blow the frame.
    """
    command = settings.get("command", [])
    timeout = settings.get("timeout_seconds", 20)
    maximum = settings.get("max_bytes", 524288)
    if not command:
        raise Denied("Upstream sync is not configured")
    if not isinstance(request, dict) or not isinstance(request.get("op"), str) or not request["op"]:
        raise Denied("VCS adapter request must be an object with a nonempty op string")
    try:
        payload = canonical(request)
    except (TypeError, ValueError) as exc:
        raise Denied("VCS adapter request is not JSON-serializable") from exc
    if len(payload) > maximum:
        raise Denied("VCS adapter request exceeds byte limit")
    result = run(list(command), timeout=timeout, maximum=maximum,
                 input_data=payload + b"\n")
    if result.reason == "timeout":
        raise Denied("Configured VCS program timed out")
    if result.reason == "output_limit":
        raise Denied("VCS adapter response exceeds byte limit")
    if result.exit_code != 0 or result.reason != "exited":
        raise Denied(f"Configured VCS program failed ({result.reason})")
    try:
        data = json.loads(result.stdout)
    except (ValueError, RecursionError) as exc:
        raise Denied("VCS adapter must return a JSON object") from exc
    if not isinstance(data, dict):
        raise Denied("VCS adapter must return a JSON object")
    return data


def validate_refs(refs) -> dict:
    """Validate a ``{name: sha}`` mapping shared by fetch and injection.

    Schema: names are ``refs/remotes/upstream/``-relative paths (nonempty,
    at most 512 chars, no NUL/newlines/backslashes/colons, no empty/``.``/
    ``..`` components, no leading slash); shas are 40/64 lowercase hex.
    Bounds: at most 4096 entries. Failure: ``Denied``.
    """
    if not isinstance(refs, dict) or len(refs) > MAX_REFS:
        raise Denied("VCS adapter must return a refs object with at most 4096 entries")
    for name, sha in refs.items():
        _check_ref_name(name)
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise Denied("Invalid VCS ref digest")
    return dict(refs)


def _check_ref_name(name) -> None:
    if not isinstance(name, str) or not name or len(name) > 512:
        raise Denied("Invalid VCS ref name")
    if "\n" in name or "\x00" in name or "\\" in name or ":" in name:
        raise Denied("Invalid VCS ref name")
    if name.startswith("/"):
        raise Denied("Invalid VCS ref name")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise Denied("Invalid VCS ref name")


def fetch_refs(settings: dict) -> dict:
    """Fetch upstream refs via ``{"op": "fetch"}`` and validate the shape.

    Schema: response ``{"refs": {name: sha}}`` validated by
    ``validate_refs``. Bounds: at most 4096 refs. Trust: refs are
    external-untrusted until merged and verified. Failure: ``Denied``.
    """
    data = invoke(settings, {"op": "fetch"})
    return {"refs": validate_refs(data.get("refs")), "trust": "external-untrusted"}


def split_ref_path(path: str) -> str | None:
    """Return the ref name for a workspace path under the reserved prefix.

    Schema: ``refs/remotes/upstream/<name>`` with a valid ref name, else
    ``None``. Bounds: name rules from ``_check_ref_name``. Trust: pure path
    syntax, no filesystem access. Failure: never raises.
    """
    if not isinstance(path, str):
        return None
    parts = path.split("/")
    if parts[:3] != list(REF_PREFIX) or len(parts) < 4:
        return None
    name = "/".join(parts[3:])
    try:
        _check_ref_name(name)
    except Denied:
        return None
    return name


def _ensure_host_dir(path: Path) -> None:
    """Create a host-side directory level, refusing symlinks and files."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o755)
        return
    except OSError as exc:
        raise Denied(f"Cannot inject upstream refs: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise Denied("Refusing to inject refs through a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise Denied("Upstream ref path is blocked by a non-directory")


def inject_refs(workspace: Path, refs: dict) -> dict:
    """Write validated refs as read-only files under the reserved subtree.

    Schema: ``refs`` is a ``{name: sha}`` mapping per ``validate_refs``;
    each ref lands at ``refs/remotes/upstream/<name>`` holding ``sha``
    plus a newline with mode 0o444. Stale files under the subtree are
    removed so a fetch refreshes the view; stale empty directories are
    left in place. Bounds: at most 4096 refs. Trust: host side only (the
    daemon/operator fetch path), never model-invoked; content stays
    external-untrusted. Retry/cancellation: single pass, no retry.
    Evidence: returned receipt names the prefix and count. Failure:
    ``Denied`` on invalid refs, symlink escape, or blocked paths.
    """
    validated = validate_refs(refs)
    if not workspace.is_dir() or workspace.is_symlink():
        raise Denied("Upstream refs require a real workspace directory")
    base = workspace.joinpath(*REF_PREFIX)
    current = workspace
    for part in REF_PREFIX:
        current = current / part
        _ensure_host_dir(current)
    wanted = set(validated)
    for name in sorted(wanted):
        parent = base
        for part in name.split("/")[:-1]:
            parent = parent / part
            _ensure_host_dir(parent)
        target = base.joinpath(*name.split("/"))
        try:
            existed = target.lstat()
        except FileNotFoundError:
            existed = None
        except OSError as exc:
            raise Denied(f"Cannot inject upstream refs: {exc}") from exc
        if existed is not None and stat.S_ISLNK(existed.st_mode):
            raise Denied("Refusing to overwrite a symlink with upstream ref content")
        atomic_write(target, (validated[name] + "\n").encode(), mode=0o444)
    _prune_stale(base, wanted)
    try:
        have = set(list_refs(workspace))
    except OSError as exc:
        raise Denied(f"Cannot verify injected upstream refs: {exc}") from exc
    if have != wanted:
        raise Denied("Injected upstream refs do not match the validated set")
    return {"injected": len(validated), "prefix": REF_PREFIX_PATH,
            "trust": "external-untrusted"}


def _prune_stale(base: Path, wanted: set[str]) -> None:
    """Remove injected files no longer present, refusing symlink games."""
    for directory, dirs, files in os.walk(base, followlinks=False):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink())
        for entry in sorted(files):
            target = here / entry
            try:
                if target.is_symlink():
                    raise Denied("Refusing to prune a symlinked upstream ref")
            except OSError as exc:
                raise Denied(f"Cannot prune upstream refs: {exc}") from exc
            rel = target.relative_to(base).as_posix()
            if rel not in wanted:
                try:
                    target.unlink()
                except OSError as exc:
                    raise Denied(f"Cannot prune upstream refs: {exc}") from exc


def list_refs(workspace: Path) -> list[str]:
    """List injected ref names (relative to the reserved prefix).

    Schema: sorted names; missing subtree reads as empty. Bounds: at most
    4096 entries or ``Denied``. Trust: host files; entries failing name
    validation (model-planted garbage) are skipped, never served. Failure:
    ``Denied`` when the subtree overflows; ``OSError`` only for host I/O.
    """
    base = workspace.joinpath(*REF_PREFIX)
    if not base.is_dir() or base.is_symlink():
        return []
    out: list[str] = []
    for directory, dirs, files in os.walk(base, followlinks=False):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink())
        for entry in sorted(files):
            target = here / entry
            if target.is_symlink():
                continue
            rel = target.relative_to(base).as_posix()
            try:
                _check_ref_name(rel)
            except Denied:
                continue
            out.append(rel)
    if len(out) > MAX_REFS:
        raise Denied("Too many injected upstream refs")
    return sorted(out)


def read_ref(workspace: Path, name: str) -> str:
    """Read one injected ref sha as text (read-only model view helper).

    Schema: ``name`` validated per ``_check_ref_name``; content must be a
    40/64 lowercase hex sha (trailing newline tolerated). Bounds: file
    capped at 4096 bytes via ``safe_read`` (no symlinks/hardlinks).
    Failure: ``Denied`` on bad names, unsafe paths, or malformed content.
    """
    _check_ref_name(name)
    data = safe_read(workspace, REF_PREFIX_PATH + "/" + name, REF_CONTENT_MAX)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Denied("Invalid upstream ref content") from exc
    if not _SHA.fullmatch(text.strip()):
        raise Denied("Invalid upstream ref content")
    return text.strip()
