"""Static artifact publication: a Markdown document beside runtime evidence.

The mechanism stores one UTF-8 document plus runtime-derived evidence under a
content-addressed ID and moves an atomic latest pointer. It never renders
HTML: presentation is operator policy (see examples/render-paper.py).
"""
from __future__ import annotations

from .fs import atomic_write, canonical, digest, mkdir, now, publish_pointer, write_json, read_json, lock


def render(snapshot: dict, document: dict | None) -> str:
    """Render the staged document verbatim with a title heading.

    Schema/bounds: ``{"title" (<=200), "body" (<=48000)}``. Trust: title/body
    are model/operator prose (data). Retry: pure text. Evidence: snapshot
    provenance lives in ``evidence.json`` and the artifact digest, not in
    Markdown prose; presentation (headers, language, order) lives in
    ``examples/render-paper.py``. Failure: never invents prose beyond the
    mechanical fallback.
    """
    if document is None:
        # Mechanical fallback only: no invented prose, all words are record data.
        document = {"title": "Snapshot " + snapshot["id"][:12],
                    "body": snapshot["state"] + "\n\n" + snapshot["summary"]}
    return "# " + document["title"] + "\n\n" + document["body"] + "\n"


def publish(project, snapshot: dict, document: dict | None = None, *, run_id: str | None = None) -> dict:
    evidence = {"snapshot": snapshot["id"], "code_digest": snapshot["code_digest"],
                "source_run": snapshot["run"], "run": run_id,
                "verification": snapshot.get("verification"), "skipped_paths": snapshot["skipped"],
                "published_at": now()}
    markdown = render(snapshot, document)
    artifact_id = digest(canonical({"snapshot": snapshot["id"], "document": document, "run": run_id}))
    root = project.root / "artifacts"
    entry = root / artifact_id
    with lock(root / ".publish.lock"):
        mkdir(entry)
        previous = read_json(entry / "evidence.json")
        if previous is not None:
            evidence = {**evidence, "published_at": previous["published_at"]}
            if previous != evidence:
                raise ValueError("Immutable artifact evidence conflict")
        atomic_write(entry / "artifact.md", markdown.encode(), exclusive=True)
        write_json(entry / "evidence.json", evidence, exclusive=True)
        # Re-publication time belongs to the mutable pointer only.
        publish_pointer(root, {"artifact": artifact_id, "snapshot": snapshot["id"], "published_at": now()})
    return {"artifact": artifact_id, "markdown": str(entry / "artifact.md")}
