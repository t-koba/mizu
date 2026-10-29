"""Data-dir-owned shared smoke bundles: bounded preview, retirement and recovery.

No provider work or project/workspace locks here. All mutators use the same
nonblocking global lock. Preview is read-only, including quarantine recovery.
Unknown or incomplete metadata refuses the entire expiry plan before any rename.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

from .errors import Busy, Denied, LimitExceeded
from .fs import canonical, digest, loads_finite, now, safe_read, safe_write, sync_dir, write_json, identifier
from .smoke_refs import SMOKE_ID, shared_lock, validate_refs

MAX_PROJECTS = 5000
MAX_PAIRS = 5000
MAX_REFS = 20000
MAX_METADATA = 64 * 1024 * 1024
MAX_RECORD = 128 * 1024
MAX_ENTRIES = 100000  # Includes empty directories and records, not payloads.
MAX_DAYS = 36500
AUDIT_LIMIT = 128


def directory(path: Path, *, optional=False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return False
        raise Denied("Missing smoke retention directory") from None
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or getattr(info, 'st_reparse_tag', 0):
        raise Denied("Unsafe smoke retention directory")
    return True


def entries(path: Path, maximum: int, *, optional=False):
    if not directory(path, optional=optional):
        return []
    result = []
    with os.scandir(path) as stream:
        for entry in stream:
            if len(result) >= maximum:
                raise LimitExceeded("Smoke retention entry bound exceeded")
            result.append(Path(entry.path))
    return sorted(result)


class Metadata:
    def __init__(self, data: Path):
        self.data = data
        self.bytes = 0
        self.count = 0
        self.entries = 0
        self.cache = {}
        self.has_refs = False

    def read(self, path: Path, maximum=MAX_RECORD):
        name = path.relative_to(self.data).as_posix()
        if name in self.cache:
            return self.cache[name]
        raw = safe_read(self.data, name, maximum)
        self.bytes += len(raw)
        self.count += 1
        if self.bytes > MAX_METADATA or self.count > MAX_ENTRIES:
            raise LimitExceeded("Smoke retention metadata bound exceeded")
        try:
            loads_finite(raw)  # Depth and finite-number guard before the hook.
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate metadata key")
                    result[key] = value
                return result
            record = json.loads(raw, object_pairs_hook=unique)
        except (ValueError, UnicodeError) as exc:
            raise Denied("Malformed smoke retention metadata") from exc
        if not isinstance(record, dict):
            raise Denied("Smoke retention metadata must be an object")
        def check_refs(value):
            if isinstance(value, dict):
                if "smoke_refs" in value:
                    validate_refs(value["smoke_refs"])
                    self.has_refs = self.has_refs or bool(value["smoke_refs"])
                for child in value.values():
                    check_refs(child)
            elif isinstance(value, list):
                for child in value:
                    check_refs(child)
        check_refs(record)
        self.cache[name] = record
        return record


def policy(data: Path, metadata: Metadata | None = None) -> int:
    metadata = metadata or Metadata(data)
    path = data / "validation" / "retention-policy.json"
    directory(data)
    if not directory(path.parent, optional=True):
        return 31
    try:
        path.lstat()
    except FileNotFoundError:
        return 31
    record = metadata.read(path, 1024)
    if (set(record) != {"version", "days"} or type(record["version"]) is not int
            or record["version"] != 1 or type(record["days"]) is not int
            or not 0 <= record["days"] <= MAX_DAYS):
        raise Denied("Invalid shared smoke retention policy")
    return record["days"]


def set_days(data: Path, days: int) -> dict:
    if type(days) is not int or not 0 <= days <= MAX_DAYS:
        raise Denied(f"Smoke retention days must be 0-{MAX_DAYS}")
    with shared_lock(data):
        policy(data)  # Never silently repair unsafe policy state.
        (data / "validation").mkdir(exist_ok=True, mode=0o700)
        write_json(data / "validation" / "retention-policy.json", {"version": 1, "days": days})
    return {"days": days}


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value) if isinstance(value, str) and len(value) <= 64 else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.utcoffset() is None:
        raise Denied("Smoke completion must be a timezone-aware timestamp")
    return parsed


def project_identity(path: Path):
    directory(path)
    info = path.lstat()
    return {"device": info.st_dev, "inode": info.st_ino}


def _manifest(metadata: Metadata, smoke_id: str, pair: Path) -> dict:
    if not SMOKE_ID.fullmatch(smoke_id):
        raise Denied("Invalid smoke pair ID")
    manifest = metadata.read(pair / "manifest.json", 4096)
    if (set(manifest) != {"version", "smoke_id", "status", "completed_at", "report", "project"}
            or type(manifest["version"]) is not int or manifest["version"] != 1
            or manifest["smoke_id"] != smoke_id or manifest["status"] not in ("pass", "fail")):
        raise Denied("Unknown or incomplete smoke manifest")
    timestamp(manifest["completed_at"])
    binding = manifest["report"]
    if (not isinstance(binding, dict) or set(binding) != {"sha256", "bytes"}
            or type(binding["bytes"]) is not int or not 1 <= binding["bytes"] <= 16 * 1024 * 1024):
        raise Denied("Invalid smoke report identity")
    project = manifest["project"]
    if project is not None and (not isinstance(project, dict) or set(project) != {"device", "inode"}
                                or any(type(v) is not int or v < 0 for v in project.values())):
        raise Denied("Invalid smoke project identity")
    return manifest


def _report(metadata: Metadata, smoke_id: str, pair: Path, manifest: dict) -> dict:
    binding = manifest["report"]
    raw = safe_read(metadata.data, (pair / "report.json").relative_to(metadata.data).as_posix(), binding["bytes"])
    if len(raw) != binding["bytes"] or digest(raw) != binding["sha256"]:
        raise Denied("Smoke report identity mismatch")
    try:
        loads_finite(raw)  # Depth and finite-number guard before duplicate-key validation.
        def unique_report_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate report key")
                result[key] = value
            return result
        report = json.loads(raw, object_pairs_hook=unique_report_keys)
    except (ValueError, UnicodeError) as exc:
        raise Denied("Malformed smoke report") from exc
    if (not isinstance(report, dict) or report.get("status") != manifest["status"]
            or report.get("time") != manifest["completed_at"]
            or report.get("smoke_id") != smoke_id):
        raise Denied("Contradictory smoke pair")
    return report


def read_pair(metadata: Metadata, smoke_id: str, *, path: Path | None = None):
    pair = path or metadata.data / "validation" / "pairs" / smoke_id
    names = {p.name for p in entries(pair, 4)}
    manifest = _manifest(metadata, smoke_id, pair)
    expected = {"manifest.json", "report.json"} | ({"project"} if manifest["project"] is not None else set())
    if names != expected:
        raise Denied("Smoke pair members do not match manifest")
    _report(metadata, smoke_id, pair, manifest)
    if manifest["project"] is not None and manifest["project"] != project_identity(pair / "project"):
        raise Denied("Smoke project identity mismatch")
    return manifest


def read_committed(metadata: Metadata, smoke_id: str, path: Path) -> dict | None:
    """Validate a committed deletion while its manifest marker remains last."""
    names = {p.name for p in entries(path, 3)}
    if not names:
        return None  # Crash after deleting the final member but before rmdir.
    if "manifest.json" not in names:
        raise Denied("Committed smoke quarantine lacks its commit marker")
    manifest = _manifest(metadata, smoke_id, path)
    allowed = {"manifest.json"}
    if manifest["project"] is not None:
        allowed.add("project")
    if "report.json" in names:
        allowed.add("report.json")
        _report(metadata, smoke_id, path, manifest)
    if not names.issubset(allowed):
        raise Denied("Unknown committed smoke quarantine member")
    if "project" in names and manifest["project"] != project_identity(path / "project"):
        raise Denied("Smoke project identity mismatch")
    return manifest


def _delete_committed(data: Path, path: Path, smoke_id: str) -> None:
    """Resume only a validated commit; retain manifest.json until last."""
    metadata = Metadata(data)
    read_committed(metadata, smoke_id, path)
    names = {p.name for p in entries(path, 3)}
    if "project" in names:
        shutil.rmtree(path / "project")
        sync_dir(path)
    if "report.json" in names:
        (path / "report.json").unlink()
        sync_dir(path)
    if "manifest.json" in names:
        (path / "manifest.json").unlink()
        sync_dir(path)
    path.rmdir()
    sync_dir(path.parent)


def _remove_unpublished_stage(data: Path, stage: Path, staging: Path, project: Path | None,
                             project_identity_before: dict | None, project_moved: bool,
                             expected_files: dict[str, bytes]) -> None:
    """Restore this call's moved project and remove only its known stage files."""
    try:
        stage.lstat()
    except FileNotFoundError:
        if project_moved:
            raise Denied("Smoke staging disappeared before project restore") from None
        return
    directory(stage)
    names = {path.name for path in entries(stage, 4)}
    if project_moved:
        member = stage / "project"
        if (project is None or project_identity_before is None or "project" not in names
                or project_identity(member) != project_identity_before):
            raise Denied("Moved smoke project identity changed before restore")
        try:
            project.lstat()
        except FileNotFoundError:
            pass
        else:
            raise Denied("Smoke project restore destination already exists")
        os.rename(member, project)
        sync_dir(stage)
        sync_dir(project.parent)
        names.remove("project")
    elif "project" in names:
        raise Denied("Unexpected smoke project in staging")
    if not names.issubset(expected_files):
        raise Denied("Unknown smoke staging member")
    for name in sorted(names):
        path = stage / name
        expected = expected_files[name]
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink()
                or getattr(info, "st_reparse_tag", 0) or info.st_size != len(expected)):
            raise Denied("Unsafe smoke staging member")
        raw = safe_read(data, path.relative_to(data).as_posix(), len(expected))
        if raw != expected:
            raise Denied("Smoke staging member changed before cleanup")
        path.unlink()
        sync_dir(stage)
    stage.rmdir()
    sync_dir(staging)


