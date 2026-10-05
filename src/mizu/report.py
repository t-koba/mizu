"""Static artifact publication: a Markdown document beside runtime evidence.

The mechanism stores one UTF-8 document plus runtime-derived evidence under a
content-addressed ID and moves an atomic latest pointer. It never renders
HTML: presentation is operator policy (see examples/render-paper.py).
"""
from __future__ import annotations

from .errors import Denied, LimitExceeded
from .fs import atomic_write, canonical, digest, mkdir, now, publish_pointer, write_json, read_json, lock, safe_read, text_preview, DIGEST

#: Prompt bound (UTF-8 bytes) on the previous edition offered to `report`
#: roles. The read cap covers the staged-document schema in bytes: bodies
#: are bounded by 48000 characters (schema maxLength counts characters),
#: so 48000 x 4B worst case plus title and heading slack.
PREVIOUS_REPORT_BYTES = 8192
PREVIOUS_REPORT_READ_BYTES = 262144


def previous(project) -> dict | None:
    """Bounded body of the latest published artifact, or None.

    Schema: ``{"artifact", "snapshot", "body", "body_truncated"}``. Bounds:
    body is a UTF-8 prefix of at most ``PREVIOUS_REPORT_BYTES``. Trust:
    recorded local state only. Retry/cancellation: pure projection over
    bounded reads. Failure: never raises; a missing, corrupt, or unreadable
    artifact reads as no previous edition (context, not authority).
    """
    try:
        pointer = read_json(project.root / "artifacts" / "latest.json", {})
        if not isinstance(pointer, dict):
            return None
        artifact = pointer.get("artifact")
        if not isinstance(artifact, str) or not DIGEST.fullmatch(artifact):
            return None
        raw = safe_read(project.root, "artifacts/" + artifact + "/artifact.md",
                        PREVIOUS_REPORT_READ_BYTES)
    except (OSError, ValueError, Denied, LimitExceeded):
        return None
    body, truncated = text_preview(raw.decode("utf-8", "replace"), PREVIOUS_REPORT_BYTES)
    snapshot = pointer.get("snapshot")
    return {"artifact": artifact,
            "snapshot": snapshot if isinstance(snapshot, str) else None,
            "body": body, "body_truncated": truncated}


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
