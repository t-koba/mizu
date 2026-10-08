"""Generic offline-input materialization via a trusted host adapter.

Schema: operator-owned `[materialize] command` argv receives one JSON object
on stdin (`{"spec": ...}`) and must print one JSON object on stdout.
Bounds: spec at most 8192 chars; canonical request and adapter stdout each
capped at `max_bytes`; content at most `max_bytes` chars; cache identifier
at most 256 chars. Trust: operator-owned host program only; never
model-provided argv, never run in the sandbox. Results stay
external-untrusted until applied as workspace edits and verified.
Retry/cancellation: single invocation under `timeout_seconds`; no shell retry.
Evidence: caller binds results to digests/receipts.
Failure: `Denied` on unconfigured adapter, oversize request, timeout,
oversize output, nonzero exit, malformed JSON, or digest mismatch.

First use (this slice): the adapter warms host caches that are already
mounted read-only via static `[sandbox] mounts` (e.g. a cargo/npm cache
directory) and/or returns digest-bound text content (e.g. an updated
lockfile) the worker applies as an ordinary workspace edit. Dynamic
per-request mounts and image switching are explicitly out of scope:
the product never mounts arbitrary adapter paths.

Ecosystem logic (cargo/npm/rustup, registry allowlists, layer builds)
lives in the operator adapter and its policy, never here.
"""
from __future__ import annotations

import json

from .errors import Denied
from .fs import canonical, digest
from .process import run

#: Declarative request bound: a description of desired inputs, not a script.
SPEC_MAX = 8192
#: Cache identifier bound: names a warmed location for audit, never a mount.
CACHE_ID_MAX = 256


def invoke(settings: dict, spec: str) -> dict:
    """Invoke the configured adapter once and return its validated receipt.

    Schema: `spec` is a nonempty declarative description (opaque to the
    product, interpreted by the operator adapter). Response
    `{"content": str, "digest": 64 hex, "cache": str optional}` where
    `digest` must equal sha256 over `fs.canonical(content)` recomputed
    locally. `cache` names the warmed location for audit only.
    """
    command = settings.get("command", [])
    timeout = settings.get("timeout_seconds", 20)
    maximum = settings.get("max_bytes", 524288)
    if not command:
        raise Denied("Dependency materialization is not configured")
    if not isinstance(spec, str) or not spec.strip() or len(spec) > SPEC_MAX:
        raise Denied("Materialize spec must be a nonempty description of at most 8192 chars")
    if "\x00" in spec:
        raise Denied("Materialize spec must not contain NUL")
    try:
        payload = canonical({"spec": spec})
    except (TypeError, ValueError) as exc:
        raise Denied("Materialize request is not JSON-serializable") from exc
    if len(payload) > maximum:
        raise Denied("Materialize request exceeds byte limit")
    result = run(list(command), timeout=timeout, maximum=maximum,
                 input_data=payload + b"\n")
    if result.reason == "timeout":
        raise Denied("Configured materialize program timed out")
    if result.reason == "output_limit":
        raise Denied("Materialize adapter response exceeds byte limit")
    if result.exit_code != 0 or result.reason != "exited":
        raise Denied(f"Configured materialize program failed ({result.reason})")
    try:
        data = json.loads(result.stdout)
    except (ValueError, RecursionError) as exc:
        raise Denied("Materialize adapter must return a JSON object") from exc
    if not isinstance(data, dict):
        raise Denied("Materialize adapter must return a JSON object")
    content = data.get("content")
    echoed = data.get("digest")
    if not isinstance(content, str) or len(content) > maximum:
        raise Denied("Materialize adapter must return content within the byte limit")
    if not isinstance(echoed, str) or len(echoed) != 64:
        raise Denied("Materialize adapter must confirm the content digest")
    if echoed != digest(canonical(content)):
        raise Denied("Materialized content digest mismatch")
    cache = data.get("cache", "")
    if cache is None:
        cache = ""
    if not isinstance(cache, str) or len(cache) > CACHE_ID_MAX:
        raise Denied("Invalid materialize cache identifier")
    if "\x00" in cache or "\n" in cache:
        raise Denied("Invalid materialize cache identifier")
    return {"content": content, "digest": echoed, "cache": cache,
            "trust": "external-untrusted"}
