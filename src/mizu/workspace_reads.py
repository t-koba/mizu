"""Read-only workspace observations exposed by the runtime context."""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from . import vcs as _vcs
from .errors import Busy, Denied, LimitExceeded
from .fs import (DIGEST, READ_PAGE_CHARS, digest, page, read_json, relative_parts, safe_read)

MAX_READ_IDENTITIES = 1024

if TYPE_CHECKING:
    from .runtime import Context


def op_diff(context: Context, args: dict) -> dict:
    base = context._observed_diff_base()
    if base is not None:
        try:
            return context.project.snapshots.changes(context.snapshot["id"], base=base)
        except Denied:
            pass
    return context.project.snapshots.changes(context.snapshot["id"])

def observed_diff_base(context: Context, *, max_bytes: int) -> str | None:
    """Return this role's observed snapshot ID when it is usable as a diff base.

    Schema: the ``observed/<role>.json`` cursor the engine writes, holding
    ``snapshot`` and ``code_digest`` (and read-only scheduling evidence). Bounds:
    the cursor is read under the caller-supplied ``max_bytes``, shared with
    the runtime scheduler. Trust: local recorded state, never a
    claim about the tree, so an absent, malformed, digest-mismatched or
    no-longer-retained cursor is unusable and yields None; ancestry is
    rechecked by the snapshot store at diff time and falls back there.
    Failure: never raises for cursor problems; a valid cursor naming the
    current snapshot is usable and renders an empty diff.
    """
    try:
        cursor = read_json(
            context.project.root / "observed" / f"{context.role.name}.json",
            {}, max_bytes=max_bytes)
    except (OSError, ValueError, Busy, Denied, LimitExceeded):
        return None
    if not isinstance(cursor, dict):
        return None
    snapshot_id = cursor.get("snapshot")
    code_digest = cursor.get("code_digest")
    if (not isinstance(snapshot_id, str) or not DIGEST.fullmatch(snapshot_id)
            or not isinstance(code_digest, str) or not DIGEST.fullmatch(code_digest)):
        return None
    try:
        record = context.project.snapshots.get(snapshot_id)
    except (OSError, ValueError, Busy, Denied, LimitExceeded):
        return None
    if record.get("code_digest") != code_digest:
        return None
    return snapshot_id


def op_files(context: Context, args: dict) -> dict:
    if context.role.workspace == "none":
        names = []
    elif context.role.workspace != "write":
        names = sorted(context.snapshot["files"])
        names = sorted(names + context._injected_ref_paths())
        result = page(names, args.get("offset", 0), args.get("limit", 1000))
        result["files"] = result.pop("items")
        return result
    else:
        names = []
        for directory, dirs, files in os.walk(context.workspace, followlinks=False):
            rel = Path(directory).relative_to(context.workspace)
            dirs[:] = sorted(d for d in dirs if not context.project.snapshots.excluded((rel / d).as_posix()))
            for name in sorted(files):
                path = (rel / name).as_posix()
                if not context.project.snapshots.excluded(path):
                    names.append(path)
                    if len(names) > context.config.limits.snapshot_files:
                        raise LimitExceeded("Workspace file count exceeded")
        names.sort()
        names = names + context._injected_ref_paths()
        names.sort()
    result = page(names, args.get("offset", 0), args.get("limit", 1000))
    result["files"] = result.pop("items")
    return result


def injected_ref_paths(context: Context) -> list[str]:
    # Injected refs live in the project harness store outside the
    # working tree, served under the virtual refs/remotes/upstream/*
    # prefix even for read roles (snapshots capture the workspace
    # only, so materialized inputs lack them by construction).
    try:
        refs = _vcs.list_refs(context.project.root)
    except OSError as exc:
        raise Denied(f"Upstream refs are unavailable: {exc}") from exc
    return [_vcs.REF_PREFIX_PATH + "/" + name for name in refs]


def op_read(context: Context, args: dict, *, max_identities: int = MAX_READ_IDENTITIES) -> dict:
    path = args["path"]
    offset, limit = args.get("offset", 0), args.get("limit", READ_PAGE_CHARS)
    expected = args.get("content_id")
    if offset and expected is None:
        expected = context.read_content_ids.get(path)
    if expected is not None and (not isinstance(expected, str) or not DIGEST.fullmatch(expected)):
        raise Denied("Invalid read content_id")
    ref = _vcs.split_ref_path(path)
    if ref is not None:
        if context.role.workspace == "none":
            raise Denied("Path is not exposed to this role")
        # Existence probe, not a read: any workspace entry at the
        # virtual path collides, even a symlink, directory, special
        # file or hardlink that safe_read would refuse. Only a
        # missing path falls through to the virtual ref. Unreadable
        # states fail closed toward refusal.
        try:
            target = context.workspace.joinpath(*relative_parts(path))
        except (ValueError, Denied, OSError):
            collision = False
        else:
            try:
                os.lstat(target)
            except FileNotFoundError:
                collision = False
            except NotADirectoryError:
                collision = False
            except OSError:
                collision = True
            else:
                collision = True
        if collision:
            raise Denied("Path collides with the reserved virtual upstream-ref prefix; "
                         "relocate genuine files out of refs/remotes/upstream/")
        try:
            text = _vcs.read_ref(context.project.root, ref)
        except OSError as exc:
            raise Denied(f"Upstream refs are unavailable: {exc}") from exc
        data = text.encode("utf-8")
    else:
        if context.role.workspace == "none" or context.project.snapshots.excluded(path):
            raise Denied("Path is not exposed to this role")
        # file_bytes bounds the complete file; READ_PAGE_CHARS separately
        # bounds the decoded text returned in one protocol response.
        data = safe_read(context.workspace, path, context.config.limits.file_bytes)
        text = data.decode("utf-8", "replace")
    content_id = digest(data)
    if expected is not None and expected != content_id:
        raise Denied("Read file changed since the previous page")
    if path not in context.read_content_ids and len(context.read_content_ids) >= max_identities:
        raise LimitExceeded("Read identity retention bound exceeded")
    context.read_content_ids[path] = content_id
    end = min(len(text), offset + limit)
    return {"path": path, "text": text[offset:end], "content_id": content_id,
            "offset": offset, "next_offset": end if end < len(text) else None,
            "truncated": end < len(text)}
