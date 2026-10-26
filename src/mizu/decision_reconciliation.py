"""Trusted host-only legacy receipt reconciliation and one-way runtime activation.

Both calls require the complete enabled-role set and explicit trusted routes.
No model-supplied route/manifest grants authority. See docs/shared-records.md
for the schema, lock boundary and interrupted-apply contract.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import re

from .admission import role_exclusion
from .errors import Denied, LimitExceeded
from .fs import MAX_FRAME, atomic_write, canonical, digest, identifier, loads_finite, lock, safe_read
from .insights import (DECISION_RECEIPT_BYTES, INSIGHT_DECISION_POLICY,
                       MAX_DECISION_EVENT_BYTES)
from .shared_records import RECEIPT_BYTES, RECORD_BYTES, SharedRecords
from .smoke_refs import record_lock

_ACTIONS = {"accept", "modify", "reject", "defer"}


def _routes(routes, enabled_roles):
    if (type(enabled_roles) not in (list, tuple) or type(routes) is not dict
            or any(not isinstance(role, str) for role in enabled_roles)):
        raise Denied("Expected explicit enabled roles and decision routes")
    for role in enabled_roles:
        identifier(role)
    if len(set(enabled_roles)) != len(enabled_roles) or set(routes) != set(enabled_roles):
        raise Denied("Decision routes must name every enabled role exactly")
    normalized = {}
    for role in sorted(enabled_roles):
        route = routes[role]
        if (type(route) is not dict or set(route) != {"actions", "sources"}
                or type(route["actions"]) not in (list, tuple)
                or any(not isinstance(action, str) or action not in _ACTIONS
                       for action in route["actions"])
                or len(set(route["actions"])) != len(route["actions"])
                or route["sources"] not in ("own", "any")):
            raise Denied("Invalid explicit decision route")
        normalized[role] = {"actions": sorted(route["actions"]), "sources": route["sources"]}
    _bounded(normalized)
    return normalized


def _bounded(value):
    raw = canonical(value)
    if len(raw) > MAX_FRAME:
        raise LimitExceeded("Decision reconciliation exceeds MAX_FRAME")
    return raw


@contextmanager
def _boundary(insights, routes):
    # Runtime takes its role exclusion before source/ack locks. Take all roles
    # nonblocking *before* record_lock, never wait for a live run to finish.
    with ExitStack() as stack:
        for role in routes:
            stack.enter_context(lock(role_exclusion(insights.root, role), blocking=False))
        stack.enter_context(record_lock(insights.root))
        for role in routes:
            stack.enter_context(insights._decision_receipt_lock(role))
        yield


def _scan(insights, routes):
    envelopes = insights._decision_projection_envelopes_locked(limit=512)
    store = SharedRecords(insights.root, {"insight.decision": INSIGHT_DECISION_POLICY})
    sources = {}
    for rid, envelope in sorted(envelopes.items()):
        payload = envelope["payload"]
        # Include exact immutable history and current insight/decision, not only
        # the projected fields. Even non-payload source movement invalidates apply.
        insight_name = f"inbox/{payload['insight']}.json"
        try:
            (insights.root / insight_name).lstat()
        except FileNotFoundError:
            insight_raw = canonical(insights.read(payload["insight"]))
        else:
            insight_raw = safe_read(insights.root, insight_name, MAX_DECISION_EVENT_BYTES)
        sources[rid] = {
            "record_digest": digest(canonical(envelope)),
            "history_digest": digest(safe_read(insights.root,
                f"decision-history/{payload['event']}.json", MAX_DECISION_EVENT_BYTES)),
            "insight_digest": digest(insight_raw),
            "decision_digest": digest(safe_read(insights.root,
                f"decisions/{payload['insight']}.json", MAX_DECISION_EVENT_BYTES)),
        }
        # Preflight never creates projections, but validates any existing one.
        raw = store._read(store._record_name(rid), RECORD_BYTES)
        if raw is not None:
            store.get(rid, expected_digest=sources[rid]["record_digest"])

    legacy = {}
    roles = {}
    for role, route in routes.items():
        current, received, shared = [], [], []
        shared_digests = {}
        legacy[role] = {}
        for rid, envelope in sorted(envelopes.items()):
            payload = envelope["payload"]
            if (payload["action"] not in route["actions"]
                    or (route["sources"] == "own" and payload["source"] != role)):
                continue
            current.append(rid)
            event = dict(payload, ack_token=insights._decision_token(role, payload))
            present = insights._decision_received(role, event)
            receipt_digest = None
            if present:
                received.append(rid)
                receipt_digest = digest(safe_read(insights.root,
                    f"decision-receipts/{role}/{event['ack_token']}.json", DECISION_RECEIPT_BYTES))
            legacy[role][rid] = {"ack_token": event["ack_token"],
                                 "receipt_digest": receipt_digest}
            # received() requires a projection. If absent, a receipt must also
            # be absent; refuse an orphan rather than creating to inspect it.
            if store._read(store._record_name(rid), RECORD_BYTES) is None:
                if store._read(store._receipt_name(role, rid), RECEIPT_BYTES) is not None:
                    raise Denied("Shared decision receipt has no immutable record")
            elif store.received(role, rid, sources[rid]["record_digest"]):
                shared.append(rid)
                shared_digests[rid] = digest(store._read(store._receipt_name(role, rid), RECEIPT_BYTES))
        if not set(shared) <= set(received):
            raise Denied("Shared decision receipts exceed exact legacy receipts")
        pending = sorted(set(current) - set(received))
        roles[role] = {"current_ids": current, "legacy_received_ids": received,
                       "legacy_pending_ids": pending, "shared_received_ids": shared,
                       "shared_pending_ids": sorted(set(current) - set(shared)),
                       "shared_receipt_digests": shared_digests,
                       "post_map_pending_ids": pending, "post_map_parity": True}
    boundary = {"routes": routes, "route_digest": digest(canonical(routes)),
                "sources": sources, "legacy_receipts": legacy}
    return boundary, roles, envelopes, store


def _plan(boundary, roles):
    body = {"schema": 1, "boundary": boundary, "roles": roles}
    _bounded(body)
    # Identity pins input authority, not progress in the append-only output.
    result = dict(body, plan_digest=digest(canonical({"schema": 1, "boundary": boundary})))
    _bounded(result)
    return result


def _bound_post_report(boundary, roles):
    post_roles = {role: dict(report, shared_received_ids=report["legacy_received_ids"],
                            shared_pending_ids=report["legacy_pending_ids"],
                            shared_receipt_digests={rid: "0" * 64 for rid in report["legacy_received_ids"]})
                  for role, report in roles.items()}
    _plan(boundary, post_roles)  # Same serialized size as the final report.


def preflight_decision_receipts(insights, routes, *, enabled_roles) -> dict:
    """Return a deterministic pinned plan/report without writing any receipts.

    Routes and enabled_roles are trusted caller-owned configuration snapshots,
    required even when empty. No projections are written. Busy, corrupt stores,
    mismatch or overflow refuses, without repair or runtime cutover.
    """
    routes = _routes(routes, enabled_roles)
    with _boundary(insights, routes):
        _require_legacy(insights)
        boundary, roles, _, _ = _scan(insights, routes)
        _bound_post_report(boundary, roles)
        return _plan(boundary, roles)


def apply_decision_receipts(insights, plan, routes, *, enabled_roles) -> dict:
    """Apply an exact plan against freshly supplied trusted configuration.

    An I/O failure may leave an immutable projection/receipt prefix. Exact retry
    (including restart) permits any existing shared subset of legacy received;
    shared state is deliberately excluded from the pinned boundary identity.
    """
    routes = _routes(routes, enabled_roles)
    _validate_plan(plan)
    with _boundary(insights, routes):
        _require_legacy(insights)
        return _apply_locked(insights, plan, routes)


def _validate_plan(plan):
    if (type(plan) is not dict or set(plan) != {"schema", "boundary", "roles", "plan_digest"}
            or type(plan["schema"]) is not int or plan["schema"] != 1):
        raise Denied("Invalid decision reconciliation plan")
    _bounded(plan)
    if digest(canonical({"schema": 1, "boundary": plan["boundary"]})) != plan["plan_digest"]:
        raise Denied("Decision reconciliation plan digest mismatch")


def _apply_locked(insights, plan, routes):
    boundary, roles, envelopes, store = _scan(insights, routes)
    # Bound all reports before any writes, including the post-map report.
    _plan(boundary, roles)
    if boundary != plan["boundary"]:
        raise Denied("Decision reconciliation boundary changed")
    # Original shared receipts must remain unchanged. Newly appearing
    # receipts are allowed only within the legacy-acknowledged set (_scan).
    if type(plan["roles"]) is not dict or set(plan["roles"]) != set(roles):
        raise Denied("Invalid decision reconciliation role report")
    for role, report in roles.items():
        original = plan["roles"][role]
        if type(original) is not dict or type(original.get("shared_receipt_digests")) is not dict:
            raise Denied("Invalid decision reconciliation role report")
        original_shared = original["shared_receipt_digests"]
        expected = dict(report, shared_received_ids=sorted(original_shared),
                        shared_pending_ids=sorted(set(report["current_ids"]) - set(original_shared)),
                        shared_receipt_digests=original_shared)
        if (original != expected or not set(original_shared) <= set(report["legacy_received_ids"])
                or any(report["shared_receipt_digests"].get(rid) != computed
                       for rid, computed in original_shared.items())):
            raise Denied("Existing shared decision receipt or report changed")
    _bound_post_report(boundary, roles)
    # Only routed, legacy-acknowledged identities require a projection/write.
    for role, report in roles.items():
        ids = report["legacy_received_ids"]
        if not ids:
            continue
        for rid in ids:
            store.put(envelopes[rid], writer="host.insights")
        manifest = store.freeze(role, ids)
        missing = set(ids) - set(report["shared_received_ids"])
        store.acknowledge(role, [asdict(entry) for entry in manifest
                                 if entry.record_id in missing], manifest=manifest)
    final_boundary, verified, _, _ = _scan(insights, routes)
    if final_boundary != boundary:
        raise Denied("Decision reconciliation boundary changed during apply")
    for role, report in verified.items():
        if (report["shared_received_ids"] != report["legacy_received_ids"]
                or report["shared_pending_ids"] != report["legacy_pending_ids"]):
            raise Denied("Decision reconciliation pending-set mismatch after apply")
    return _plan(boundary, verified)


def _require_legacy(insights):
    from .decision_delivery import authority
    if authority(insights.root) is not None:
        raise Denied("Decision delivery is shared; legacy reconciliation is closed")


def activate_decision_delivery(project):
    """Host-only, one-way cutover using a trusted project/config snapshot.

    Quiesce every enabled role before source and receipt locks. Re-run exact
    preflight, mapping and full pending parity inside that same boundary, then
    publish a create-only marker last. A crash before publication leaves legacy
    authoritative and an idempotent prefix. A retry after publication returns
    the marker (never reimports legacy receipts). No model tool calls this API.
    """
    from .decision_delivery import AUTHORITY_FILE, PLAN_FILE, REPORT_FILE, authority, page_bytes
    insights = project.insights
    enabled = list(project.roles)
    routes = _routes({name: {"actions": list(project.config.roles[name].decision_events),
                            "sources": project.config.roles[name].decision_sources}
                      for name in enabled}, enabled)
    for name in enabled:
        if routes[name]["actions"]:
            page_bytes(project.config.roles[name])
    with _boundary(insights, routes):
        marker = authority(insights.root)
        if marker is not None:
            return marker
        boundary, roles, _, store = _scan(insights, routes)
        _bound_post_report(boundary, roles)
        # The shadow pilot has no authority to hide even a route-excluded or
        # stale event. Refuse unexplained decision receipts anywhere, including
        # disabled recipients, rather than importing uncertain state.
        allowed = {store._receipt_name(role, rid)
                   for role, report in roles.items() for rid in report["legacy_received_ids"]}
        base = insights.root / "shared-record-receipts"
        if store._parents("shared-record-receipts/probe"):
            for directory in base.iterdir():
                if not store._parents(directory.relative_to(insights.root).as_posix() + "/probe"):
                    raise Denied("Unsafe shared receipt directory")
                for path in directory.iterdir():
                    name = path.relative_to(insights.root).as_posix()
                    raw = store._read(name, RECEIPT_BYTES)
                    if re.fullmatch(r"\.new-[a-z0-9_]{8}", path.name):
                        # Atomic-write crash residue is not a committed receipt.
                        # Still safe-read it: never ignore an unsafe entry.
                        continue
                    receipt = loads_finite(raw)
                    if type(receipt) is not dict or not isinstance(receipt.get("record_id"), str):
                        raise Denied("Uncertain shared receipt inventory")
                    if receipt["record_id"].startswith("insight.decision/") and name not in allowed:
                        raise Denied("Unexplained shared-only decision receipt")
        try:
            (insights.root / PLAN_FILE).lstat()
        except FileNotFoundError:
            plan = _plan(boundary, roles)
            atomic_write(insights.root / PLAN_FILE, canonical(plan), exclusive=True)
        else:
            plan = loads_finite(safe_read(insights.root, PLAN_FILE, MAX_FRAME))
            _validate_plan(plan)
        report = _apply_locked(insights, plan, routes)
        try:
            (insights.root / REPORT_FILE).lstat()
        except FileNotFoundError:
            atomic_write(insights.root / REPORT_FILE, canonical(report), exclusive=True)
        else:
            if safe_read(insights.root, REPORT_FILE, MAX_FRAME) != canonical(report):
                raise Denied("Decision activation report changed before marker")
        marker = {"schema": 1, "kind": "insight.decision", "authority": "shared",
                  "plan_digest": report["plan_digest"], "report_digest": digest(canonical(report))}
        atomic_write(insights.root / AUTHORITY_FILE, canonical(marker), exclusive=True)
        return authority(insights.root)
