"""Userland projection of authoritative records into declared kernel objects.

The legacy insight, decision and operator-record stores keep authority. This
module only maps their facts onto kernel kinds declared by the deployment; it
never reads a kernel object to decide legacy behaviour. A projection failure
is therefore a derived-index failure, not a loss of the accepted record.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Mapping

from .errors import Denied
from .fs import canonical, digest
from .shared_records import RECORD_NAME_MAX_CHARS

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
_ACTIONS = frozenset(("accept", "modify", "defer", "reject", "withdraw"))
_STATES = {"accept": "accepted", "modify": "modified", "defer": "deferred",
           "reject": "rejected", "withdraw": "withdrawn"}
_FROM_STATES = {"accept": ("pending", "deferred"), "modify": ("pending", "deferred"),
                "defer": ("pending",), "reject": ("pending", "deferred"),
                "withdraw": ("pending", "deferred")}
MAX_SHADOW_FAILURES = 32


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


def insight_object_id(record: Mapping[str, Any]) -> str:
    """Map one legal legacy insight ID to the kernel's fixed 32-hex identity."""
    legacy = record["id"]
    if not isinstance(legacy, str) or not legacy:
        raise Denied("Invalid insight projection identity")
    if _HEX_ID.fullmatch(legacy):
        return legacy
    return digest(canonical(["kernel-shadow", "insight", legacy]))[:32]


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
    fields = {"insight": insight_object_id(record), "revision": record["rev"],
              "action": record["action"], "reason": record["reason"]}
    scope = record.get("scope")
    if scope is not None:
        fields["scope"] = scope
    revisit = instant_value(record.get("revisit"))
    if revisit is not None:
        fields["revisit"] = revisit
    return fields


def notice_object_id(record: Mapping[str, Any]) -> str:
    """Map one accepted operator notice ID to a stable kernel object ID."""
    legacy = record["id"]
    if not isinstance(legacy, str) or not legacy:
        raise Denied("Invalid notice projection identity")
    return digest(canonical(["kernel-shadow", "notice", legacy]))[:32]


def notice_fields(record: Mapping[str, Any], computed: str) -> dict:
    """Map the declared subset of one operator notice to kernel object fields."""
    payload = record["payload"]
    fields = {"subject_digest": computed, "kind": record["kind"], "body": payload["body"]}
    target = payload.get("target")
    subject = record.get("subject")
    source = target if target is not None else subject
    reference = source.get("id") if isinstance(source, Mapping) else None
    # The field is optional: an oversized reference is omitted rather than
    # silently truncated into a different identity.
    if isinstance(reference, str) and len(reference) <= RECORD_NAME_MAX_CHARS:
        fields["reference"] = reference
    return fields


def insight_question_object_id(record: Mapping[str, Any]) -> str:
    """Hash one insight submission's identity into a kernel question ID."""
    legacy = record["id"]
    revision = record.get("rev", 1)
    if (not isinstance(legacy, str) or not legacy
            or type(revision) is not int or revision < 1):
        raise Denied("Invalid question projection identity")
    return digest(canonical(["kernel-shadow", "question", "insight", legacy, revision]))[:32]


def record_question_object_id(record: Mapping[str, Any], computed: str) -> str:
    """Hash one role question's ID and content digest into a kernel question ID."""
    legacy = record["id"]
    if (not isinstance(legacy, str) or not legacy
            or not isinstance(computed, str) or not computed):
        raise Denied("Invalid question projection identity")
    return digest(canonical(["kernel-shadow", "question", "record", legacy, computed]))[:32]


def insight_question_fields(record: Mapping[str, Any]) -> dict:
    """Map an operator-addressed insight to the declared question fields."""
    fields = {"subject_type": "insight", "subject_ref": insight_object_id(record),
              "subject_rev": record.get("rev", 1), "body": record["body"]}
    urgency = record.get("urgency")
    if urgency is not None:
        fields["urgency"] = urgency
    return fields


def record_question_fields(record: Mapping[str, Any], computed: str) -> dict:
    """Map one role question envelope to the declared question fields."""
    payload = record["payload"]
    body = payload.get("body") if isinstance(payload, Mapping) else None
    if not isinstance(body, str) or not body.strip():
        raise Denied("Role question has no representable body")
    fields = {"subject_type": "record", "record_kind": record["kind"],
              "record_id": record["id"], "record_digest": computed, "body": body}
    urgency = payload.get("urgency")
    if urgency is not None:
        fields["urgency"] = urgency
    return fields


