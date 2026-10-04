"""Upstream-sync trusted argv adapter (M1 mechanism, step 2: invocation only).

Schema: operator-owned ``[vcs] command`` argv receives one JSON object on
stdin (``{\"op\": ...}``) and must print one JSON object on stdout.
Bounds: request canonical bytes <= ``max_bytes``; stdout capped at
``max_bytes`` via ``process.run``. Trust: operator-owned host program only;
never model-provided argv, never run in the sandbox. Retry/cancellation:
single invocation under ``timeout_seconds``; no shell retry. Evidence: caller
binds results to receipts/digests. Failure: ``Denied`` on unconfigured
adapter, oversize request, timeout, oversize output, nonzero exit or
malformed JSON.
"""
from __future__ import annotations

import json

from .errors import Denied
from .fs import canonical
from .process import run


def invoke(settings: dict, request: dict) -> dict:
    """Invoke the configured adapter once and return its JSON object.

    Schema: ``request`` is a JSON object with a string ``op`` field;
    the adapter must return a JSON object. Bounds: canonical request bytes
    and adapter stdout are each capped at ``max_bytes``; the call is bounded
    by ``timeout_seconds``. Trust: ``settings`` comes from operator config,
    not model output. Failure: ``Denied`` with a short reason; stderr is
    truncated to 2000 chars so adapter chatter cannot blow the frame.
    """
    command = settings.get("command", [])
    timeout = settings.get("timeout_seconds", 20)
    maximum = settings.get("max_bytes", 524288)
    if not command:
        raise Denied("Upstream sync is not configured")
    if not isinstance(request, dict) or not isinstance(request.get("op"), str) or not request["op"]:
        raise Denied("VCS adapter request must be an object with a nonempty op string")
    try:
        payload = canonical(request)
    except (TypeError, ValueError) as exc:
        raise Denied("VCS adapter request is not JSON-serializable") from exc
    if len(payload) > maximum:
        raise Denied("VCS adapter request exceeds byte limit")
    result = run(list(command), timeout=timeout, maximum=maximum,
                 input_data=payload + b"\n")
    if result.reason == "timeout":
        raise Denied("Configured VCS program timed out")
    if result.reason == "output_limit":
        raise Denied("VCS adapter response exceeds byte limit")
    if result.exit_code != 0 or result.reason != "exited":
        raise Denied(f"Configured VCS program failed ({result.reason})")
    try:
        data = json.loads(result.stdout)
    except (ValueError, RecursionError) as exc:
        raise Denied("VCS adapter must return a JSON object") from exc
    if not isinstance(data, dict):
        raise Denied("VCS adapter must return a JSON object")
    return data


def fetch_refs(settings: dict) -> dict:
    """Fetch upstream refs via ``{\"op\": \"fetch\"}`` and validate the shape.

    Schema: response ``{\"refs\": {name: sha}}`` where names are nonempty
    ``refs/remotes/upstream/``-relative paths (no newlines, max 512 chars)
    and shas are 40/64 lowercase hex. Bounds: at most 4096 refs. Trust:
    refs are external-untrusted until merged and verified. Failure: ``Denied``.
    """
    data = invoke(settings, {"op": "fetch"})
    refs = data.get("refs")
    if not isinstance(refs, dict) or len(refs) > 4096:
        raise Denied("VCS adapter must return a refs object with at most 4096 entries")
    import re
    for name, sha in refs.items():
        if not isinstance(name, str) or not name or len(name) > 512 or "\n" in name or "\x00" in name:
            raise Denied("Invalid VCS ref name")
        if name.startswith("/") or ".." in name.split("/"):
            raise Denied("Invalid VCS ref name")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", sha):
            raise Denied("Invalid VCS ref digest")
    return {"refs": dict(refs), "trust": "external-untrusted"}
