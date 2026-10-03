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
        write_json(stage / "changes.json", project.snapshots.changes(snapshot["id"]))
        write_json(stage / "history.json", [{k: item[k] for k in ("id", "created_at", "summary", "outcome", "code_digest")}
                                           for item in project.snapshots.history(snapshot["id"])])
        atomic_write(stage / "PROJECT.md", snapshot["goal"].encode())
        atomic_write(stage / "STATE.md", snapshot["state"].encode())
        os.rename(stage, destination)
        return {"bundle": str(destination), "snapshot": snapshot["id"], "created_at": snapshot["created_at"]}
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
            return {k: self.snapshot[k] for k in ("id", "created_at", "code_digest", "goal", "state", "outcome", "summary", "verification")}
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
