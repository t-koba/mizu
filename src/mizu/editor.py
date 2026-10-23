"""A small stdio MCP server for exported, immutable Editor capsules.

This process is NOT an OS sandbox. Launch the Editor itself in a container or a
separate account with only the exported bundle and outbox mounted.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from .errors import Denied
from .fs import PREVIEW_BYTES, atomic_write, canonical, digest, mkdir, relative_parts, safe_read, write_json, page
from .project import Project
from .protocol import DEFINITIONS, MCP_VERSIONS, PAGE_FIELDS, obj, text, validate
assert MCP_VERSIONS is not None  # re-exported: single-sourced in protocol.py, framed in mcp_loop.py

#: Exported-bundle framing: whole-manifest reads vs recent-summary reads.
BUNDLE_MANIFEST_BYTES = 16 * 1024 * 1024
BUNDLE_CHANGES_BYTES = 262144

TOOLS = {
    "get_changes": ("Read the exported bounded diff and recent snapshot summaries.", obj({})),
    "get_status": ("Read the exact exported project state and its timestamp.", obj({})),
    "list_files": ("List the files in this immutable snapshot; use offset/limit for subsequent pages.", obj(PAGE_FIELDS)),
    "read_file": ("Read one relative path from the exported snapshot.", obj({"path": text(4096)}, ("path",))),
    "search_files": ("Search literal text in the exported snapshot; bounded to 50 matches.", obj({"query": text(1000)}, ("query",))),
    # Input schema shared with the runtime submit_insight; outbox files are
    # additionally framed by PREVIEW_BYTES at ingest.
    "submit_insight": ("Propose an idea to the Worker; no editing or control authority.",
                       DEFINITIONS["submit_insight"][1]),
}


def export_public_source(snapshot: dict) -> dict:
    """Derive the versioned public-source identity of an exported snapshot.

    Schema: ``{"snapshot", "code_digest", "projection_schema",
    "projection_algorithm", "projection_digest", "captured_files",
    "public_files"}``. The whole-capture ``code_digest`` keeps its exact
    meaning; the projection digest binds only the filtered public manifest
    and must never be compared as a ``code_digest``. Bounds: no filesystem
    access beyond the snapshot record. Failure: ``Denied`` on malformed
    snapshot file metadata (never inspects ignored private state).
    """
    from .distribution import public_source_identity, public_projection
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("files"), dict):
        raise Denied("Export requires snapshot file metadata")
    if not isinstance(snapshot.get("code_digest"), str):
        raise Denied("Export requires a snapshot code digest")
    try:
        identity = public_source_identity(snapshot["files"])
        public_files = len(public_projection(snapshot["files"]))
    except ValueError as exc:
        raise Denied(f"Export requires projectable snapshot files: {exc}") from exc
    return {"snapshot": snapshot.get("id"), "code_digest": snapshot["code_digest"],
            "projection_schema": identity["schema"],
            "projection_algorithm": identity["algorithm"],
            "projection_digest": identity["digest"],
            "captured_files": len(snapshot["files"]), "public_files": public_files}


def export(project: Project, destination: Path) -> dict:
    destination = destination.resolve()
    if destination.exists():
        raise Denied("Export destination already exists")
    mkdir(destination.parent)
    stage = Path(tempfile.mkdtemp(prefix=".export-", dir=destination.parent))
    try:
        snapshot = project.snapshots.get()
        project.snapshots.materialize(snapshot, stage / "code")
        write_json(stage / "snapshot.json", snapshot)
        public_source = export_public_source(snapshot)
        write_json(stage / "public_source.json", public_source)
        write_json(stage / "changes.json", project.snapshots.changes(snapshot["id"]))
        write_json(stage / "history.json", [{k: item[k] for k in ("id", "created_at", "summary", "outcome", "code_digest")}
                                           for item in project.snapshots.history(snapshot["id"])])
        atomic_write(stage / "PROJECT.md", snapshot["goal"].encode())
        atomic_write(stage / "STATE.md", snapshot["state"].encode())
        os.rename(stage, destination)
        return {"bundle": str(destination), "snapshot": snapshot["id"], "created_at": snapshot["created_at"],
                "code_digest": snapshot["code_digest"],
                "projection_schema": public_source["projection_schema"],
                "projection_algorithm": public_source["projection_algorithm"],
                "projection_digest": public_source["projection_digest"],
                "public_files": public_source["public_files"]}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


class Capsule:
    def __init__(self, bundle: Path, outbox: Path):
        self.bundle, self.outbox = bundle.resolve(), outbox.resolve()
        if self.bundle == self.outbox or self.bundle in self.outbox.parents or self.outbox in self.bundle.parents:
            raise Denied("Outbox must be separate from the immutable bundle")
        self.snapshot = json.loads(safe_read(self.bundle, "snapshot.json", BUNDLE_MANIFEST_BYTES))
        body = {k: v for k, v in self.snapshot.items() if k != "id"}
        if digest(canonical(body)) != self.snapshot.get("id"):
            raise Denied("Bundle manifest integrity failure")
        # Missing is the only legacy case. An unsafe, unreadable, oversized or
        # malformed projection must fail closed rather than being mistaken for
        # an older bundle with no projection metadata. lstat distinguishes an
        # absent entry without following a symlink; safe_read then pins/validates
        # any present entry and propagates every refusal.
        try:
            (self.bundle / "public_source.json").lstat()
        except FileNotFoundError:
            raw = None
        else:
            raw = safe_read(self.bundle, "public_source.json", BUNDLE_MANIFEST_BYTES)
        self.public_source = None
        if raw is not None:
            try:
                candidate = json.loads(raw)
            except ValueError as exc:
                raise Denied("Bundle public-source integrity failure") from exc
            if (not isinstance(candidate, dict)
                    or candidate.get("snapshot") != self.snapshot.get("id")
                    or candidate.get("code_digest") != self.snapshot.get("code_digest")
                    or not isinstance(candidate.get("projection_digest"), str)):
                raise Denied("Bundle public-source integrity failure")
            expected = export_public_source(self.snapshot)
            if (set(candidate) != set(expected)
                    or any(type(candidate[key]) is not type(value) or candidate[key] != value
                           for key, value in expected.items())):
                raise Denied("Bundle public-source integrity failure")
            self.public_source = candidate
        mkdir(self.outbox)

    def read(self, name: str) -> str:
        relative_parts(name)
        entry = self.snapshot["files"].get(name)
        if entry is None:
            raise Denied("File is not part of the exported snapshot")
        data = safe_read(self.bundle / "code", name, PREVIEW_BYTES)
        if digest(data) != entry["sha256"]:
            raise Denied("Exported file integrity failure")
        return data.decode("utf-8", "replace")

    def call(self, name: str, arguments: dict) -> dict:
        if name not in TOOLS:
            raise Denied("Unknown tool")
        validate(arguments, TOOLS[name][1])
        if name == "get_changes":
            return {"changes": json.loads(safe_read(self.bundle, "changes.json", BUNDLE_CHANGES_BYTES)),
                    "history": json.loads(safe_read(self.bundle, "history.json", BUNDLE_CHANGES_BYTES))}
        if name == "get_status":
            status = {k: self.snapshot[k] for k in ("id", "created_at", "code_digest", "goal", "state", "outcome", "summary", "verification")}
            # Legacy bundles predate the projection file and report None;
            # current bundles carry the explicit projection identity.
            status["public_source"] = self.public_source
            return status
        if name == "list_files":
            result = page(sorted(self.snapshot["files"]), arguments.get("offset", 0), arguments.get("limit", 1000))
            result["files"] = result.pop("items")
            return result
        if name == "read_file":
            return {"path": arguments["path"], "text": self.read(arguments["path"])}
        if name == "search_files":
            if not arguments["query"].strip():
                raise Denied("A nonempty literal query is required")
            hits, scanned, skipped = [], 0, []
            for path, entry in sorted(self.snapshot["files"].items()):
                if entry["bytes"] > PREVIEW_BYTES or scanned + entry["bytes"] > 8 * 1024 * 1024:
                    skipped.append(path)
                    continue
                scanned += entry["bytes"]
                for line, value in enumerate(self.read(path).splitlines(), 1):
                    if arguments["query"] in value:
                        hits.append({"path": path, "line": line, "text": value[:1000]})
                        if len(hits) == 50:
                            return {"matches": hits, "truncated": True, "skipped": skipped}
            return {"matches": hits, "truncated": bool(skipped), "skipped": skipped}
        if not arguments["title"].strip() or not arguments["body"].strip():
            raise Denied("Insight title and body must be nonempty")
        insight_id = uuid.uuid4().hex
        record = {**arguments, "base_snapshot": self.snapshot["id"]}
        if len(canonical(record)) > PREVIEW_BYTES:
            raise Denied("Insight exceeds outbox ingest byte bound")
        write_json(self.outbox / f"{insight_id}.json", record, exclusive=True)
        return {"id": insight_id, "status": "submitted_to_outbox", "base_snapshot": self.snapshot["id"]}


def serve(bundle: Path, outbox: Path, *, input_stream=None, output_stream=None) -> None:
    """Newline-delimited JSON-RPC; stdout is exclusively protocol output."""
    from .mcp_loop import serve_stdio
    capsule = Capsule(bundle, outbox)

    def list_tools() -> list[dict]:
        return [{"name": n, "description": d, "inputSchema": schema}
                for n, (d, schema) in TOOLS.items()]

    def call_tool(name, arguments: dict) -> dict:
        return capsule.call(name, arguments)

    serve_stdio(input_stream=input_stream, output_stream=output_stream,
                server_name="mizu-editor", list_tools=list_tools, call_tool=call_tool)