def answer_object_id(record: Mapping[str, Any]) -> str:
    """Map one accepted legacy answer ID to a stable kernel object ID."""
    legacy = record["id"]
    if not isinstance(legacy, str) or not legacy:
        raise Denied("Invalid answer projection identity")
    return digest(canonical(["kernel-shadow", "answer", legacy]))[:32]


def answer_fields(record: Mapping[str, Any], question_id: str, revision: int) -> dict:
    """Map one accepted answer to the declared create-only answer fields."""
    payload = record["payload"]
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        raise Denied("Answer has no representable body")
    fields = {"question": question_id, "revision": revision,
              "action": payload["action"], "body": body}
    # The field is optional: an oversized reference is omitted rather than
    # silently truncated into a different identity.
    reference = record["id"]
    if isinstance(reference, str) and len(reference) <= RECORD_NAME_MAX_CHARS:
        fields["reference"] = reference
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
        self.failures: list[dict] = []

    def note_failure(self, operation: str, insight_id: str, exc: BaseException) -> None:
        """Retain a bounded process-local record of a derived projection failure."""
        reason = str(exc)[:256]
        self.failures.append({"operation": operation, "insight": insight_id,
                              "error": type(exc).__name__,
                              "reason": reason if str(exc) == reason else reason[:253] + "..."})
        del self.failures[:-MAX_SHADOW_FAILURES]

    def clear_failures(self, insight_id: str) -> None:
        self.failures = [row for row in self.failures if row["insight"] != insight_id]

    def verify(self, insight: Mapping[str, Any], decision: Mapping[str, Any] | None = None) -> dict:
        """Compare one authoritative fact pair with its derived kernel objects."""
        insight_id = insight["id"]
        result = {"insight": insight_id, "revision": insight.get("rev", 1),
                  "ok": True, "mismatches": []}
        mismatches = result["mismatches"]

        def mismatch(text: str) -> None:
            mismatches.append(text)

        try:
            current = self.objects.read("insight", insight_object_id(insight))
        except Denied as exc:
            mismatch(f"insight object unavailable: {type(exc).__name__}")
            current = None
        if current is not None:
            for key, value in insight_fields(insight).items():
                if current["fields"].get(key) != value:
                    mismatch(f"insight field {key} differs")
            action = decision.get("action") if decision else None
            expected_state = _STATES.get(action) if action in _ACTIONS else None
            defer_without_instant = (action == "defer"
                                     and instant_value(decision.get("revisit")) is None)
            if (expected_state is not None and not defer_without_instant
                    and current["state"] != expected_state):
                mismatch(f"insight state is {current['state']}, expected {expected_state}")
            if action == "defer":
                instant = instant_value(decision.get("revisit"))
                if instant is not None and current["fields"].get("revisit_at") != instant:
                    mismatch("insight revisit_at differs")
            if action == "withdraw" and current["fields"].get("revisit_at") is not None:
                mismatch("withdrawn insight still has a revisit instant")

        action = decision.get("action") if decision else None
        if decision is not None and action in _ACTIONS:
            object_id = decision_object_id(decision)
            expected = decision_fields(decision)
            try:
                recorded = self.objects.read("decision", object_id)
            except Denied as exc:
                mismatch(f"decision object {object_id} unavailable: {type(exc).__name__}")
            else:
                if recorded["fields"] != expected:
                    mismatch(f"decision object {object_id} fields differ")
        result["ok"] = not mismatches
        return result

    def submitted(self, record: Mapping[str, Any]) -> None:
        object_id = insight_object_id(record)
        if self.objects.revisions("insight", object_id):
            return
        self.objects.create("insight", insight_fields(record), principal(record["source"]),
                            object_id=object_id)

    def revised(self, record: Mapping[str, Any]) -> None:
        object_id = insight_object_id(record)
        self.submitted(record)
        current = self.objects.read("insight", object_id)
        fields = insight_fields(record)
        updatable = {key: fields[key]
                     for key in ("title", "body", "urgency", "holder") if key in fields}
        if all(current["fields"].get(key) == value for key, value in updatable.items()):
            return
        # Kernel revisions also record state transitions, so the legacy revision
        # number is content identity here, not the kernel object's revision.
        self.objects.transition("insight", object_id, "revise",
                                expected_rev=current["rev"], fields=updatable,
                                actor=principal(record["source"]))

    def _create_once(self, kind: str, object_id: str, fields: dict, label: str) -> bool:
        """Create one derived object or verify an exact replay of its content."""
        if self.objects.revisions(kind, object_id):
            existing = self.objects.read(kind, object_id)
            if existing["fields"] != fields:
                raise Denied(f"{label} projection id was reused with different content")
            return False
        self.objects.create(kind, fields, "host", object_id=object_id)
        return True

    def noticed(self, record: Mapping[str, Any], computed: str) -> None:
        """Mirror one accepted operator notice as a create-only kernel object."""
        self._create_once("notice", notice_object_id(record),
                          notice_fields(record, computed), "Notice")

    def questioned(self, record: Mapping[str, Any]) -> None:
        """Mirror one operator-addressed insight submission as a question."""
        if record.get("addressee") != "operator":
            return
        self._create_once("question", insight_question_object_id(record),
                          insight_question_fields(record), "Insight question")

    def questioned_record(self, record: Mapping[str, Any], computed: str) -> None:
        """Mirror one accepted role record question as a kernel question."""
        self._create_once("question", record_question_object_id(record, computed),
                          record_question_fields(record, computed), "Record question")

    def answered(self, record: Mapping[str, Any],
                 insight: Mapping[str, Any] | None = None) -> None:
        """Mirror one accepted answer, then mark a published question answered."""
        subject = record["subject"]
        if not isinstance(subject, Mapping):
            raise Denied("Invalid answer projection subject")
        if subject.get("type") == "insight":
            if insight is None or insight.get("id") != subject.get("id"):
                raise Denied("Answer question insight is unavailable")
            revision = subject["rev"]
            question_id = insight_question_object_id({**insight, "rev": revision})
        elif subject.get("type") == "record":
            revision = 1
            question_id = record_question_object_id(subject, subject["digest"])
        else:
            raise Denied("Invalid answer projection subject")
        fields = answer_fields(record, question_id, revision)
        # The create-only answer is established before the mutable question
        # state, so a rejected answer field cannot leave a half-written
        # projection. A replay still reaches the transition below so a failed
        # transition can be repaired without rewriting the answer object.
        self._create_once("answer", answer_object_id(record), fields, "Answer")
        if record["payload"]["action"] != "publish":
            return
        question = self.objects.read("question", question_id)
        if question["state"] != "pending":
            return
        self.objects.transition("question", question_id, "answer",
                                expected_rev=question["rev"], fields=None, actor="host")

    def decided(self, insight: Mapping[str, Any], record: Mapping[str, Any]) -> None:
        action = record.get("action")
        if action not in _ACTIONS:
            return
        self.submitted(insight)
        # The create-only decision record is established before the mutable
        # insight state, so a rejected decision field cannot leave a half-written
        # projection. Replays still reach the state transition below.
        decision_id = decision_object_id(record)
        fields = decision_fields(record)
        if self.objects.revisions("decision", decision_id):
            existing = self.objects.read("decision", decision_id)
            if existing["fields"] != fields:
                raise Denied("Decision projection id was reused with different content")
        else:
            self.objects.create("decision", fields, "host", object_id=decision_id)
        object_id = insight_object_id(insight)
        current = self.objects.read("insight", object_id)
        if current["state"] not in _FROM_STATES.get(action, ()):
            return
        state_fields = {}
        if action == "defer":
            instant = instant_value(record.get("revisit"))
            if instant is None:
                return  # the decision is recorded; the state needs an instant
            state_fields["revisit_at"] = instant
        if action == "withdraw" and current["fields"].get("revisit_at") is not None:
            state_fields["revisit_at"] = None  # clear the superseded revisit timer
        actor = (principal(insight["source"]) if action == "withdraw"
                 else current["fields"].get("holder",
                                            principal(insight.get("addressee", "writer"))))
        self.objects.transition("insight", object_id, action,
                                expected_rev=current["rev"], fields=state_fields, actor=actor)
