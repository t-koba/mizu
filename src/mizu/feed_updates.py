"""Role-scoped feed discovery; resumable delivery from complete observations.

Workers observe one feed behind the existing killable network boundary. The
parent holds one project cursor lock only through cleanup, cursor reads, response
sizing and atomic replacement, never through network work. All callers read the
latest cursor under that lock, so concurrent observations cannot lose updates.
"""
from __future__ import annotations

import stat

from .errors import Denied, MizuError
from .fs import ATOMIC_WRITE_TEMP_PREFIX, DIGEST, canonical, digest, loads_finite, lock, mkdir, safe_read, safe_write

# Existing per-feed configuration permits at most 1000 entries. State keeps
# at most 1000 completed and 1000 partially delivered SHA-256 identities,
# plus a fixed version/scope envelope, never XML.
_STATE_BYTES = 2 * 1000 * 67 + 1024


def _previous(root, name, role, feed):
    path = root / name
    try:
        path.lstat()
    except FileNotFoundError:
        return set(), set()
    raw = safe_read(root, name, _STATE_BYTES)
    state = loads_finite(raw)
    if not isinstance(state, dict) or raw != canonical(state):
        raise Denied("Invalid feed cursor")
    checksum = state.pop("checksum", None)
    if checksum != digest(canonical(state)):
        raise Denied("Invalid feed cursor checksum")
    version = state.get("version")
    fields = {"version", "role", "feed", "identities"}
    if (type(version) is not int or version not in (1, 2)
            or set(state) != (fields if version == 1 else fields | {"delivered"})
            or state["role"] != role or state["feed"] != feed):
        raise Denied("Invalid feed cursor")
    sets = []
    for keys in (state["identities"], state.get("delivered", [])):
        if (not isinstance(keys, list) or len(keys) > 1000
                or any(not isinstance(key, str) or not DIGEST.fullmatch(key) for key in keys)
                or len(set(keys)) != len(keys)):
            raise Denied("Invalid feed cursor")
        sets.append(set(keys))
    if sets[0] & sets[1]:
        raise Denied("Invalid feed cursor")
    return sets[0], sets[1]


def _directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise Denied("Unsafe feed cursor directory")


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise Denied("Unsafe feed cursor file")


def _retire(base, roles, configured):
    """Validate cleanup targets before deleting; never recurse or follow links.

    All cursor access uses the project lock, including retirement. Old role lock
    files are obsolete: no caller opens them, so the same gate can retire them.
    The project lock itself lives outside this tree and lasts with the project.
    """
    files, directories = [], []
    for path in base.iterdir():
        if DIGEST.fullmatch(path.name):
            _directory(path)
            if path.name not in roles:
                directories.append(path)
            for child in path.iterdir():
                cursor = child.suffix == ".json" and DIGEST.fullmatch(child.stem)
                temporary = child.name.startswith(ATOMIC_WRITE_TEMP_PREFIX)
                if not (cursor or temporary):
                    raise Denied("Unexpected feed cursor file")
                if path.name not in roles or temporary or child.name not in configured:
                    _regular(child)
                    files.append(child)
        elif path.suffix == ".lock" and DIGEST.fullmatch(path.stem):
            _regular(path)
            files.append(path)
        else:
            raise Denied("Unexpected feed cursor path")
    for path in files:
        path.unlink()
    for path in directories:
        path.rmdir()


def _status(feed):
    return {"feed": feed, "entries": [], "errors": [], "overflow": None,
            "complete": False, "truncated": False, "cursor_advanced": False}


def _metadata_size(feed):
    # The worker's largest error list; all single parent failures (including
    # state_write_failed/cancelled) are shorter. False is the longest boolean/
    # null spelling. Optional diagnostics are charged separately as payload.
    return len(canonical({**_status(feed), "overflow": False,
                          "errors": ["ambiguous_identity", "duplicate_identity", "missing_identity"]}))


def _fit_row(row, room):
    """Preserve identity/evidence; explicitly empty optional text if necessary."""
    compact = {**row, "truncated_fields": []}
    for field in ("summary", "title", "url"):
        if compact[field]:
            compact[field] = ""
            compact["truncated_fields"].append(field)
            if len(canonical(compact)) <= room:
                return compact
    return None


