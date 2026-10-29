"""Partial Context and live-tree boundary fencing; no runner or quiescence authority.

The host may inject a private reservation projection: id, owner, status and
request.source_snapshot. Additional trusted store fields are ignored. None
means no gate; every other value must validate or all calls are refused. The
view is never accepted from tool arguments. This is a fixed per-Context view,
not a durable reservation lookup or an atomic workspace exclusion mechanism.
"""
from pathlib import Path
import tomllib

from .errors import Denied, LimitExceeded
from .fs import DIGEST, safe_read
from .protocol import DEFINITIONS, validate

POLICY_PATH = Path(__file__).resolve().parents[2] / "docs" / "async-delegation-gates.toml"
POLICY_MAX_BYTES = 65536


def _policy():
    """Load only for a held/unknown reservation; missing/bad policy is denial."""
    try:
        raw = safe_read(POLICY_PATH.parent, POLICY_PATH.name, POLICY_MAX_BYTES)
        policy = tomllib.loads(raw.decode("utf-8"))
        if (policy["version"] != 1 or policy["status"] != "pre-integration-gates"
                or policy["runner_integration_allowed"] is not False):
            raise ValueError
        for key in ("reserved_statuses", "parent_control_plane", "parent_transport", "parent_snapshot_only"):
            if (not isinstance(policy[key], list) or not policy[key]
                    or any(not isinstance(item, str) or not item for item in policy[key])):
                raise ValueError
        vcs = policy["parameterized"]["vcs_read"]
        if (vcs["default"] != "deny" or not isinstance(vcs["allow"], list)
                or any(not isinstance(item, str) or not item for item in vcs["allow"])):
            raise ValueError
        return policy
    except (OSError, ValueError, KeyError, TypeError, LimitExceeded):
        raise Denied("Reservation access policy is unavailable or malformed") from None


def check_access(view, role_name, snapshot, operation, arguments):
    if view is None:
        return  # No policy I/O or change to ordinary authorization.
    policy = _policy()
    if (not isinstance(view, dict) or not isinstance(view.get("id"), str)
            or not DIGEST.fullmatch(view["id"]) or view.get("owner") != role_name
            or not isinstance(view.get("status"), str)
            or view["status"] not in policy["reserved_statuses"]
            or not isinstance(view.get("request"), dict)):
        raise Denied("Reservation state is unknown or belongs to another role")
    source = view["request"].get("source_snapshot")
    if not isinstance(source, str) or not DIGEST.fullmatch(source):
        raise Denied("Reservation source snapshot is unknown")
    if not isinstance(operation, str) or not isinstance(arguments, dict):
        raise Denied("Invalid reserved operation")
    if operation in policy["parent_transport"]:
        return
    if operation in policy["parent_control_plane"]:
        return
    if (operation == "diff" and operation in policy["parent_snapshot_only"]
            and isinstance(snapshot, dict) and snapshot.get("id") == source):
        return
    if operation == "vcs_read":
        # Direct dispatch must reject malformed parameters too, before handlers.
        validate(arguments, DEFINITIONS[operation][1])
        if arguments.get("op") in policy["parameterized"][operation]["allow"]:
            return
    # consult has no explicit immutable input; finish has no exact-job wait.
    # Neither a native tool grant nor an unknown operation bypasses this fence.
    raise Denied("Operation is unavailable while the workspace is reserved")


def refuse_live_tree(view, operation):
    """Fail closed before a live-workspace capture or child access.

    The view is an internal host binding. Any non-null value means access may
    be reserved, including malformed or stale views; callers must not infer
    release from missing fields or terminal execution alone.
    """
    if view is not None:
        raise Denied(f"{operation} is unavailable while workspace access may be reserved")