def publish(data: Path, smoke_id: str, report: dict, project: Path | None) -> dict:
    """Stage complete evidence, then publish with exactly one directory rename.

    Provider execution is complete before this call. Staging creation, owner
    project movement, and pair publication share the global retention lock, so
    contention cannot leave a partial stage and a reference scan sees either
    the owner project or the completed bundle.
    """
    if not SMOKE_ID.fullmatch(smoke_id):
        raise Denied("Invalid smoke pair ID")
    if (not isinstance(report, dict) or report.get("status") not in ("pass", "fail")):
        raise Denied("Smoke report needs a terminal pass/fail status")
    timestamp(report.get("time"))
    directory(data)
    if project is not None:
        project = Path(project)
    destination = data / "validation" / "pairs" / smoke_id
    result = {**report, "smoke_id": smoke_id, "pair": str(destination),
              "evidence": str(destination / "project") if project else None,
              "report": str(destination / "report.json")}
    report_bytes = canonical(result)
    if len(report_bytes) > 16 * 1024 * 1024:
        raise LimitExceeded("Smoke report size bound exceeded")
    with shared_lock(data):
        validation = data / "validation"
        validation.mkdir(exist_ok=True, mode=0o700)
        directory(validation)
        staging = validation / "staging"
        staging.mkdir(exist_ok=True, mode=0o700)
        directory(staging)
        pairs = validation / "pairs"
        pairs.mkdir(exist_ok=True, mode=0o700)
        directory(pairs)
        sync_dir(validation)
        destination = pairs / smoke_id
        if destination.exists() or destination.is_symlink():
            raise Denied("Smoke pair already exists")
        stage = staging / smoke_id
        try:
            stage.lstat()
        except FileNotFoundError:
            pass
        else:
            raise Denied("Smoke staging ID already exists")
        project_identity_before = None
        if project is not None:
            projects = data / "projects"
            directory(projects)
            if project.parent != projects:
                raise Denied("Smoke project must be a direct child of the shared projects directory")
            project_identity_before = project_identity(project)
        expected_files = {"report.json": report_bytes}
        stage_created = False
        project_moved = False
        published = False
        try:
            try:
                stage.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise Denied("Smoke staging ID already exists") from exc
            stage_created = True
            write_json(stage / "report.json", result)
            raw_report = safe_read(data, (stage / "report.json").relative_to(data).as_posix(),
                                   16 * 1024 * 1024)
            manifest = {"version": 1, "smoke_id": smoke_id, "status": result["status"],
                        "completed_at": result["time"],
                        "report": {"sha256": digest(raw_report), "bytes": len(raw_report)},
                        "project": project_identity_before}
            manifest_bytes = canonical(manifest)
            expected_files["manifest.json"] = manifest_bytes
            write_json(stage / "manifest.json", manifest)
            sync_dir(stage)
            if project is not None:
                os.rename(project, stage / "project")
                project_moved = True
                sync_dir(project.parent)
            read_pair(Metadata(data), smoke_id, path=stage)
            os.rename(stage, destination)
            published = True
            sync_dir(staging)
            sync_dir(pairs)
        finally:
            if stage_created and not published:
                _remove_unpublished_stage(data, stage, staging, project, project_identity_before,
                                          project_moved, expected_files)
    return result