def _read_feed(context, role, root, name, status, room):
    """Consume one worker result before starting the next worker.

    Only returnable rows escape this frame, not the observation or identity set.
    Metadata (including any write failure) is already reserved by read().
    """
    try:
        observation = context._bounded_web_operation(
            context.config, context.run_dir, "feed_updates", status["feed"],
            context.tools_cancelled, role)
    except (MizuError, OSError, ValueError) as exc:
        observation = {"errors": ["observation_failed"], "detail": str(exc)[:512]}
    # The network worker never holds this gate. Reopen the latest cursor only
    # here: another caller may have committed or retired state during the fetch.
    with lock(context.project.root / "feed-updates.lock"):
        mkdir(root)
        _directory(root)
        try:
            previous, delivered = _previous(root, name, role, name[:-5])
        except (MizuError, OSError, ValueError, TypeError):
            status["errors"] = ["corrupt_state"]
            return room
        if "detail" in observation:
            status["errors"] = observation["errors"]
            detail = observation["detail"]
            # Diagnostics are optional; unlike cursor-bearing entries they can
            # be clipped. Charge their escaped UTF-8 size, not character count.
            while detail and len(canonical({"detail": detail})) - 1 > room:
                detail = detail[:-1]
            if detail:
                status["detail"] = detail
                room -= len(canonical({"detail": detail})) - 1
            return room
        status["overflow"] = observation["overflow"]
        status["errors"] = observation["errors"]
        identities = {row["identity"] for row in observation["entries"]}
        # Only a complete source can prune or extend partial-delivery progress.
        # Keep the last completed identity set intact until every new ID fits.
        delivered &= identities - previous
        seen = previous | delivered
        for row in observation["entries"]:
            if row["identity"] in seen:
                continue
            separator = 1 if status["entries"] else 0
            # Do not clip a later row just to fill leftover room. On a repeat
            # it becomes the first undelivered row and can use the full budget.
            fitted = row
            if len(canonical(row)) + separator > room:
                fitted = _fit_row(row, room) if not status["entries"] else None
            if fitted is None:
                status["truncated"] = True
                break
            status["entries"].append(fitted)
            room -= len(canonical(fitted)) + separator
        status["complete"] = observation["complete"] and not status["truncated"]
        if not observation["complete"]:
            return room
        delivered.update(row["identity"] for row in status["entries"])
        if context.tools_cancelled():
            status["complete"] = False
            status["errors"] = ["cancelled"]
            return room
        try:
            state = {"version": 2, "role": role, "feed": name[:-5],
                     "identities": sorted(identities if status["complete"] else previous),
                     "delivered": [] if status["complete"] else sorted(delivered)}
            safe_write(root, name, canonical({**state, "checksum": digest(canonical(state))}))
        except (MizuError, OSError, ValueError):
            status["complete"] = False
            status["errors"] = ["state_write_failed"]
        else:
            status["cursor_advanced"] = status["complete"]
    return room


def read(context):
    """Read one feed at a time, keeping only bounded response/status data.

    State belongs to the authenticated role, never a caller argument. Reserve
    metadata before observing anything: entries already committed will never be
    evicted to fit a later feed or failure. If metadata alone exceeds the bound,
    return every status for generic result paging without observing any feed.
    """
    role = context.role.name
    root = context.project.root / "feed-updates" / digest(role.encode())
    feeds = dict.fromkeys(context.config.web["feeds"])
    names = {digest(feed.encode()) + ".json" for feed in feeds}
    response = {"feeds": [], "scope": "configured-feeds-only", "trust": "external-untrusted",
                "complete": False, "truncated": False}
    if role not in context.config.roles:
        raise Denied("Feed cursor role is not configured")
    # A single permanent gate also protects cleanup from live cursor writes.
    with lock(context.project.root / "feed-updates.lock"):
        mkdir(root.parent)
        _directory(root.parent)
        _retire(root.parent, {digest(name.encode()) for name in context.config.roles}, names)
        mkdir(root)
        _directory(root)
    room = context.config.limits.tool_response_bytes - len(canonical(response))
    for feed in feeds:
        status = _status(feed)
        cost = _metadata_size(feed) + (1 if response["feeds"] else 0)
        response["feeds"].append(status)
        room -= cost
    if room < 0:
        response["truncated"] = True
        return response
    for status in response["feeds"]:
        room = _read_feed(context, role, root, digest(status["feed"].encode()) + ".json", status, room)
        # Once finalized, this feed needs no further failure reserve. Reclaim
        # only metadata slack, never entries or optional detail already charged.
        metadata = {**status, "entries": []}
        metadata.pop("detail", None)
        room += _metadata_size(status["feed"]) - len(canonical(metadata))
    response["complete"] = not response["truncated"] and all(status["complete"] for status in response["feeds"])
    response["truncated"] |= any(status["truncated"] for status in response["feeds"])
    return response
