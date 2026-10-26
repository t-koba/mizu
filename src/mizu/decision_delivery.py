"""Single runtime authority for insight decisions; activation is host policy only."""
from dataclasses import asdict, replace

from .config import role_decision_events_limit
from .errors import Denied
from .fs import DIGEST, MAX_FRAME, canonical, digest, loads_finite, safe_read
from .insights import INSIGHT_DECISION_POLICY
from .shared_records import SharedRecords

AUTHORITY_FILE = "decision-authority.json"
PLAN_FILE = "decision-activation-plan.json"
REPORT_FILE = "decision-activation-report.json"


def authority(root):
    """Absent means legacy. An unsafe, malformed or partial marker never does."""
    try:
        (root / AUTHORITY_FILE).lstat()
    except FileNotFoundError:
        return None
    raw = safe_read(root, AUTHORITY_FILE, 4096)
    try:
        marker = loads_finite(raw)
        if (type(marker) is not dict or set(marker) != {"schema", "kind", "authority", "plan_digest", "report_digest"}
                or type(marker["schema"]) is not int or marker["schema"] != 1
                or marker["kind"] != "insight.decision" or marker["authority"] != "shared"
                or any(not isinstance(marker[key], str) or not DIGEST.fullmatch(marker[key])
                       for key in ("plan_digest", "report_digest")) or canonical(marker) != raw):
            raise ValueError("marker schema")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise Denied("Invalid decision authority marker; reconciliation required") from exc
    report_raw = safe_read(root, REPORT_FILE, MAX_FRAME)
    if digest(report_raw) != marker["report_digest"]:
        raise Denied("Decision authority report digest mismatch")
    report = loads_finite(report_raw)
    if not isinstance(report, dict) or report.get("plan_digest") != marker["plan_digest"]:
        raise Denied("Decision authority plan digest mismatch")
    return marker


def page_bytes(role):
    value = getattr(role, "decision_page_bytes", None)
    if type(value) is not int or not 1 <= value <= MAX_FRAME:
        raise Denied("Shared decision subscribers require explicit decision_page_bytes (1..1048576)")
    return value


def pending(project, role, *, actions=None, limit=None):
    """One branch only, shared failures propagate. Manifest is host-only unit state."""
    shared = authority(project.insights.root) is not None
    actions = role.decision_events if actions is None else actions
    limit = role_decision_events_limit(project.config, role) if limit is None else limit
    sources = getattr(role, "decision_sources", "own") or "own"
    if shared:
        budget = page_bytes(role) if role.decision_events else 1  # no subscription, no delivery
        if actions:
            page, manifest = project.insights.shared_decision_page(
                role.name, actions, max_bytes=budget, limit=limit, sources=sources, prompt=True)
        else:
            page, manifest = {"events": [], "pending_count": 0, "truncated": False,
                              "count_truncated": False, "byte_truncated": False}, ()
        return dict(shared=True, events=page["events"], diagnostics=[], page=page, manifest=manifest)
    events, diagnostics = (project.insights.decision_events_with_diagnostics(
        role.name, actions, limit=limit, sources=sources) if actions else ([], []))
    return dict(shared=False, events=events, diagnostics=diagnostics, page=None, manifest=())


def store(context):
    # Do not reinterpret a retained unit against today's routes/source. But an
    # authority mutation/corruption cannot authorize a different receipt writer.
    if not context.shared_decisions or authority(context.project.insights.root) is None:
        raise Denied("Shared decision unit authority changed")
    return SharedRecords(context.project.insights.root, {"insight.decision": INSIGHT_DECISION_POLICY})


def validate(context, tokens):
    shared = store(context)
    allowed = shared._manifest(context.role.name, context.decision_manifest)
    for token in tokens:
        if token not in allowed:
            raise Denied("Decision token is not in this unit's manifest")
        entry = allowed[token]
        shared.get(entry.record_id, expected_digest=entry.record_digest)


def acknowledge(context, tokens):
    shared = store(context)
    validate(context, tokens)
    selections = [asdict(entry) for entry in context.decision_manifest if entry.ack_token in tokens]
    with context.project.insights._decision_receipt_lock(context.role.name):
        shared.acknowledge(context.role.name, selections, manifest=context.decision_manifest)


def read(context, args):
    return store(context).read_page(context.role.name, args["ack_token"],
                                    manifest=context.decision_manifest,
                                    offset=args.get("offset", 0), limit=args.get("limit", 8192))


def bind(context, state):
    """Freeze delivery and a manifest-only reader in this unit's host context.

    The subscription grants this narrow reader, not arbitrary store access.
    No generic write or activation tool is exposed.
    """
    context.shared_decisions = state["shared"]
    context.decision_manifest = state["manifest"]
    context.decision_page = state["page"]
    context.decision_events = state["events"]
    context.decision_receipt_diagnostics = state["diagnostics"]
    if state["shared"] and context.role.decision_events and "decision_read" not in context.role.capabilities:
        context.role = replace(context.role, capabilities=(*context.role.capabilities, "decision_read"))
