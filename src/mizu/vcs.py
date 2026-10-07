"""Upstream-sync trusted argv adapter plus read-only ref injection.

Schema: operator-owned ``[vcs] command`` argv receives one JSON object on
stdin (``{"op": ...}``) and must print one JSON object on stdout.
Bounds: request canonical bytes <= ``max_bytes``; stdout capped at
``max_bytes`` via ``process.run``; at most 4096 refs; ref names 1-512 chars.
Trust: operator-owned host program only; never model-provided argv, never run
in the sandbox. Injected refs are external-untrusted until merged and
verified. Retry/cancellation: single invocation under ``timeout_seconds``;
no shell retry. Evidence: caller binds results to receipts/digests.
Failure: ``Denied`` on unconfigured adapter, oversize request, timeout,
oversize output, nonzero exit, malformed JSON, invalid refs, or unsafe paths.

Ref injection (host side only): ``inject_refs`` writes validated refs as
read-only files under the reserved workspace subtree
``refs/remotes/upstream/*``. ``Snapshots.excluded`` always excludes that
subtree, so injected refs never affect ``code_digest`` even if the model
rewrites them; only a host-side fetch refreshes them. ``files``/``read``
serve them read-only from the project workspace.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .errors import Denied
from .fs import atomic_write, canonical, safe_read
from .process import run

#: Reserved workspace subtree for injected upstream refs. Operators must not
#: keep project source here; snapshots always exclude it (see
#: ``Snapshots.excluded``) so refs never affect ``code_digest``.
REF_PREFIX = ("refs", "remotes", "upstream")
REF_PREFIX_PATH = "/".join(REF_PREFIX)
#: Maximum refs per fetch/injection (matches the adapter shape bound).
MAX_REFS = 4096
#: Ref file content bound for reads (a hex sha plus newline is far smaller).
REF_CONTENT_MAX = 4096

_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")


def invoke(settings: dict, request: dict) -> dict:
    """Invoke the configured adapter once and return its JSON object.

    Schema: ``request`` is a JSON object with a string ``op`` field;
    the adapter must return a JSON object. Bounds: canonical request bytes
    and adapter stdout are each capped at ``max_bytes``; the call is bounded
    by ``timeout_seconds``. Trust: ``settings`` comes from operator config,
    not model output. Failure: ``Denied`` with a short reason; adapter
    output is capped at ``max_bytes`` (combined stdout+stderr via
    ``process.run``) so adapter chatter cannot blow the frame.
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


def validate_refs(refs) -> dict:
    """Validate a ``{name: sha}`` mapping shared by fetch and injection.

    Schema: names are ``refs/remotes/upstream/``-relative paths (nonempty,
    at most 512 chars, no NUL/newlines/backslashes/colons, no empty/``.``/
    ``..`` components, no leading slash); shas are 40/64 lowercase hex.
    Bounds: at most 4096 entries. Failure: ``Denied``.
    """
    if not isinstance(refs, dict) or len(refs) > MAX_REFS:
        raise Denied("VCS adapter must return a refs object with at most 4096 entries")
    for name, sha in refs.items():
        _check_ref_name(name)
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise Denied("Invalid VCS ref digest")
    return dict(refs)


def _check_ref_name(name) -> None:
    if not isinstance(name, str) or not name or len(name) > 512:
        raise Denied("Invalid VCS ref name")
    if "\n" in name or "\x00" in name or "\\" in name or ":" in name:
        raise Denied("Invalid VCS ref name")
    if name.startswith("/"):
        raise Denied("Invalid VCS ref name")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise Denied("Invalid VCS ref name")


def fetch_refs(settings: dict) -> dict:
    """Fetch upstream refs via ``{"op": "fetch"}`` and validate the shape.

    Schema: response ``{"refs": {name: sha}}`` validated by
    ``validate_refs``. Bounds: at most 4096 refs. Trust: refs are
    external-untrusted until merged and verified. Failure: ``Denied``.
    """
    data = invoke(settings, {"op": "fetch"})
    return {"refs": validate_refs(data.get("refs")), "trust": "external-untrusted"}


def split_ref_path(path: str) -> str | None:
    """Return the ref name for a workspace path under the reserved prefix.

    Schema: ``refs/remotes/upstream/<name>`` with a valid ref name, else
    ``None``. Bounds: name rules from ``_check_ref_name``. Trust: pure path
    syntax, no filesystem access. Failure: never raises.
    """
    if not isinstance(path, str):
        return None
    parts = path.split("/")
    if parts[:3] != list(REF_PREFIX) or len(parts) < 4:
        return None
    name = "/".join(parts[3:])
    try:
        _check_ref_name(name)
    except Denied:
        return None
    return name