def _preflight(metadata, root, names):
    """Bound enumeration before store replay (including permanent seed history)."""
    for name in names:
        base = root / name
        for path in entries(base, MAX_ENTRIES, optional=True):
            metadata.entries += 1
            if name != "work-items" and (path.suffix != ".json" or not re.fullmatch(r"[A-Za-z0-9_.-]+", path.stem)):
                raise Denied("Unknown smoke reference store entry")
            if name == "work-items":
                directory(path)
                for child in entries(path, 260):
                    metadata.read(child)
            else:
                record = metadata.read(path)
                if name == "decisions":
                    from .work_items import WorkItems
                    WorkItems._validate_current_decision_shape(record, path.stem)
                if name == "waits":
                    from .work_item_waits import stored_wait
                    fields = {"owner", "rev", "decision", "registered_at", "run"}
                    if (not fields.issubset(record) or record["owner"] != path.stem
                            or type(record["rev"]) is not int or record["rev"] < 1
                            or not isinstance(record["decision"], dict)
                            or record["decision"].get("id") != record["owner"]
                            or record["decision"].get("rev") != record["rev"]
                            or record["decision"].get("action") != "defer"):
                        raise Denied("Malformed smoke wait owner")
                    timestamp(record["registered_at"])
                    if "registration_id" in record:
                        from .insights import _wait_registration_id
                        if _wait_registration_id(record) is None:
                            raise Denied("Malformed wait registration identity")
                        fields.add("registration_id")
                    stored_wait({key: value for key, value in record.items() if key not in fields})
            if metadata.entries + metadata.count > MAX_ENTRIES:
                raise LimitExceeded("Smoke reference store entry bound exceeded")


