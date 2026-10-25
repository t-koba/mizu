"""Exact retained evidence reads and durable per-invocation evidence records."""
from __future__ import annotations

import json
import re
import uuid

from .errors import Denied
from .fs import (DIGEST, READ_PAGE_CHARS, digest, safe_read, sync_dir,
                write_json)

#: Exact retained-evidence locators, never prefixes or filesystem paths.
EVIDENCE_UUID = re.compile(r"[0-9a-f]{32}\Z")
MAX_STARTED_EVIDENCE_BYTES = 65536


def _reject_nonfinite_json(value: str):
    raise ValueError("Non-finite JSON value")


def read_retained(context, kind: str, args: dict) -> dict:
    """Read exactly two bounded files; no discovery, fallback or writes.

    started.json is capped at min(file_bytes, 64 KiB); the complete evidence
    record uses file_bytes. Identity is harness-owned run metadata plus the
    configured caller role; record contents remain untrusted. Every page
    revalidates both files.
    Unsafe/unavailable/corrupt records fail without a retry or history scan.
    """
    run, evidence_id = args["run"], args["evidence_id"]
    if any(not isinstance(value, str) or not EVIDENCE_UUID.fullmatch(value)
           for value in (run, evidence_id)):
        raise Denied("Evidence requires full lowercase 32-hex run and evidence UUIDs")
    offset, limit = args.get("offset", 0), args.get("limit", READ_PAGE_CHARS)
    expected = args.get("content_id")
    if offset and expected is None:
        raise Denied("Evidence continuation requires content_id")
    if expected is not None and (not isinstance(expected, str) or not DIGEST.fullmatch(expected)):
        raise Denied("Invalid evidence content_id")
    prefix = f"runs/{run}/"
    evidence = f"{kind}/{evidence_id}.json"
    maximum = context.config.limits.file_bytes
    started_maximum = min(maximum, MAX_STARTED_EVIDENCE_BYTES)
    try:
        started = json.loads(safe_read(context.project.root, prefix + "started.json", started_maximum)
                             .decode("utf-8"), parse_constant=_reject_nonfinite_json)
        if (not isinstance(started, dict) or started.get("run") != run
                or started.get("role") != context.role.name):
            raise Denied("Evidence run identity or role mismatch")
        data = safe_read(context.project.root, prefix + evidence, maximum)
        text = data.decode("utf-8")
        record = json.loads(text, parse_constant=_reject_nonfinite_json)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise Denied("Corrupt retained evidence or run metadata") from exc
    command = record.get("command") if kind == "experiments" and isinstance(record, dict) else record
    expected_kind = "experiment" if kind == "experiments" else "command"
    if (not isinstance(command, dict) or command.get("id") != evidence_id
            or command.get("kind") != expected_kind or command.get("role") != context.role.name):
        raise Denied("Evidence record identity, kind or role mismatch")
    content_id = digest(data)
    if expected is not None and expected != content_id:
        raise Denied("Evidence changed since the previous page")
    end = min(len(text), offset + limit)
    return {"source": "retained-" + expected_kind + "-evidence", "trust": "recorded-local-untrusted",
            "run": run, "evidence_id": evidence_id, "evidence": evidence,
            "text": text[offset:end], "content_id": content_id, "offset": offset,
            "next_offset": end if end < len(text) else None, "truncated": end < len(text)}


def new_invocation(kind: str) -> tuple[str, str]:
    """Return a fresh per-invocation adapter evidence identity.

    Schema: ``kind`` names the evidence directory (``vcs-read``,
    ``vcs-publish``, ``vcs-retire``, ``vcs-dispose``, ``materialize``);
    the path is ``<kind>/<uuid>.json``. Returns ``(evidence_id,
    relative_path)``. Mutation callers persist an intent before dispatch and
    update that same path with the observation; other callers write once.
    Every invocation gets a fresh id that no later call can overwrite. Write
    errors propagate.
    """
    evidence_id = uuid.uuid4().hex
    return evidence_id, f"{kind}/{evidence_id}.json"


def mutation(context, kind: str, op: str, parameters: dict):
    """Build dispatch/observation callbacks for one VCS mutation.

    Only bounded, validated non-secret pins/approval identities belong in
    ``parameters``, never adapter argv, credentials or approval bodies. The
    dispatch guard allocates and durably writes an outcome-unknown intent;
    failure prevents dispatch. A validated observation retains the intent and
    identity at the same path. An interrupted/failed adapter call leaves the
    intent alone; nothing here retries or reconciles it.
    """
    pending = None

    def before_invoke():
        nonlocal pending
        evidence_id, evidence = context._new_invocation_evidence(kind)
        pending = {"evidence_id": evidence_id, "evidence": evidence,
                   "outcome": "unknown",
                   "intent": {"op": op, "parameters": parameters}}
        write_json(context.run_dir / evidence, pending, exclusive=True)
        # Persist the evidence-directory entry as well as the file itself.
        sync_dir(context.run_dir)

    def observe(record):
        record = {**record, **pending, "outcome": "observed"}
        path = context.run_dir / pending["evidence"]
        try:
            write_json(path, record)
        except BaseException:
            # Atomic writes normally leave the intent untouched. Restore it
            # also if a late failure (e.g. directory fsync) followed rename.
            # This is evidence repair only, never another adapter attempt.
            write_json(path, pending)
            raise
        return record

    return before_invoke, observe
