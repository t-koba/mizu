"""Host-only conversion of superseded project formats during promotion."""
from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import stat
import tarfile
import time
from pathlib import Path

from . import record_migration, storage, work_item_migration
from .errors import Denied, MizuError
from .fs import digest, mkdir, sync_dir, write_json
from .project import Project


_FAILURES = (MizuError, OSError, ValueError, EOFError, tarfile.TarError, sqlite3.Error)
_COUNTS = ("entries_examined", "candidates", "converted", "already_current", "renamed",
           "resolved_open", "resolved_drop", "explicit_no_work", "unresolved", "ambiguous_or_corrupt")


def _directory_mode(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise Denied(f"Unsafe migration directory: {path}")
    return stat.S_ISDIR(info.st_mode)


def managed_projects(config) -> list[Path]:
    """Use the same project inventory for stop checks and conversion."""
    directory = config.data / "projects"
    try:
        is_directory = _directory_mode(directory)
    except FileNotFoundError:
        return []
    if not is_directory:
        raise Denied(f"Not a project directory: {directory}")
    return sorted(path for path in directory.iterdir()
                  if not path.name.startswith(".") and _directory_mode(path))


@contextlib.contextmanager
def _step(project, archive, commands, stage):
    try:
        yield
    except _FAILURES as exc:
        previews = "; ".join(f"mizu {command} {project}" for command in commands)
        raise Denied(f"Install migration for {project}: {stage} failed: {exc}. "
                     f"Inspect with {previews}; archive: {archive} (check its sibling receipt). "
                     "Keep projects stopped until repaired or restored.") from exc


def _summary(report):
    if not isinstance(report, dict):
        raise Denied("Invalid migration report")
    return {**{key: report[key] for key in (*_COUNTS, "ok", "scan_complete", "truncated") if key in report},
            **{key: len(report[key]) for key in ("conflicts", "errors") if key in report}}


def _require_complete(report, summary):
    if summary.get("ok") is not True or summary.get("truncated") or summary.get("scan_complete") is False:
        action = report.get("action", "Inspect the dry-run report for details.")
        raise Denied(f"Migration report is not OK: {summary}. {action}")


def convert(config, projects: list[Path], release: Path) -> list[dict]:
    """Preflight and back up all affected projects before applying any migration.

    Called only under the installer's stopped-project exclusion. Each archive
    and receipt persists outside the project, including on a failed promotion.
    """
    if not projects:
        return []
    operations = {
        "migrate-records": (record_migration.flat_records_present, record_migration.plan, record_migration.apply),
        "migrate-work-items": (work_item_migration.legacy_resolutions_present,
                               work_item_migration.plan, work_item_migration.apply),
    }
    root = config.data / "migration-backups"
    attempt = root / f"{digest(os.fsencode(release))[:16]}-{time.time_ns()}"
    pending = []
    for path in projects:
        archive = attempt / f"{path.name}.tar.gz"
        with _step(path.name, archive, operations, "open project for scan"):
            project = Project(config, path.name, read_only=True)
        found = {}
        for command, (present, plan, _) in operations.items():
            with _step(project.name, archive, [command], "preflight scan (no backup created)"):
                if present(project):
                    report = plan(project)
                    summary = _summary(report)
                    _require_complete(report, summary)
                    found[command] = dict(summary, status="pending")
        if found:
            row = dict(project=project.name, release=str(release), archive=str(archive),
                       receipt=str(archive.with_suffix(".json")), status="pending", conversions=found)
            pending.append((project, archive, row))

    for index, (project, archive, row) in enumerate(pending):
        with _step(project.name, archive, row["conversions"], "verified backup and receipt"):
            if index == 0:
                mkdir(root)
                if not _directory_mode(root):
                    raise Denied("Invalid migration backup directory")
                sync_dir(root.parent)
                attempt.mkdir(mode=0o700)  # A collision must not reuse an earlier attempt.
                sync_dir(root)
            backup = storage.backup(project, archive, verify=True)
            if (not isinstance(backup, dict) or backup.get("ok", True) is not True or backup.get("truncated")
                    or backup.get("scan_complete") is False
                    or backup.get("archive") != str(archive.resolve())
                    or type(backup.get("members")) is not int or backup["members"] < 1):
                raise Denied("Backup did not report a verified archive")
            storage.validate_backup_archive(archive)
            checkpoint = backup.get("checkpoint")
            if not isinstance(checkpoint, str) or project.snapshots.get(checkpoint).get("skipped"):
                raise Denied("Backup checkpoint is missing or omitted unsupported workspace files")
            with archive.open("rb") as stream:
                sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
            row.update(status="backed_up", sha256=sha256, members=backup["members"], checkpoint=checkpoint)
            write_json(Path(row["receipt"]), row, exclusive=True)

    for project, archive, row in pending:
        receipt = Path(row["receipt"])
        for command, conversion in row["conversions"].items():
            with _step(project.name, archive, [command], "conversion"):
                row["status"] = conversion["status"] = "applying"
                write_json(receipt, row)
                try:
                    present, _, apply = operations[command]
                    report = apply(project)
                    summary = _summary(report)
                    conversion.update(summary)
                    _require_complete(report, summary)
                    if present(project):
                        raise Denied("Superseded records remain after apply")
                except _FAILURES as exc:
                    row["status"] = "failed"
                    conversion.update(status="failed", ok=False, failure=str(exc))
                    write_json(receipt, row)
                    raise
                conversion["status"] = "converted"
                write_json(receipt, row)
        with _step(project.name, archive, row["conversions"], "record conversion completion"):
            row["status"] = "converted"
            write_json(receipt, row)
    return [row for _, _, row in pending]