def collect_refs(metadata: Metadata, pairs: dict, *, roots=None) -> set:
    from .insights import (Insights, _effective_decision, _rev_of, MAX_EVIDENCE_ENTRIES,
                           MAX_EVIDENCE_CHARS, INSIGHT_METADATA_DEFAULTS, insight_metadata)
    from .work_items import WorkItems
    explicit_roots = roots is not None
    roots = entries(metadata.data / "projects", MAX_PROJECTS, optional=True) if roots is None else roots
    pinned = set()
    ref_count = 0

    def add(refs):
        nonlocal ref_count
        validate_refs(refs)
        ref_count += len(refs)
        if ref_count > MAX_REFS:
            raise LimitExceeded("Smoke live reference bound exceeded")
        if any(ref not in pairs for ref in refs):
            raise Denied("Live smoke reference has no complete pair")
        pinned.update(refs)

    class ReferenceItems(WorkItems):
        def _read(self, name):
            return metadata.read(self.root / name)

        def _retained_verification(self, verification, code_digest, scope_digest):
            if isinstance(verification, dict) and isinstance(verification.get("receipt_id"), str):
                metadata.read(self.root / "evidence/acceptance/objects" / f"{verification['receipt_id']}.json")
            return super()._retained_verification(verification, code_digest, scope_digest)

    from .snapshot import Snapshots

    class ReferenceSnapshots(Snapshots):
        def get(self, snapshot_id):
            return self.validate_manifest(snapshot_id, metadata.read(self.root / "snapshots" / f"{snapshot_id}.json"))

    for root in roots:
        if not explicit_roots:
            identifier(root.name)
        directory(root)
        _preflight(metadata, root, ("inbox", "insight-ids", "decisions", "waits", "decision-history", "work-items"))
        for path in entries(root / "insight-ids", MAX_ENTRIES, optional=True):
            identity = metadata.read(path)
            if (set(identity) != {"id", "sha256", "created_at"} or identity["id"] != path.stem
                    or not isinstance(identity["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", identity["sha256"])):
                raise Denied("Malformed Insight identity")
            identifier(identity["id"])
            timestamp(identity["created_at"])
            if not (root / "inbox" / path.name).exists():
                decision = metadata.read(root / "decisions" / path.name)
                if decision.get("action") not in ("accept", "modify", "reject", "withdraw"):
                    raise Denied("Current Insight identity has no retained owner")
        insights = Insights(root)
        snapshots = ReferenceSnapshots(root, excludes=(), max_file=2**63-1,
                                       max_bytes=2**63-1, max_files=MAX_ENTRIES, create_dirs=False)
        store = ReferenceItems(root, snapshots, insights)
        for path in entries(root / "inbox", MAX_ENTRIES, optional=True):
            record = metadata.read(path)
            if (set(record) - {"id", "source", "title", "body", "base_snapshot", "run", "origin", "created_at", "updated_at", "rev", "evidence", *INSIGHT_METADATA_DEFAULTS}
                    or path.suffix != ".json" or record.get("id") != path.stem
                    or type(record.get("rev", 1)) is not int or record.get("rev", 1) < 1
                    or not isinstance(record.get("evidence", []), list)
                    or len(record.get("evidence", [])) > MAX_EVIDENCE_ENTRIES
                    or not all(isinstance(record.get(k), str) for k in ("title", "body", "source"))):
                raise Denied("Malformed current Insight reference record")
            identifier(record["id"])
            insight_metadata(record)
            rev = _rev_of(record)
            decision_path = root / "decisions" / path.name
            decision = metadata.read(decision_path) if decision_path.exists() else None
            if decision is not None:
                WorkItems._validate_current_decision_shape(decision, record["id"])
                if (decision.get("id") != record["id"] or decision.get("action") not in
                        ("accept", "modify", "reject", "withdraw", "defer")
                        or type(decision.get("rev", 1)) is not int):
                    raise Denied("Malformed current Insight decision")
            effective = _effective_decision(rev, decision)
            active = effective is None or effective["action"] == "defer"
            for entry in record.get("evidence", []):
                if (not isinstance(entry, dict) or set(entry) - {"smoke_refs", "rev"} != {"author", "run", "created_at", "text"}
                        or ("rev" in entry and "smoke_refs" not in entry)
                        or (entry.get("run") is not None and not isinstance(entry["run"], str))
                        or not isinstance(entry.get("text"), str) or not entry["text"].strip()
                        or len(entry["text"]) > MAX_EVIDENCE_CHARS):
                    raise Denied("Malformed Insight evidence")
                identifier(entry["author"])
                timestamp(entry["created_at"])
                refs = validate_refs(entry.get("smoke_refs", []))
                if "smoke_refs" in entry and (type(entry.get("rev")) is not int or entry["rev"] != rev):
                    raise Denied("Smoke Insight evidence revision mismatch")
                if active:
                    add(refs)
            wait_path = root / "waits" / path.name
            if wait_path.exists():
                wait = metadata.read(wait_path)
                refs = validate_refs(wait.get("smoke_refs", []))
                if refs and (wait.get("owner") != record["id"]
                             or type(wait.get("rev")) is not int or wait["rev"] < 1):
                    raise Denied("Smoke wait owner/revision mismatch")
                if active and effective is not None and effective["action"] == "defer" and wait.get("rev") == rev:
                    add(refs)
        for path in entries(root / "waits", MAX_ENTRIES, optional=True):
            wait = metadata.read(path)
            if wait.get("smoke_refs") and not (root / "inbox" / path.name).exists():
                # Terminal archived waits may remain, but must have a current
                # terminal decision proving they no longer own a payload pin.
                decision = metadata.read(root / "decisions" / path.name)
                if decision.get("action") not in ("accept", "modify", "reject", "withdraw"):
                    raise Denied("Smoke wait has no current owner")
        seen = set()
        for event_id, event in store._iter_item_events(recover=False):
            kind, seed = store._seed_for_event(event_id, event, readonly=True)
            if kind != "seed" or seed["id"] in seen:
                continue
            seen.add(seed["id"])
            item = None
            retirement = {}
            for item in store._iter_chain(seed["id"], seed_event=event,
                                          history_revisions=store._history_revisions(seed["id"], recover=False),
                                          retirement=retirement):
                pass
            if item is None:
                raise Denied("Missing work-item chain")
            from .work_item_retirement import project as _retire_project
            item = _retire_project(item, retirement.get("record"))
            refs = validate_refs(item.get("smoke_refs", []))
            wait_refs = validate_refs((item.get("wait") or {}).get("smoke_refs", []))
            if item["status"] not in ("done", "dropped"):
                add(refs)
                add(wait_refs)
    return pinned


def plan(data: Path, days: int, moment: dt.datetime, recovery=(), metadata: Metadata | None = None):
    metadata = metadata or Metadata(data)
    pairs = {}
    for path in entries(data / "validation" / "pairs", MAX_PAIRS, optional=True):
        if not SMOKE_ID.fullmatch(path.name):
            raise Denied("Unknown smoke pair entry")
        pairs[path.name] = read_pair(metadata, path.name)
    published = set(pairs)
    for path, smoke_id, state in recovery:
        if state == "pending":
            pairs[smoke_id] = read_pair(metadata, smoke_id, path=path)
    if len(pairs) > MAX_PAIRS:
        raise LimitExceeded("Smoke pair bound exceeded including pending recovery")
    pinned = collect_refs(metadata, pairs)
    cutoff = moment - dt.timedelta(days=days)
    candidates = [key for key, manifest in pairs.items()
                  if key in published and key not in pinned and timestamp(manifest["completed_at"]) < cutoff]
    return {"candidates": candidates, "pinned": sorted(pinned), "metadata_bytes": metadata.bytes}


def quarantine(data: Path, metadata: Metadata | None = None):
    metadata = metadata or Metadata(data)
    directory(data / "validation" / "pairs", optional=True)
    result = []
    seen = set()
    for path in entries(data / "validation" / "quarantine", MAX_PAIRS, optional=True):
        parts = path.name.split(".")
        if len(parts) != 2 or not SMOKE_ID.fullmatch(parts[0]) or parts[1] not in ("pending", "committed"):
            raise Denied("Unknown smoke quarantine state")
        if parts[0] in seen:
            raise Denied("Contradictory smoke quarantine states")
        seen.add(parts[0])
        directory(path)
        # Pending is whole; committed keeps a validated manifest marker until
        # its final member is removed, so unknown leftovers fail closed.
        if parts[1] == "pending":
            read_pair(metadata, parts[0], path=path)
        else:
            read_committed(metadata, parts[0], path)
        target = data / "validation" / "pairs" / parts[0]
        if target.exists() or target.is_symlink():
            raise Denied("Quarantine conflicts with published pair")
        result.append((path, parts[0], parts[1]))
    return result


def audit_rows(data: Path, metadata: Metadata | None = None):
    metadata = metadata or Metadata(data)
    path = data / "validation" / "retention-audit.json"
    try:
        path.lstat()
    except FileNotFoundError:
        rows = []
    else:
        record = metadata.read(path, 65536)
        rows = record.get("events")
        if set(record) != {"version", "events"} or type(record["version"]) is not int or record["version"] != 1 or not isinstance(rows, list) or len(rows) > AUDIT_LIMIT:
            raise Denied("Malformed smoke maintenance audit")
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"smoke_id", "action", "at"}
                or not isinstance(row["smoke_id"], str) or not SMOKE_ID.fullmatch(row["smoke_id"])
                or row["action"] not in ("rollback", "recovered-deletion", "deleted")):
            raise Denied("Malformed smoke maintenance audit entry")
        timestamp(row["at"])
    return rows


