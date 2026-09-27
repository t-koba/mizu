"""Static artifact publication: a Markdown document beside runtime evidence.

The mechanism stores one UTF-8 document plus runtime-derived evidence under a
content-addressed ID and moves an atomic latest pointer. It never renders
HTML: presentation is operator policy (see examples/render-paper.py).
"""
from __future__ import annotations

from .fs import atomic_write, canonical, digest, mkdir, now, publish_pointer, write_json


def render(snapshot: dict, document: dict | None) -> str:
    if document is None:
        # Mechanical fallback only: no invented prose, all words are record data.
        document = {"title": "Snapshot " + snapshot["id"][:12],
                    "body": snapshot["state"] + "\n\n" + snapshot["summary"]}
    return "# " + document["title"] + "\n\nSnapshot: `" + snapshot["id"] + "`  \nAs of: " + snapshot["created_at"] + "\n\n" + document["body"] + "\n"


def publish(project, snapshot: dict, document: dict | None = None, *, run_id: str | None = None) -> dict:
    evidence = {"snapshot": snapshot["id"], "code_digest": snapshot["code_digest"],
                "source_run": snapshot["run"], "run": run_id,
                "verification": snapshot.get("verification"), "skipped_paths": snapshot["skipped"],
                "published_at": now()}
    markdown = render(snapshot, document)
    artifact_id = digest(canonical({"snapshot": snapshot["id"], "document": document, "run": run_id}))
    root = project.root / "artifacts"
    entry = root / artifact_id
    mkdir(entry)
    atomic_write(entry / "artifact.md", markdown.encode())
    write_json(entry / "evidence.json", evidence)
    # Entry first, pointer last (see fs.publish_pointer).
    publish_pointer(root, {"artifact": artifact_id, "snapshot": snapshot["id"], "published_at": evidence["published_at"]})
    return {"artifact": artifact_id, "markdown": str(entry / "artifact.md")}
