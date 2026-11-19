"""Userland projection of authoritative records into declared kernel objects.

The legacy insight and decision stores keep authority. This module only maps
their recorded facts onto the generic kernel kinds declared by the deployment;
it never reads a kernel object to decide legacy behaviour. A projection failure
is therefore a derived-index failure, not a loss of the accepted record.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Mapping

from .errors import Denied
from .fs import canonical, digest

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
_ACTIONS = frozenset(("accept", "modify", "defer", "reject", "withdraw"))


def principal(value: Any) -> str:
    """Map one trusted legacy authority name to a kernel principal."""
    if value in ("host", "operator"):
        return value
    if isinstance(value, str) and value.startswith("role:"):
        name = value[5:]
    else:
        name = value
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise Denied("Unrepresentable kernel principal")
    return f"role:{name}"


def insight_fields(record: Mapping[str, Any]) -> dict:
    """Map the declared subset of one insight to kernel object fields."""
    fields = {"title": record["title"], "body": record["body"],
              "holder": principal(record.get("addressee", "writer"))}
    urgency = record.get("urgency")
    if urgency is not None:
        fields["urgency"] = urgency
    return fields


def instant_value(value: Any) -> str | None:
    """Return an aware ISO instant from an exact value or a bounded revisit phrase."""
    if not isinstance(value, str) or not value:
        return None
    for candidate in (value, *value.split()):
        if len(candidate) > 64:
            continue
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return candidate
    return None


def decision_fields(record: Mapping[str, Any]) -> dict:
    """Map one recorded decision to the declared kernel decision fields."""
    if record.get("action") not in _ACTIONS:
        raise Denied("Action is not a kernel decision")
    fields = {"insight": record["id"], "revision": record["rev"],
              "action": record["action"], "reason": record["reason"]}
    scope = record.get("scope")
    if scope is not None:
        fields["scope"] = scope
    revisit = instant_value(record.get("revisit"))
    if revisit is not None:
        fields["revisit"] = revisit
    return fields


def decision_object_id(record: Mapping[str, Any]) -> str:
    """Use the legacy key for one substantive decision, content for other events."""
    if record.get("action") in ("accept", "modify", "reject"):
        key = record.get("decision_key")
        if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{32}", key):
            return key
        raise Denied("Invalid decision projection key")
    return digest(canonical([record["id"], record["rev"], record["action"],
                             record.get("reason"), record.get("revisit"),
                             record.get("created_at")]))[:32]


class KernelShadow:
    """Mirror accepted legacy facts into the kernel without owning them."""

    def __init__(self, objects):
        self.objects = objects

    def submitted(self, record: Mapping[str, Any]) -> None:
        object_id = record["id"]
        if self.objects.revisions("insight", object_id):
            return
        self.objects.create("insight", insight_fields(record), principal(record["source"]),
                            object_id=object_id)

    def revised(self, record: Mapping[str, Any]) -> None:
        object_id = record["id"]
        if record.get("rev", 1) < 2:
            return
        self.submitted(record)
        current = self.objects.read("insight", object_id)
        fields = insight_fields(record)
        updatable = {key: fields[key] for key in ("title", "body", "urgency") if key in fields}
        if all(current["fields"].get(key) == value for key, value in updatable.items()):
            return
        # Kernel revisions also record state transitions, so the legacy revision
        # number is content identity here, not the kernel object's revision.
        self.objects.transition("insight", object_id, "revise",
                                expected_rev=current["rev"], fields=updatable,
                                actor=principal(record["source"]))

    def decided(self, insight: Mapping[str, Any], record: Mapping[str, Any]) -> None:
        action = record.get("action")
        if action not in _ACTIONS:
            return
        self.submitted(insight)
        current = self.objects.read("insight", insight["id"])
        if current["state"] == "pending":
            fields = {}
            if action == "defer":
                instant = instant_value(record.get("revisit"))
                if instant is None:
                    fields = None  # the decision still projects; the state needs an instant
                else:
                    fields["revisit_at"] = instant
            if fields is not None:
                actor = (principal(insight["source"]) if action == "withdraw"
                         else principal(insight.get("addressee", "writer")))
                self.objects.transition("insight", insight["id"], action,
                                        expected_rev=current["rev"], fields=fields, actor=actor)
        object_id = decision_object_id(record)
        fields = decision_fields(record)
        if self.objects.revisions("decision", object_id):
            existing = self.objects.read("decision", object_id)
            if existing["fields"] != fields:
                raise Denied("Decision projection id was reused with different content")
            return
        self.objects.create("decision", fields, "host", object_id=object_id)