def audit(data: Path, smoke_id: str, action: str):
    rows = audit_rows(data)
    path = data / "validation" / "retention-audit.json"
    write_json(path, {"version": 1, "events": (rows + [{"smoke_id": smoke_id, "action": action, "at": now()}])[-AUDIT_LIMIT:]})


def run(data: Path, *, apply=False, moment=None) -> dict:
    moment = moment or dt.datetime.now(dt.timezone.utc)
    timestamp(moment.isoformat())
    # Preview is advisory and strictly read-only, including lock inodes.
    # Apply always recomputes the complete plan under the global lock.
    with shared_lock(data) if apply else contextlib.nullcontext():
        return _run_locked(data, apply=apply, moment=moment)


def _run_locked(data: Path, *, apply: bool, moment: dt.datetime) -> dict:
    """Shared implementation; applying callers must already hold shared_lock."""
    metadata = Metadata(data)
    days = policy(data, metadata)
    recovery = quarantine(data, metadata)
    result = {"days": days, "apply": apply, "candidates": [], "pinned": [],
              "recovery": [p.name for p, _, _ in recovery], "retired": [], "recovered": []}
    if days:
        result.update(plan(data, days, moment, recovery, metadata))
    audit_rows(data, metadata)
    if not apply:
        return bounded_result(result)
    # Pending pairs participate in the scan above but never in this
    # invocation's expiry candidates. Recovery does not bypass bad refs.
    try:
        for path, smoke_id, state in recovery:
            if state == "pending":
                os.rename(path, data / "validation" / "pairs" / smoke_id)
                sync_dir(path.parent)
                sync_dir(data / "validation" / "pairs")
                action = "rollback"
            else:
                _delete_committed(data, path, smoke_id)
                action = "recovered-deletion"
            result["recovered"].append(smoke_id)
            audit(data, smoke_id, action)
        for smoke_id in result["candidates"]:
            source = data / "validation" / "pairs" / smoke_id
            qroot = data / "validation" / "quarantine"
            qroot.mkdir(exist_ok=True, mode=0o700)
            directory(qroot)
            pending = qroot / f"{smoke_id}.pending"
            committed = qroot / f"{smoke_id}.committed"
            os.rename(source, pending)
            sync_dir(source.parent)
            sync_dir(qroot)
            os.rename(pending, committed)  # irreversible deletion commit
            sync_dir(qroot)
            _delete_committed(data, committed, smoke_id)
            result["retired"].append(smoke_id)
            audit(data, smoke_id, "deleted")
    except (OSError, Denied, LimitExceeded) as exc:
        result.update(complete=False, error=type(exc).__name__, failed_id=smoke_id)
        return bounded_result(result)
    result["complete"] = True
    return bounded_result(result)

