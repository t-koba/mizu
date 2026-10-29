"""Single runtime authority for insight decisions; activation is host policy only."""
from dataclasses import asdict, replace
from datetime import datetime

import json

from .config import role_decision_events_limit
from .errors import Denied, LimitExceeded
from .fs import DIGEST, MAX_FRAME, canonical, digest, loads_finite, safe_read
from .insights import INSIGHT_DELIVERY_POLICIES, DECISION_RECEIPT_DIAGNOSTIC_LIMIT
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


def pending(project, role, *, actions=None, limit=None, include_insights=True):
    """One branch only, shared failures propagate. Manifest is host-only unit state."""
    shared = authority(project.insights.root) is not None
    if role.name == "operator":
        raise Denied("Operator insights belong to the human host inbox")
    configured_roles = set(project.roles) & project.config.roles.keys()
    actions = role.decision_events if actions is None else actions
    limit = role_decision_events_limit(project.config, role) if limit is None else limit
    sources = getattr(role, "decision_sources", "own") or "own"
    if shared:
        budget = page_bytes(role) if role.decision_events else (role.decision_page_bytes or MAX_FRAME)
        if actions or include_insights:
            page, manifest = project.insights.shared_decision_page(
                role.name, actions, max_bytes=budget, limit=limit, sources=sources, prompt=True,
                configured_roles=configured_roles if include_insights else None)
        else:
            page, manifest = {"events": [], "pending_count": 0, "truncated": False,
                              "count_truncated": False, "byte_truncated": False}, ()
        return dict(shared=True, events=page["events"], diagnostics=[], page=page, manifest=manifest)
    events, diagnostics = (project.insights.decision_events_with_diagnostics(
        role.name, actions, limit=limit, sources=sources) if actions else ([], []))
    if include_insights:
        insights, troubled = project.insights.pending_insight_events(role.name, configured_roles, limit=limit)
        # Each candidate list is already limited to `limit`; exactly `limit`
        # rows means the true total may be larger, so the combined total is
        # incomplete. Never present that incomplete total as exact.
        decision_capped = bool(actions) and len(events) == limit
        insight_capped = len(insights) == limit
        source_capped = decision_capped or insight_capped
        combined_all = [*events, *insights]
        diagnostics = (diagnostics + troubled)[:DECISION_RECEIPT_DIAGNOSTIC_LIMIT]
        combined_all.sort(key=lambda e: (datetime.fromisoformat(e.get("decided_at", e.get("created_at"))),
                                         e.get("kind") == "insight.pending", e["event"]))
        if source_capped:
            pending_total = None
            count_truncated = True
        else:
            pending_total = len(combined_all)
            count_truncated = pending_total > limit
        limited = combined_all[:limit]
        delivered = list(limited)
        # Bound the combined legacy decision/attention prompt contribution to
        # the existing fixed MAX_FRAME transport bound without a new knob.
        # Preserve all current decision events and exact ack tokens; enforce
        # the bound by omitting only newest attention rows (oldest attention
        # prefix kept, oldest-first replay preserved). Omitted attention rows
        # stay pending and truncation is visible via the returned page.
        if any(e.get("kind") == "insight.pending" for e in delivered):
            def _legacy_summary(evts):
                if source_capped:
                    return {"pending_count": None,
                            "truncated": True,
                            "count_truncated": True,
                            "byte_truncated": len(evts) < len(limited)}
                return {"pending_count": pending_total,
                        "truncated": len(evts) < pending_total,
                        "count_truncated": count_truncated,
                        "byte_truncated": len(evts) < min(pending_total, limit)}
            def _legacy_size(evts):
                return len(json.dumps({"decision_events": evts,
                                       "decision_page": _legacy_summary(evts)},
                                      ensure_ascii=True).encode("utf-8"))
            if _legacy_size([]) > MAX_FRAME:
                raise LimitExceeded("Legacy decision page metadata exceeds byte bound")
            while delivered and _legacy_size(delivered) > MAX_FRAME:
                for idx in range(len(delivered) - 1, -1, -1):
                    if delivered[idx].get("kind") == "insight.pending":
                        del delivered[idx]
                        break
                else:
                    # The decision-only payload itself exceeds the bound; it
                    # was deliverable before attention existed. Preserve the
                    # exact pre-existing decision rows instead of newly
                    # failing because attention was present. All attention is
                    # already omitted above and stays pending.
                    break
            if len(delivered) < len(limited):
                summary = _legacy_summary(delivered)
                page = {"events": list(delivered), **summary}
                return dict(shared=False, events=delivered, diagnostics=diagnostics,
                            page=page, manifest=())
        events = delivered
    return dict(shared=False, events=events, diagnostics=diagnostics, page=None, manifest=())


def store(context):
    # Do not reinterpret a retained unit against today's routes/source. But an
    # authority mutation/corruption cannot authorize a different receipt writer.
    if not context.shared_decisions or authority(context.project.insights.root) is None:
        raise Denied("Shared decision unit authority changed")
    return SharedRecords(context.project.insights.root, INSIGHT_DELIVERY_POLICIES)


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
    if state["shared"] and state["manifest"] and "decision_read" not in context.role.capabilities:
        context.role = replace(context.role, capabilities=(*context.role.capabilities, "decision_read"))
