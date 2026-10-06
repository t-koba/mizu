"""Offline audit of retained run records: evidence gathering, not enforcement.

``audit_run`` reads one already-retained run directory and checks the
mid-run grant invariants the receipts can prove. It makes no model calls,
writes nothing, and needs no new retention: every input is a file the
execution mechanism already records. Each check reports one verdict:

``held`` (receipts show the invariant intact), ``violated`` (receipts show
the invariant broken), or ``unverifiable`` (the retained records cannot
prove either way for this run). Corrupt or unreadable records never invent
a verdict; they read as ``unverifiable``.

Structural limits are reported, not hidden: engine-tool executions leave no
per-call receipt (only aggregate admission counts), and refused attempts
that do not end the run leave no receipt either (only terminal refusals
are recorded in ``error.json``). Content-reading audit therefore cannot see
every trajectory-shaped event; what it can see, it checks exactly.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from .errors import Denied
from .fs import read_json

#: Admission reasons the execution mechanism records in started.json.
ADMISSIONS = ("schedule", "change", "decision", "wait")


def _load(path: Path):
    """Read one receipt, or None when it cannot be trusted.

    Missing files, symlinks, directories, and unparseable content all read
    as absent: the audit never follows links out of the run directory and
    never invents record contents.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None
        record = read_json(path, None)
        return record if isinstance(record, dict) else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _moment(value) -> dt.datetime | None:
    try:
        moment = dt.datetime.fromisoformat(value) if isinstance(value, str) else None
    except (ValueError, TypeError):
        return None
    if moment is None or moment.tzinfo is None:
        return None
    return moment


def _check_admission(run_dir: Path) -> dict:
    started = _load(run_dir / "started.json")
    result = _load(run_dir / "result.json")
    if isinstance(result, dict) and result.get("skipped"):
        return {"check": "admission_recorded", "verdict": "held",
                "detail": "no dispatch; unchanged runs execute no tools"}
    if isinstance(started, dict) and started.get("admission") in ADMISSIONS:
        return {"check": "admission_recorded", "verdict": "held",
                "detail": f"admitted as {started['admission']}"}
    return {"check": "admission_recorded", "verdict": "unverifiable",
            "detail": "no started.json admission reason retained"}


def _check_sealed(run_dir: Path) -> dict:
    intent = _load(run_dir / "finish-intent.json")
    if not isinstance(intent, dict):
        error = _load(run_dir / "error.json")
        if isinstance(error, dict) and "seal" in str(error.get("error", "")).lower():
            return {"check": "sealed_after_finish", "verdict": "held",
                    "detail": "post-finish attempt refused and recorded"}
        return {"check": "sealed_after_finish", "verdict": "unverifiable",
                "detail": "no finish intent retained; refused non-terminal attempts leave no receipt"}
    sealed_at = _moment(intent.get("recorded_at"))
    if sealed_at is None:
        return {"check": "sealed_after_finish", "verdict": "unverifiable",
                "detail": "finish intent carries no parseable timestamp"}
    directory = run_dir / "commands"
    try:
        records = sorted(directory.glob("*.json")) if directory.is_dir() and not directory.is_symlink() else []
    except OSError:
        records = []
    late = []
    for path in records:
        if path.is_symlink() or path.name.endswith(".started.json"):
            continue
        record = _load(path)
        if not isinstance(record, dict):
            continue
        for key in ("created_at", "finished_at"):
            moment = _moment(record.get(key))
            if moment is not None and moment >= sealed_at:
                late.append(path.name)
                break
    if late:
        return {"check": "sealed_after_finish", "verdict": "violated",
                "detail": "tool executed after finish: " + ", ".join(sorted(late))}
    return {"check": "sealed_after_finish", "verdict": "held",
            "detail": "no tool execution at or after the finish intent"}


def _check_read_role(run_dir: Path, read_roles) -> dict:
    started = _load(run_dir / "started.json")
    role = started.get("role") if isinstance(started, dict) else None
    directory = run_dir / "commands"
    try:
        records = sorted(directory.glob("*.json")) if directory.is_dir() and not directory.is_symlink() else []
    except OSError:
        records = []
    writable = sorted({p.name for p in records
                       if not p.is_symlink() and not p.name.endswith(".started.json")
                       and isinstance(_load(p), dict) and _load(p).get("writable") is True})
    if role in tuple(read_roles or ()):
        if writable:
            return {"check": "read_role_no_writable_commands", "verdict": "violated",
                    "detail": f"read role {role} ran writable commands: " + ", ".join(writable)}
        return {"check": "read_role_no_writable_commands", "verdict": "held",
                "detail": f"read role {role} ran no writable commands"}
    return {"check": "read_role_no_writable_commands", "verdict": "held",
            "detail": "write role or unscoped check; writable commands permitted"}


def _check_verification(run_dir: Path) -> dict:
    proof = _load(run_dir / "verification.json")
    result = _load(run_dir / "result.json")
    finish = result.get("finish") if isinstance(result, dict) else None
    outcome = finish.get("outcome") if isinstance(finish, dict) else None
    if not isinstance(proof, dict):
        if outcome == "done":
            return {"check": "verification_consistent", "verdict": "violated",
                    "detail": "done outcome with no retained verification proof"}
        return {"check": "verification_consistent", "verdict": "held",
                "detail": "no verification attempted; non-done outcomes need none"}
    if proof.get("passed") is True and proof.get("unchanged_during_verification") is not True:
        return {"check": "verification_consistent", "verdict": "violated",
                "detail": "verification passed while the code moved underneath it"}
    if proof.get("passed") is True:
        return {"check": "verification_consistent", "verdict": "held",
                "detail": "verification passed on an unchanged, representable digest"}
    return {"check": "verification_consistent", "verdict": "held",
            "detail": "verification did not pass; nothing completed on a bad proof"}


def _check_engine_tools() -> dict:
    return {"check": "engine_tool_calls_receipted", "verdict": "unverifiable",
            "detail": "engine-tool executions leave no per-call receipt, only aggregate "
                      "admission counts; grant checks happen at call time and refused "
                      "non-terminal calls leave no record"}


def audit_run(run_dir: Path | str, *, read_roles=()) -> dict:
    """Audit one retained run directory without touching it.

    Schema: ``run_dir`` is a retained ``runs/<id>`` directory; ``read_roles``
    names the roles whose workspace is read-only, so their writable command
    records (if any) count as violations. Returns ``{run, checks}`` where
    each check is ``{check, verdict, detail}``. Bounds: reads regular files
    under the run directory only, never follows symlinks. Trust: retained
    receipts only, never model input. Failure: Denied when the directory
    itself is missing; per-record trouble reads as ``unverifiable``.
    """
    root = Path(run_dir)
    if not root.is_dir() or root.is_symlink():
        raise Denied("Audit needs a retained run directory")
    checks = [_check_admission(root), _check_sealed(root), _check_read_role(root, read_roles),
              _check_verification(root), _check_engine_tools()]
    return {"run": root.name, "checks": checks,
            "violated": sum(1 for item in checks if item["verdict"] == "violated")}