def _clear_readonly_for_windows(path: Path) -> None:
    """Clear the read-only bit so Windows can replace/remove the file.

    POSIX unlink/replace succeeds on read-only files, but Windows refuses
    with PermissionError. Injected refs stay read-only otherwise; this only
    makes the pending replace/remove writable on Windows.
    """
    if os.name != "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_host_dir(path: Path) -> None:
    """Create a host-side directory level, refusing symlinks and files."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o755)
        return
    except OSError as exc:
        raise Denied(f"Cannot inject upstream refs: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise Denied("Refusing to inject refs through a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise Denied("Upstream ref path is blocked by a non-directory")


def inject_refs(workspace: Path, refs: dict) -> dict:
    """Write validated refs as read-only files under the reserved subtree.

    Schema: ``refs`` is a ``{name: sha}`` mapping per ``validate_refs``;
    each ref lands at ``refs/remotes/upstream/<name>`` holding ``sha``
    plus a newline with mode 0o444. Stale files under the subtree are
    removed so a fetch refreshes the view; stale empty directories are
    left in place. Bounds: at most 4096 refs. Trust: host side only (the
    daemon/operator fetch path), never model-invoked; content stays
    external-untrusted. Retry/cancellation: single pass, no retry.
    Evidence: returned receipt names the prefix and count. Failure:
    ``Denied`` on invalid refs, symlink escape, or blocked paths.
    """
    validated = validate_refs(refs)
    if not workspace.is_dir() or workspace.is_symlink():
        raise Denied("Upstream refs require a real workspace directory")
    base = workspace.joinpath(*REF_PREFIX)
    current = workspace
    for part in REF_PREFIX:
        current = current / part
        _ensure_host_dir(current)
    wanted = set(validated)
    for name in sorted(wanted):
        parent = base
        for part in name.split("/")[:-1]:
            parent = parent / part
            _ensure_host_dir(parent)
        target = base.joinpath(*name.split("/"))
        try:
            existed = target.lstat()
        except FileNotFoundError:
            existed = None
        except OSError as exc:
            raise Denied(f"Cannot inject upstream refs: {exc}") from exc
        if existed is not None and stat.S_ISLNK(existed.st_mode):
            raise Denied("Refusing to overwrite a symlink with upstream ref content")
        if existed is not None:
            _clear_readonly_for_windows(target)
        atomic_write(target, (validated[name] + "\n").encode(), mode=0o444)
    _prune_stale(base, wanted)
    try:
        have = set(list_refs(workspace))
    except OSError as exc:
        raise Denied(f"Cannot verify injected upstream refs: {exc}") from exc
    if have != wanted:
        raise Denied("Injected upstream refs do not match the validated set")
    return {"injected": len(validated), "prefix": REF_PREFIX_PATH,
            "trust": "external-untrusted"}


def _prune_stale(base: Path, wanted: set[str]) -> None:
    """Remove injected files no longer present, refusing symlink games."""
    for directory, dirs, files in os.walk(base, followlinks=False):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink())
        for entry in sorted(files):
            target = here / entry
            try:
                if target.is_symlink():
                    raise Denied("Refusing to prune a symlinked upstream ref")
            except OSError as exc:
                raise Denied(f"Cannot prune upstream refs: {exc}") from exc
            rel = target.relative_to(base).as_posix()
            if rel not in wanted:
                try:
                    _clear_readonly_for_windows(target)
                    target.unlink()
                except OSError as exc:
                    raise Denied(f"Cannot prune upstream refs: {exc}") from exc


def list_refs(workspace: Path) -> list[str]:
    """List injected ref names (relative to the reserved prefix).

    Schema: sorted names; missing subtree reads as empty. Bounds: at most
    4096 entries or ``Denied``. Trust: host files; entries failing name
    validation (model-planted garbage) are skipped, never served. Failure:
    ``Denied`` when the subtree overflows; ``OSError`` only for host I/O.
    """
    base = workspace.joinpath(*REF_PREFIX)
    if not base.is_dir() or base.is_symlink():
        return []
    out: list[str] = []
    for directory, dirs, files in os.walk(base, followlinks=False):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink())
        for entry in sorted(files):
            target = here / entry
            if target.is_symlink():
                continue
            rel = target.relative_to(base).as_posix()
            try:
                _check_ref_name(rel)
            except Denied:
                continue
            out.append(rel)
    if len(out) > MAX_REFS:
        raise Denied("Too many injected upstream refs")
    return sorted(out)


def read_ref(workspace: Path, name: str) -> str:
    """Read one injected ref sha as text (read-only model view helper).

    Schema: ``name`` validated per ``_check_ref_name``; content must be a
    40/64 lowercase hex sha (trailing newline tolerated). Bounds: file
    capped at 4096 bytes via ``safe_read`` (no symlinks/hardlinks).
    Failure: ``Denied`` on bad names, unsafe paths, or malformed content.
    """
    _check_ref_name(name)
    data = safe_read(workspace, REF_PREFIX_PATH + "/" + name, REF_CONTENT_MAX)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Denied("Invalid upstream ref content") from exc
    if not _SHA.fullmatch(text.strip()):
        raise Denied("Invalid upstream ref content")
    return text.strip()


#: Read-only adapter operations served by ``vcs_read``. Publishing
#: operations (``push``/``pr``) are never served through this path.
#: ``acquire`` materializes exact-revision external content for assessment;
#: it mutates nothing, so it reads like any other observation.
READ_OPS = frozenset({"status", "log", "comments", "proposals", "acquire"})
#: Mutating adapter operations served by ``vcs_publish`` behind a recorded
#: human ``GO <branch>`` approval bound to branch and code digest.
PUBLISH_OPS = frozenset({"push", "pr"})

#: Approval body line binding an insight to the exact pushed tree.
#: Example body line: ``digest: <64 lowercase hex>``.
GO_DIGEST_RE = re.compile(r"^digest:\s*([0-9a-f]{64})\s*$", re.M)


def go_title(branch: str) -> str:
    """Return the required human-approval insight title for a branch."""
    return "GO " + branch


def parse_go_digest(body: str) -> str | None:
    """Return the ``digest:`` line from an approval body, else ``None``."""
    if not isinstance(body, str):
        return None
    match = GO_DIGEST_RE.search(body)
    return match.group(1) if match else None


def check_branch(branch: str) -> str:
    """Validate a publish/read branch name (same rules as ref names)."""
    _check_ref_name(branch)
    if len(branch) > 256:
        raise Denied("Invalid VCS branch name")
    return branch


#: Insight source and decision run marking an operator-recorded approval.
#: Model ``submit_insight`` fixes source to the role name and model ``decide``
#: fixes run to the run directory name, so neither can mint these values.
OPERATOR_SOURCE = "operator"
OPERATOR_RUN = "operator"


def require_go_approval(project, branch: str, code_digest: str) -> dict:
    """Require a recorded human ``GO <branch>`` approval for this digest.

    Schema: ``branch`` per ``check_branch``; ``code_digest`` 64 hex of the
    exact tree being published (workspace capture, not model supplied).
    Trust: local insight inbox plus ``decisions/`` records; the approval
    insight must carry source ``operator`` (``mizu insight submit``) and the
    ``accept`` decision must carry run ``operator`` (``mizu insight decide``).
    The opaque ``origin`` label is ignored here: it never grants approval.
    Model-submitted insights (source is the role name) and model decisions
    (run is the run directory name) never satisfy this gate. Use a dedicated
    publisher role without ``submit_insight``/``decide`` (refused together
    with ``vcs_publish`` at config load) for defense in depth.
    Retry/cancellation: pure local reads, no retry. Evidence: returned
    ``{"insight": id, "branch": ..., "code_digest": ...}`` names the
    approval bound to this publication. Failure: ``Denied`` when no matching
    title exists, when the digest line is missing/stale, when the source or
    decision run is not the operator channel, or when the matching
    record is undecided or not ``accept``. Stale digests are refused even
    when an older approval exists.
    """
    from .fs import DIGEST as _DIGEST
    check_branch(branch)
    if not isinstance(code_digest, str) or not _DIGEST.fullmatch(code_digest):
        raise Denied("Invalid code digest for publication approval")
    want = go_title(branch)
    inbox = project.root / "inbox"
    saw_title = False
    saw_stale = False
    saw_undecided = False
    saw_refused = False
    saw_forged = False
    try:
        paths = sorted(inbox.glob("*.json"))
    except OSError as exc:
        raise Denied(f"Publication approval is unavailable: {exc}") from exc
    for path in paths:
        if path.is_symlink():
            continue
        try:
            item = json.loads(path.read_bytes())
            if not isinstance(item, dict) or item.get("title") != want:
                continue
        except (OSError, ValueError):
            continue
        saw_title = True
        if item.get("source") != OPERATOR_SOURCE:
            saw_forged = True
            continue
        if parse_go_digest(item.get("body", "")) != code_digest:
            saw_stale = True
            continue
        insight_id = item.get("id")
        if not isinstance(insight_id, str) or not insight_id:
            continue
        decision_path = project.root / "decisions" / f"{insight_id}.json"
        if decision_path.is_symlink():
            saw_undecided = True
            continue
        try:
            decision = json.loads(decision_path.read_bytes())
        except (OSError, ValueError):
            saw_undecided = True
            continue
        if not isinstance(decision, dict) or decision.get("id") != insight_id:
            saw_undecided = True
            continue
        if decision.get("action") == "accept" and decision.get("run") == OPERATOR_RUN:
            return {"insight": insight_id, "branch": branch,
                    "code_digest": code_digest}
        if decision.get("action") == "accept":
            saw_forged = True
            continue
        saw_refused = True
    if saw_forged:
        raise Denied("Publication approval must come from the operator channel")
    if saw_refused or saw_undecided:
        raise Denied("Publication approval is not accepted for this code digest")
    if saw_stale:
        raise Denied("Recorded human approval is stale for this code digest")
    if saw_title:
        raise Denied("Publication approval is not accepted for this code digest")
    raise Denied("External publication requires recorded human approval")


#: Maximum acquired content chars per ``acquire`` response (mirrors the
#: default adapter byte bound; larger materializations are refused so the
#: caller narrows scope instead of blowing the frame).
MAX_ACQUIRE_CONTENT = 524288


def acquire_digest(content: str) -> str:
    """Bind acquired content to its digest (recomputed locally, never trusted).

    Schema: ``content`` is text; the digest is sha256 over
    ``fs.canonical(content)``. Adapters must echo this digest; the caller
    recomputes and compares instead of trusting the echo.
    """
    from .fs import digest as _digest, canonical as _canonical
    if not isinstance(content, str):
        raise Denied("Invalid VCS acquired content")
    return _digest(_canonical(content))


def parse_acquired(data: dict) -> dict:
    """Validate an ``acquire`` adapter response into a normalized receipt.

    Schema: ``{"id", "sha", "digest", "content"}``; ``id`` 1-128 chars
    without newline/NUL, ``sha`` 40/64 lowercase hex of the materialized
    revision, ``digest`` 64 lowercase hex, ``content`` text at most
    524288 chars. Trust: shape only; echo and digest binding are checked
    by the caller against the request. Failure: ``Denied``.
    """
    if not isinstance(data, dict) or set(data) != {"id", "sha", "digest", "content"}:
        raise Denied("Invalid VCS acquired content entry")
    identity = data.get("id")
    if (not isinstance(identity, str) or not identity or len(identity) > 128
            or "\n" in identity or "\x00" in identity):
        raise Denied("Invalid VCS proposal id for acquisition")
    sha = data.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise Denied("Invalid VCS sha for acquisition")
    content_digest = data.get("digest")
    if not isinstance(content_digest, str) or len(content_digest) != 64 or not _SHA.fullmatch(content_digest):
        raise Denied("Invalid VCS acquired content digest")
    content = data.get("content")
    if not isinstance(content, str) or len(content) > MAX_ACQUIRE_CONTENT:
        raise Denied("Invalid VCS acquired content")
    return {"id": identity, "sha": sha, "digest": content_digest, "content": content}


def read_via(settings: dict, op: str, params: dict) -> dict:
    """Invoke a read-only adapter operation (``status``/``log``/``comments``/``acquire``).

    Schema: ``params`` must include ``branch`` for addressed reads
    (``status``/``log``/``comments``); optional ``sha`` passes through
    when given. ``proposals`` enumerates with an optional ``branch``
    filter. ``acquire`` takes ``id`` (proposal id) plus the exact head
    ``sha`` to materialize and needs no branch: the endpoint travels in
    the proposal record, so fork heads acquire by id and sha alone.
    Bounds: same ``max_bytes``/timeout contract as ``invoke``. Trust:
    operator-owned adapter; results are external-untrusted. Failure:
    ``Denied`` on publish ops, bad params, a moved head (echo mismatch),
    a digest mismatch, or any adapter contract violation. Never publishes.
    """
    if op not in READ_OPS:
        raise Denied("vcs_read cannot publish; unknown or mutating operation")
    if not isinstance(params, dict):
        raise Denied("Invalid VCS read parameters")
    if op == "acquire":
        # Exact-revision acquisition: the adapter materializes the head
        # revision named by (id, sha) and echoes both plus the content
        # digest. A moved head fails closed here (echo mismatch), so the
        # caller re-observes proposals and retries with the fresh sha;
        # identical repeats re-acquire identical content.
        proposal_id = params.get("id")
        if (not isinstance(proposal_id, str) or not proposal_id or len(proposal_id) > 128
                or "\n" in proposal_id or "\x00" in proposal_id):
            raise Denied("Invalid VCS proposal id for acquisition")
        acquire_sha = params.get("sha")
        if not isinstance(acquire_sha, str) or not _SHA.fullmatch(acquire_sha):
            raise Denied("Invalid VCS sha for acquisition")
        acquired = parse_acquired(invoke(settings, {"op": op, "id": proposal_id, "sha": acquire_sha}))
        if acquired["id"] != proposal_id or acquired["sha"] != acquire_sha:
            raise Denied("Acquired content must match the requested proposal revision")
        if acquired["digest"] != acquire_digest(acquired["content"]):
            raise Denied("Acquired content digest mismatch")
        return {"op": op, "id": proposal_id, "sha": acquire_sha,
                "digest": acquired["digest"], "content": acquired["content"],
                "trust": "external-untrusted"}
    branch = params.get("branch")
    request = {"op": op}
    if op == "proposals":
        # Proposals are enumerated, not addressed: an optional branch only
        # filters the adapter query, so head/base movement is observable.
        # An optional cursor resumes a truncated enumeration; the response
        # reports completeness, never inferred from row counts.
        if branch is not None:
            check_branch(branch)
            request["branch"] = branch
        cursor = params.get("cursor")
        if cursor is not None:
            if (not isinstance(cursor, str) or not cursor or len(cursor) > 256
                    or "\n" in cursor or "\x00" in cursor):
                raise Denied("Invalid VCS proposals cursor")
            request["cursor"] = cursor
    else:
        check_branch(branch)
        request["branch"] = branch
    sha = params.get("sha")
    if sha is not None:
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise Denied("Invalid VCS sha filter")
        request["sha"] = sha
    data = invoke(settings, request)
    if op == "status":
        return {"op": op, "branch": branch, "checks": parse_status_checks(data),
                "trust": "external-untrusted"}
    if op == "proposals":
        complete = data.get("complete", None)
        if complete is not None and not isinstance(complete, bool):
            raise Denied("Invalid VCS proposals completeness flag")
        cursor_out = data.get("cursor", None)
        if cursor_out is not None and (
                not isinstance(cursor_out, str) or not cursor_out
                or len(cursor_out) > 256 or "\n" in cursor_out or "\x00" in cursor_out):
            raise Denied("Invalid VCS proposals cursor")
        return {"op": op, "branch": branch, "proposals": parse_proposals(data),
                "complete": complete, "cursor": cursor_out,
                "trust": "external-untrusted"}
    return {**data, "trust": "external-untrusted"}


#: Maximum CI checks per status response (adapter payload already capped).
MAX_CHECKS = 1024


def parse_status_checks(data: dict) -> list:
    """Validate a ``status`` adapter response into normalized check rows.

    Schema: ``{"checks": [{check, state, sha, url?}]}``; ``check`` 1-256
    chars without newline/NUL, ``state`` 1-64 chars without newline/NUL,
    ``sha`` 40/64 lowercase hex, optional ``url`` at most 4096 chars
    without newline/NUL. Bounds: at most 1024 checks. Trust: adapter
    facts stay external-untrusted; this only validates shape. Failure:
    ``Denied`` on missing/malformed shapes.
    """
    if not isinstance(data, dict):
        raise Denied("VCS status must be a JSON object")
    checks = data.get("checks")
    if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
        raise Denied("VCS status must carry a checks array with at most 1024 entries")
    out = []
    for entry in checks:
        if not isinstance(entry, dict) or set(entry) - {"check", "state", "sha", "url"}:
            raise Denied("Invalid VCS status check entry")
        check = entry.get("check")
        state = entry.get("state")
        sha = entry.get("sha")
        url = entry.get("url", "")
        if (not isinstance(check, str) or not check or len(check) > 256
                or "\n" in check or "\x00" in check):
            raise Denied("Invalid CI check name")
        if (not isinstance(state, str) or not state or len(state) > 64
                or "\n" in state or "\x00" in state):
            raise Denied("Invalid CI check state")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise Denied("Invalid VCS sha for CI insight")
        if not isinstance(url, str) or len(url) > 4096 or "\x00" in url or "\n" in url:
            raise Denied("Invalid CI log URL")
        out.append({"check": check, "state": state, "sha": sha, "url": url})
    return out


#: Maximum external proposals per ``proposals`` response.
MAX_PROPOSALS = 256
#: Closed-form proposal states; ``draft`` rides alongside ``open``.
PROPOSAL_STATES = frozenset({"open", "closed", "merged"})


def _check_repo(repo) -> str:
    if (not isinstance(repo, str) or not repo or len(repo) > 256
            or "\n" in repo or "\x00" in repo or not repo.strip()):
        raise Denied("Invalid VCS proposal repo")
    return repo


def _check_endpoint(value) -> dict:
    # Either a live ``{"repo", "ref", "sha"}`` endpoint or an explicit
    # tombstone ``{"deleted": true, "repo", "ref"}`` for a removed fork:
    # positive deletion evidence, never inferred from a missing row.
    # Tombstoned endpoints carry no sha, so no revision can be assessed,
    # acquired, or disposed from them.
    if not isinstance(value, dict):
        raise Denied("Invalid VCS proposal endpoint")
    if set(value) == {"deleted", "repo", "ref"} and value.get("deleted") is True:
        repo = _check_repo(value.get("repo"))
        check_branch(value.get("ref"))
        return {"deleted": True, "repo": repo, "ref": value["ref"]}
    if set(value) != {"repo", "ref", "sha"}:
        raise Denied("Invalid VCS proposal endpoint")
    repo = _check_repo(value.get("repo"))
    check_branch(value.get("ref"))
    sha = value.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise Denied("Invalid VCS sha for proposal endpoint")
    return {"repo": repo, "ref": value["ref"], "sha": sha}


#: Maximum independent reviews per proposal row.
MAX_REVIEWS = 64
#: Closed-form review verdicts; a dismissed approval no longer approves.
REVIEW_VERDICTS = frozenset({"approved", "changes_requested", "dismissed"})


def _check_review(value) -> dict:
    # One revision-bound review: who decided, what the verdict was, and
    # the exact revision reviewed. The reviewed sha stays put even when it
    # differs from the current head: stale approvals never reattach.
    if not isinstance(value, dict) or set(value) != {"reviewer", "verdict", "sha"}:
        raise Denied("Invalid VCS proposal review")
    reviewer = value.get("reviewer")
    if (not isinstance(reviewer, str) or not reviewer or len(reviewer) > 128
            or "\n" in reviewer or "\x00" in reviewer):
        raise Denied("Invalid VCS proposal reviewer")
    verdict = value.get("verdict")
    if verdict not in REVIEW_VERDICTS:
        raise Denied("Invalid VCS proposal review verdict")
    sha = value.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise Denied("Invalid VCS sha for proposal review")
    return {"reviewer": reviewer, "verdict": verdict, "sha": sha}


def parse_proposals(data: dict) -> list:
    """Validate a ``proposals`` adapter response into normalized rows.

    Schema: ``{"proposals": [{id, state, head, base, draft, mergeable?,
    checks?, reviews?, url?}]}``; ``id`` 1-128 chars without newline/NUL,
    ``state`` one of ``open``/``closed``/``merged``, ``head``/``base``
    are live ``{repo, ref, sha}`` endpoints (repo 1-256 chars, ref per
    ``check_branch``, sha 40/64 hex) or explicit tombstones
    ``{deleted: true, repo, ref}`` for removed forks, ``draft`` a bool,
    ``mergeable`` a bool or absent/null (unknown), ``checks`` CI rows per
    ``parse_status_checks`` (each row carries its own tested sha, which
    may differ from the head sha for merge-commit rollups), ``reviews``
    revision-bound review rows or absent/null (unknown), ``url`` at most
    4096 chars without newline/NUL. Bounds: at most 256 proposals, at
    most 64 reviews each. Trust: adapter facts stay external-untrusted;
    this only validates shape. Failure: ``Denied``.
    """
    if not isinstance(data, dict):
        raise Denied("VCS proposals must be a JSON object")
    proposals = data.get("proposals")
    if not isinstance(proposals, list) or len(proposals) > MAX_PROPOSALS:
        raise Denied("VCS proposals must carry an array with at most 256 entries")
    out = []
    for entry in proposals:
        if not isinstance(entry, dict) or set(entry) - {
                "id", "state", "head", "base", "draft", "mergeable",
                "checks", "reviews", "url"}:
            raise Denied("Invalid VCS proposal entry")
        identity = entry.get("id")
        if (not isinstance(identity, str) or not identity or len(identity) > 128
                or "\n" in identity or "\x00" in identity):
            raise Denied("Invalid VCS proposal id")
        state = entry.get("state")
        if state not in PROPOSAL_STATES:
            raise Denied("Invalid VCS proposal state")
        head = _check_endpoint(entry.get("head"))
        base = _check_endpoint(entry.get("base"))
        draft = entry.get("draft")
        if not isinstance(draft, bool):
            raise Denied("Invalid VCS proposal draft flag")
        mergeable = entry.get("mergeable", None)
        if mergeable is not None and not isinstance(mergeable, bool):
            raise Denied("Invalid VCS proposal mergeable flag")
        raw_checks = entry.get("checks", [])
        if not isinstance(raw_checks, list):
            raise Denied("Invalid VCS proposal checks")
        checks = parse_status_checks({"checks": raw_checks})
        raw_reviews = entry.get("reviews", None)
        if raw_reviews is None:
            reviews = None
        else:
            if not isinstance(raw_reviews, list) or len(raw_reviews) > MAX_REVIEWS:
                raise Denied("Invalid VCS proposal reviews")
            reviews = [_check_review(item) for item in raw_reviews]
        url = entry.get("url", "")
        if not isinstance(url, str) or len(url) > 4096 or "\x00" in url or "\n" in url:
            raise Denied("Invalid VCS proposal URL")
        out.append({"id": identity, "state": state, "head": head,
                    "base": base, "draft": draft, "mergeable": mergeable,
                    "checks": checks, "reviews": reviews, "url": url})
    return out


def proposal_insight_id(proposal_id: str) -> str:
    """Derive a stable, deduplicating insight ID for an external proposal."""
    from .fs import digest as _digest, canonical as _canonical
    if (not isinstance(proposal_id, str) or not proposal_id
            or len(proposal_id) > 128 or "\n" in proposal_id
            or "\x00" in proposal_id):
        raise Denied("Invalid VCS proposal id")
    return "proposal-" + _digest(_canonical({"proposal": proposal_id}))[:32]


def proposal_facts(proposal: dict) -> tuple[str, str]:
    """Render the stable title and facts body for a validated proposal.

    The body covers identity, state, draft/mergeable flags, head/base
    endpoints and check rows only: per-run log URLs vary without meaning
    and stay available via ``vcs_read`` ``proposals``. Repeats with
    identical facts resubmit identical content, so unchanged observations
    never wake new work.
    """
    row = parse_proposals({"proposals": [proposal]})[0]
    title = f"Proposal {row['id']} {row['state']}"
    if len(title) > 200:
        title = title[:200]
    lines = [f"id: {row['id']}", f"state: {row['state']}",
             f"draft: {row['draft']}",
             f"mergeable: {row['mergeable'] if row['mergeable'] is not None else 'unknown'}",
             _format_endpoint("head", row["head"]),
             _format_endpoint("base", row["base"])]
    for check in row["checks"]:
        lines.append(f"check: {check['check']} {check['state']} {check['sha']}")
    if row["reviews"] is None:
        lines.append("reviews: unknown")
    elif not row["reviews"]:
        lines.append("reviews: none")
    for review in row["reviews"] or []:
        lines.append(f"review: {review['reviewer']} {review['verdict']} {review['sha']}")
    lines.append("trust: external-untrusted")
    return title, "\n".join(lines) + "\n"


def _format_endpoint(role: str, endpoint: dict) -> str:
    """Render one endpoint fact line, tombstones included."""
    if endpoint.get("deleted") is True:
        return f"{role}: {endpoint['repo']} {endpoint['ref']} deleted"
    return f"{role}: {endpoint['repo']} {endpoint['ref']} {endpoint['sha']}"


def record_proposal_state(project, proposal: dict, *,
                          run: str | None = None,
                          origin: str | None = None) -> dict:
    """Record an external proposal observation; revise only on change.

    Schema: ``proposal`` is one validated ``parse_proposals`` row.
    ``origin`` is an opaque trusted-caller label stored alongside the
    ``vcs`` authority; it never affects approval, routing, or dedup (the
    first-stored origin/run is preserved on repeats). Bounds: title <=
    200 chars, body within the insight byte limit. Trust: adapter-derived
    facts labeled external-untrusted in the body; the stable ID lets
    repeats return the existing record instead of spamming the inbox.
    Retry/cancellation: pure local insight reads/writes; identical facts
    are a read-only no-op (no rev bump, no wake), changed facts revise
    with an expected-rev compare-and-swap. Evidence: returned
    ``{"id": ..., "changed": bool}`` names the observation. Failure:
    ``Denied`` on bad proposals or a foreign record under the stable ID.
    """
    row = parse_proposals({"proposals": [proposal]})[0]
    title, body = proposal_facts(row)
    insight_id = proposal_insight_id(row["id"])
    try:
        current = project.insights.read(insight_id)
    except Denied:
        current = None
    if current is None:
        record = project.insights.submit(source="vcs", title=title, body=body,
                                         base_snapshot=None, run=run,
                                         origin=origin, insight_id=insight_id)
        return {"id": record["id"], "changed": True}
    if current.get("body") == body and current.get("title") == title:
        return {"id": current["id"], "changed": False}
    record = project.insights.revise(insight_id, source="vcs", title=title,
                                     body=body, base_snapshot=None, run=run,
                                     expected_rev=current.get("rev"))
    return {"id": record["id"], "changed": True}


def live_proposal_refs(project, branch: str) -> list:
    """Return sorted proposal ids still openly referencing a branch.

    Schema: scans recorded ``vcs`` proposal observations (``proposal-``
    records; CI ``ci-`` records never match) and parses their ``state:``
    and ``head:``/``base:`` fact lines. A proposal blocks while its
    latest recorded state is outside ``TERMINAL_STATES`` and a
    non-deleted head or base endpoint names ``branch``. Refs are the
    second-to-last line token (repos may hold spaces; ref names never
    do); ``deleted`` endpoints reference nothing live. Bounds: unknown
    states block (fail closed toward preservation); unparseable bodies
    are skipped, never treated as references. Trust: recorded local
    observations only, no adapter call. Failure: never raises for
    missing records (empty references); ``Denied`` only on a bad
    branch name.
    """
    check_branch(branch)
    blocking = set()
    for record in project.insights.scan_source("vcs"):
        if not record.get("id", "").startswith("proposal-"):
            continue
        body = record.get("body") or ""
        if not isinstance(body, str):
            continue
        state = None
        refs = set()
        for line in body.splitlines():
            if line.startswith("state: "):
                state = line[len("state: "):]
            elif line.startswith("head: ") or line.startswith("base: "):
                parts = line.split(" ")[1:]
                if len(parts) >= 2 and parts[-1] != "deleted":
                    refs.add(parts[-2])
        if state is not None and state not in TERMINAL_STATES and branch in refs:
            for line in body.splitlines():
                if line.startswith("id: "):
                    blocking.add(line[len("id: "):])
                    break
    return sorted(blocking)


def publish_via(settings: dict, op: str, params: dict) -> dict:
    """Invoke a mutating adapter operation (``push``/``pr``) after approval.

    Schema: ``params`` must include ``branch`` and ``code_digest`` (64 hex
    of the approved tree). The request sends ``digest`` alongside ``op``
    and ``branch``; the adapter must verify the pushed tree matches it
    before pushing and echo the same ``digest`` in its response. Bounds:
    same contract as ``invoke``. Trust: operator-owned adapter only; the
    caller must have passed ``require_go_approval`` first (this helper
    checks the op gate, not the human record). Failure: ``Denied`` on
    read-only/unknown ops, bad digest, or adapter violations including a
    missing/mismatched digest echo.
    """
    from .fs import DIGEST as _DIGEST
    if op not in PUBLISH_OPS:
        raise Denied("vcs_publish cannot publish this operation")
    if not isinstance(params, dict):
        raise Denied("Invalid VCS publish parameters")
    branch = params.get("branch")
    check_branch(branch)
    code_digest = params.get("code_digest")
    if not isinstance(code_digest, str) or not _DIGEST.fullmatch(code_digest):
        raise Denied("Invalid code digest for publication")
    data = invoke(settings, {"op": op, "branch": branch, "digest": code_digest})
    if not isinstance(data, dict):
        raise Denied("VCS adapter must return a JSON object")
    if data.get("digest") != code_digest:
        raise Denied("VCS adapter must confirm the pushed code digest")
    return {**data, "trust": "external-untrusted"}


#: Branch lifecycle classes for the retire path. Only ``owned`` temporary
#: integration branches are retire-eligible; every other class is
#: preserved: ``protected`` long-lived development/release refs,
#: ``external`` fork/pull namespaces outside local heads, ``tracking``
#: remote-tracking refs, and ``other`` active work of unknown ownership.
BRANCH_CLASSES = frozenset({"owned", "protected", "external", "tracking", "other"})


def _check_prefix(prefix) -> str:
    if (not isinstance(prefix, str) or not prefix or len(prefix) > 256
            or any(char.isspace() or char in "\x00\\:" for char in prefix)
            or prefix.startswith("/")):
        raise Denied("Invalid VCS owned prefix")
    parts = prefix.split("/")
    if any(part in (".", "..") for part in parts):
        raise Denied("Invalid VCS owned prefix")
    if any(part == "" for part in parts[:-1]) or (parts[-1] == "" and len(parts) < 2):
        raise Denied("Invalid VCS owned prefix")
    return prefix


def _prefix_match(short: str, prefix: str) -> bool:
    if prefix.endswith("/"):
        return short.startswith(prefix)
    return short == prefix or short.startswith(prefix + "/")


def _short_branch_name(name: str) -> str:
    """Return the short branch name for a short or ``refs/heads/`` ref."""
    return name[len("refs/heads/"):] if name.startswith("refs/heads/") else name


def classify_branch(ref, *, owned_prefixes=(), protected_refs=()) -> str:
    """Classify a branch/ref into its lifecycle class (pure, no I/O).

    Schema: ``ref`` is a short branch name or a full ref path
    (``refs/heads/<name>``); ``owned_prefixes`` is a list of literal
    namespace prefixes (``"mizu/"``), ``protected_refs`` a list of exact
    protected names in either short or ``refs/heads/`` form. Order:
    ``refs/remotes/*`` reads as ``tracking``, other non-heads ``refs/``
    namespaces (pull/fork) read as ``external``, then exact ``protected``
    wins over ``owned`` prefix match, and anything else is ``other``
    active work. Bounds: names per ``_check_ref_name``. Trust: pure local
    syntax plus operator namespaces; never consults the network. Failure:
    ``Denied`` on bad refs or malformed namespace policy.
    """
    _check_ref_name(ref)
    if (not isinstance(owned_prefixes, (list, tuple))
            or not isinstance(protected_refs, (list, tuple))):
        raise Denied("Invalid VCS branch namespace policy")
    owned = [_check_prefix(entry) for entry in owned_prefixes]
    for entry in protected_refs:
        check_branch(entry)
    if ref.startswith("refs/remotes/"):
        return "tracking"
    if ref.startswith("refs/") and not ref.startswith("refs/heads/"):
        return "external"
    short = _short_branch_name(ref)
    # Protected entries match in either spelling: a full ``refs/heads/``
    # entry protects the short branch and vice versa, so an owned-prefix
    # match can never retire a branch the operator meant to protect.
    protected = {_short_branch_name(entry) for entry in protected_refs}
    if short in protected:
        return "protected"
    if any(_prefix_match(short, prefix) for prefix in owned):
        return "owned"
    return "other"


def retire_via(settings: dict, branch, expected_sha) -> dict:
    """Delete an owned temporary integration branch with expected-SHA protection.

    Schema: ``settings`` carries the operator grant (``retire_grant``
    true), the namespace policy (``owned_prefixes``/``protected_refs``),
    and the adapter contract. ``branch`` per ``check_branch`` must
    classify ``owned``; ``expected_sha`` is the 40/64 hex head the caller
    observed. The adapter request sends ``{"op": "retire", "branch",
    "expected_sha"}; the adapter must delete only when its current head
    still equals ``expected_sha`` (revalidation immediately before
    action, compare-and-delete) and echo ``branch``/``sha`` with
    ``deleted`` true. Bounds: same contract as ``invoke``. Trust:
    operator-owned adapter only; results stay external-untrusted.
    Evidence: returned receipt names branch, sha, and classification.
    Failure: ``Denied`` without the configured grant, on non-owned
    branches (protected/external/tracking/other are preserved), on bad
    shas, or on a missing/mismatched confirmation echo. A failed call
    implies nothing about the remote ref; reconcile by re-observing.
    """
    if not isinstance(settings, dict) or settings.get("retire_grant") is not True:
        raise Denied("Branch retirement is not granted in VCS configuration")
    check_branch(branch)
    if not isinstance(expected_sha, str) or not _SHA.fullmatch(expected_sha):
        raise Denied("Invalid expected branch sha")
    classification = classify_branch(
        branch, owned_prefixes=settings.get("owned_prefixes", ()),
        protected_refs=settings.get("protected_refs", ()))
    if classification != "owned":
        raise Denied(f"Only owned integration branches retire; '{branch}' is {classification}")
    data = invoke(settings, {"op": "retire", "branch": branch,
                             "expected_sha": expected_sha})
    if not isinstance(data, dict):
        raise Denied("VCS adapter must return a JSON object")
    if (data.get("branch") != branch or data.get("sha") != expected_sha
            or data.get("deleted") is not True):
        raise Denied("VCS adapter must confirm the retired branch at the expected sha")
    return {"branch": branch, "sha": expected_sha, "deleted": True,
            "classification": classification, "trust": "external-untrusted"}


#: Mutating adapter operations that resolve an external proposal to a
#: terminal state. Served by ``vcs_dispose`` behind a configured grant
#: plus a recorded human ``GO <branch>`` approval for the integrated
#: tree: disposition is explicit per action, never automatic. The main
#: promotion flow is untouched; this only resolves the external record
#: after approved integration.
DISPOSE_OPS = frozenset({"close", "merge"})

#: Terminal proposal states; only these confirm a final disposition.
TERMINAL_STATES = frozenset({"closed", "merged"})


def dispose_via(settings: dict, op: str, proposal_id: str, expected_sha) -> dict:
    """Resolve an external proposal to its terminal state at an exact sha.

    Schema: ``settings`` carries the operator grant (``dispose_grant``
    true); ``op`` is ``close`` or ``merge``; ``proposal_id`` names the
    proposal; ``expected_sha`` is the 40/64 hex head the caller assessed.
    The adapter request sends ``{"op": op, "id", "sha"}``; the adapter
    must act only while the proposal is still open at ``expected_sha``
    (compare-and-dispose immediately before action: a moved head or an
    externally superseded proposal refuses) and echo ``id``/``sha`` with
    the terminal ``state`` (``close`` ends ``closed``, ``merge`` ends
    ``merged``). Bounds: same contract as ``invoke``. Trust:
    operator-owned adapter only; results stay external-untrusted.
    Evidence: returned receipt names op, id, sha, and terminal state.
    Failure: ``Denied`` without the configured grant, on unknown ops,
    bad ids/shas, or a missing/mismatched confirmation echo. A failed
    call implies nothing about the remote proposal; reconcile by
    re-observing, never by assuming disposition.
    """
    if not isinstance(settings, dict) or settings.get("dispose_grant") is not True:
        raise Denied("Proposal disposition is not granted in VCS configuration")
    if op not in DISPOSE_OPS:
        raise Denied("vcs_dispose cannot dispose with this operation")
    if (not isinstance(proposal_id, str) or not proposal_id or len(proposal_id) > 128
            or "\n" in proposal_id or "\x00" in proposal_id):
        raise Denied("Invalid VCS proposal id for disposition")
    if not isinstance(expected_sha, str) or not _SHA.fullmatch(expected_sha):
        raise Denied("Invalid expected proposal sha")
    data = invoke(settings, {"op": op, "id": proposal_id, "sha": expected_sha})
    if not isinstance(data, dict) or set(data) != {"id", "sha", "state"}:
        raise Denied("VCS adapter must confirm the disposed proposal")
    if data.get("id") != proposal_id or data.get("sha") != expected_sha:
        raise Denied("VCS adapter must confirm the disposed proposal at the expected sha")
    state = data.get("state")
    want = "merged" if op == "merge" else "closed"
    if state != want:
        raise Denied("VCS adapter must confirm the terminal proposal state")
    return {"op": op, "id": proposal_id, "sha": expected_sha, "state": state,
            "trust": "external-untrusted"}


def ci_insight_id(branch: str, sha: str, check: str) -> str:
    """Derive a stable, deduplicating insight ID for a CI failure."""
    from .fs import digest as _digest, canonical as _canonical
    check_branch(branch)
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise Denied("Invalid VCS sha for CI insight")
    if not isinstance(check, str) or not check or len(check) > 256 or "\n" in check or "\x00" in check:
        raise Denied("Invalid CI check name")
    return "ci-" + _digest(_canonical({"branch": branch, "sha": sha, "check": check}))[:32]


def record_ci_result(project, *, branch: str, sha: str, check: str,
                     state: str, url: str = "", run: str | None = None,
                     origin: str | None = None) -> dict:
    """Record a CI failure as an insight; deduplicate repeats, ignore passes.

    Schema: ``state`` is ``"failure"`` (record) or anything else
    (ignored). ``url`` is an optional bounded log link, validated but not
    stored: the persisted body covers only branch/sha/check so repeats with
    a varying per-run log URL resubmit identical content and return the
    existing record. ``origin`` is an opaque trusted-caller label stored
    alongside the ``vcs`` authority; it never affects approval, routing,
    or dedup (the first-stored origin/run is preserved on repeats).
    Bounds: title <= 200 chars, body within the insight
    byte limit. Trust: adapter-derived facts labeled external-untrusted in
    the body; the stable ID lets repeats return the existing record instead
    of spamming the inbox. The latest log URL stays available via
    ``vcs_read`` ``status``. Failure: ``Denied`` on bad names, bad URLs or
    bad origins; adapter content never raises beyond validation.
    """
    if state != "failure":
        return {"recorded": False, "state": state}
    if not isinstance(url, str) or len(url) > 4096 or "\x00" in url or "\n" in url:
        raise Denied("Invalid CI log URL")
    insight_id = ci_insight_id(branch, sha, check)
    title = f"CI {check} failed on {branch}"
    if len(title) > 200:
        title = title[:200]
    body = (f"CI check '{check}' failed on branch '{branch}' at {sha}.\n"
            f"trust: external-untrusted\n")
    record = project.insights.submit(source="vcs", title=title, body=body,
                                     base_snapshot=None, run=run, origin=origin,
                                     insight_id=insight_id)
    return {"recorded": True, "id": record["id"], "title": title}