def bounded_result(result):
    """Plans remain complete internally; CLI samples never exceed 100 IDs/list."""
    for field in ("candidates", "pinned", "recovery", "retired", "recovered"):
        values = result[field]
        result[field + "_total"] = len(values)
        result[field + "_digest"] = digest(canonical(values))
        result[field] = values[:100]
    return result


#: Default automatic shared-smoke cadence (seconds) when configuration
#: omits the policy; the validated ``[limits]`` value normally applies.
AUTOMATIC_SMOKE_RETENTION_INTERVAL_DEFAULT = 86400
#: Maximum automatic shared-smoke cadence (31 days, mirroring run retention).
AUTOMATIC_SMOKE_RETENTION_INTERVAL_MAX = 2678400
#: Bound on the automatic shared-smoke due-state file; larger state is unavailable.
_AUTOMATIC_SMOKE_RETENTION_STATE_BYTES = 4096


def _automatic_smoke_due(data: Path, instant: int, interval: int) -> bool:
    """Bounded, no-follow due-state read; malformed state refuses the sweep."""
    parent = data / "maintenance"
    if not directory(parent, optional=True):
        return True
    path = parent / "automatic-smoke-retention.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    record = Metadata(data).read(path, _AUTOMATIC_SMOKE_RETENTION_STATE_BYTES)
    valid = (type(record.get("checked_at")) is int
             and 0 <= record["checked_at"] <= instant)
    if record.get("status") == "completed":
        valid = (valid and set(record) == {"checked_at", "status", "days", "retired_total", "recovered_total"}
                 and type(record.get("days")) is int and 0 <= record["days"] <= MAX_DAYS
                 and all(type(record.get(k)) is int and 0 <= record[k] <= MAX_PAIRS
                         for k in ("retired_total", "recovered_total")))
    elif record.get("status") == "refused":
        valid = (valid and set(record) == {"checked_at", "status", "error"}
                 and isinstance(record.get("error"), str)
                 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", record["error"]) is not None)
    else:
        valid = False
    if not valid:
        raise Denied("Invalid automatic smoke retention record")
    return instant - record["checked_at"] >= interval


def maintain_automatic_smoke_retention(project) -> dict:
    """Best-effort global cadence; call before any project execution locks.

    The existing nonblocking shared lock serializes both cadence and sweep,
    including reference writers and manual apply. No second lock/order exists.
    Busy does not consume the cadence. Only a due attempt writes a bounded
    result; refused plans consume the interval to avoid repeated costly scans.
    Unsafe state refuses before expiry. Unexpected failures also stay local to
    maintenance, never swallowing process cancellation (BaseException).
    """
    try:
        interval = getattr(project.config.limits, "automatic_smoke_retention_interval_seconds",
                           AUTOMATIC_SMOKE_RETENTION_INTERVAL_DEFAULT)
        if type(interval) is not int or not 0 <= interval <= AUTOMATIC_SMOKE_RETENTION_INTERVAL_MAX:
            return {"status": "unavailable"}
        if interval == 0:
            return {"status": "disabled"}
        data = Path(project.config.data)
        directory(data)  # Validate before any path-following lock/directory creation.
        instant = int(time.time())
        if not _automatic_smoke_due(data, instant, interval):
            return {"status": "not-due"}
        with shared_lock(data):
            # Another project may have run since the fast check.
            instant = int(time.time())
            if not _automatic_smoke_due(data, instant, interval):
                return {"status": "not-due"}
            parent = data / "maintenance"
            parent.mkdir(exist_ok=True, mode=0o700)
            directory(parent)
            result = {"checked_at": instant, "status": "refused"}
            try:
                outcome = _run_locked(data, apply=True, moment=dt.datetime.now(dt.timezone.utc))
                if outcome.get("complete") is True:
                    result.update(status="completed", days=outcome["days"],
                                  retired_total=outcome["retired_total"],
                                  recovered_total=outcome["recovered_total"])
                else:
                    result["error"] = outcome["error"]
            except Busy:
                return {"status": "busy"}
            except Exception as exc:
                result["error"] = type(exc).__name__[:64]
            # Never follow linked parents/targets, including swaps after the due read.
            safe_write(data, "maintenance/automatic-smoke-retention.json", canonical(result))
            return result
    except Busy:
        return {"status": "busy"}
    except Exception:
        return {"status": "unavailable"}


def validate_restored_refs(data: Path, root: Path):
    """Caller holds the global lock through publication of the restored root."""
    metadata = Metadata(data)
    _preflight(metadata, root, ("inbox", "insight-ids", "decisions", "waits", "decision-history", "work-items"))
    # Importing no smoke references must not change legacy restore semantics
    # (notably identity-only tombstones whose bodies predate archival). Such
    # incomplete state still refuses any later global expiry scan.
    if not metadata.has_refs:
        return
    pairs = {path.name: read_pair(metadata, path.name)
             for path in entries(data / "validation/pairs", MAX_PAIRS, optional=True)}
    collect_refs(metadata, pairs, roots=[root])
