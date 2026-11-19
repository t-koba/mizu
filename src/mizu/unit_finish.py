"""Own unit-finish validation, acknowledgement selection, writer-parking evidence and progress records."""
from __future__ import annotations

import datetime as dt

from . import (check_jobs, ci_waits, decision_delivery, operator_records, proposal_waits,
               queue_decision_delivery, queue_record_delivery, reviews, work_item_events)
from . import wait_events as structured_wait_events
from .config import role_progress_signals
from .errors import Busy, Denied, LimitExceeded
from .fs import DIGEST, canonical, digest, now, write_json
from .snapshot import (PROGRESS_SIGNALS, WRITER_HANDOFF_PROGRESS_SIGNALS,
                       progress_disposition, validate_next_step, validate_progress)
from .work_items import MAX_OPEN_PAGE_ROWS


def _progress_obligations_digest(obligations) -> str:
    """Digest only stable identity fields for the bounded pending-obligation view."""
    rows = []
    for item in obligations or ():
        if not isinstance(item, dict):
            continue
        rows.append({key: item.get(key) for key in ("insight", "rev", "registered_at", "requires")})
    rows.sort(key=lambda item: (str(item.get("registered_at") or ""),
                                str(item.get("insight") or ""),
                                int(item.get("rev") or 0)))
    return digest(canonical(rows))


#: Sentinel for _progress_record callers that carry no ready-work observation.
#: Omission keeps legacy shapes for direct callers; run completion supplies
#: the prompt-bound signature explicitly and produces schema 3.
_READY_WORK_UNSET: object = object()


def _progress_input_unchanged(previous: dict, signals: dict, obligations_digest: str,
                              ready_work, ready_work_signature, wait_resolution_tokens,
                              wait_resolution_high_water) -> bool:
    """Whether a finish observes the same bounded input facts as ``previous``.

    Outcome is deliberately absent: a wait or blocked finish carries the same
    input evidence as a continuation, so only changed observations start a new
    streak. Fields omitted by legacy callers cannot prove a change and are
    ignored.
    """
    if previous.get("obligations_digest") != obligations_digest:
        return False
    if any(previous["signals"][name] != bool(signals.get(name, False))
           for name in PROGRESS_SIGNALS):
        return False
    if ("ready_work" in previous and ready_work is not _READY_WORK_UNSET
            and previous["ready_work"] != ready_work):
        return False
    if ("ready_work_signature" in previous and ready_work_signature is not _READY_WORK_UNSET
            and previous["ready_work_signature"] != ready_work_signature):
        return False
    if ("wait_resolution_tokens" in previous and wait_resolution_tokens is not _READY_WORK_UNSET
            and previous["wait_resolution_tokens"] != list(wait_resolution_tokens)):
        return False
    if ("wait_resolution_high_water" in previous
            and wait_resolution_high_water is not _READY_WORK_UNSET
            and previous["wait_resolution_high_water"] != dict(wait_resolution_high_water)):
        return False
    return True


def _progress_record(snapshot: dict, role_name: str, selected: tuple[str, ...],
                    signals: dict[str, bool], outcome: str, *, obligations_digest: str | None = None,
                    ready_work: bool | None | object = _READY_WORK_UNSET,
                    ready_work_signature: str | None | object = _READY_WORK_UNSET,
                    wait_resolution_tokens: list[str] | object = _READY_WORK_UNSET,
                    wait_resolution_high_water: dict | object = _READY_WORK_UNSET) -> dict:
    """Build compact unit evidence and its bounded consecutive no-progress count.

    Every finish outcome with no selected progress signal and unchanged input
    advances the streak; progress or a genuine input change restarts it. The
    outcome never decides whether input changed.

    ``ready_work`` is the bounded ``page_ready(limit=1)`` existence probe
    taken at writer run completion (True/False) or None when that probe was
    unreadable. An explicit value produces a schema-2 record; omission keeps
    the legacy schema-1 shape for direct callers without an observation.
    An explicit ready_work_signature produces schema 3 and binds the ready-set
    event baseline. It normally reflects the unit input; a validated writer
    parking finish may reanchor it to the complete empty finish-time inventory
    so the unit's own dispositions do not trigger a redundant wake.
    Schema 4 also records the bounded exact wait-resolution causes offered to
    the writer. Schema 5 adds a proof-set-bound shown prefix for overflow; neither
    receipt admits an old proof on an unrelated ready-set change. Historical
    schemas 6–7 remain readable but new records never carry wait conversions.
    """
    obligations_digest = obligations_digest or _progress_obligations_digest(())
    has_progress = any(signals[name] for name in selected)
    previous = snapshot.get("progress")
    previous_count = 0
    if isinstance(previous, dict):
        try:
            validate_progress(previous)
            if (previous["role"] == role_name and set(previous["selected"]) == set(selected)
                    and _progress_input_unchanged(
                        previous, signals, obligations_digest, ready_work, ready_work_signature,
                        wait_resolution_tokens, wait_resolution_high_water)):
                previous_count = previous["no_progress_count"]
        except Denied:
            # Snapshot loading validates persisted records; direct callers with
            # malformed records rebase rather than trusting their counter.
            previous_count = 0
    # 101 is one above the largest allowed continuation policy and is
    # sufficient to preserve the parked disposition without unbounded growth.
    count = 0 if has_progress else min(101, previous_count + 1)
    if (wait_resolution_high_water is not _READY_WORK_UNSET
            and wait_resolution_tokens is _READY_WORK_UNSET):
        raise Denied("Wait-resolution high water requires exact delivery tokens")
    if (wait_resolution_tokens is not _READY_WORK_UNSET
            and ready_work_signature is _READY_WORK_UNSET):
        raise Denied("Wait-resolution delivery requires a ready-work observation")
    if ready_work_signature is not _READY_WORK_UNSET and ready_work is _READY_WORK_UNSET:
        ready_work = None
    if ready_work is _READY_WORK_UNSET:
        record = {"schema": 1, "signals": {name: bool(signals.get(name, False))
                                            for name in PROGRESS_SIGNALS},
                  "selected": list(selected), "role": role_name,
                  "has_progress": has_progress, "no_progress_count": count,
                  "obligations_digest": obligations_digest,
                  "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")}
        return validate_progress(record)
    if ready_work is not None and type(ready_work) is not bool:
        raise Denied("Invalid ready-work observation")
    record = {"schema": 2, "signals": {name: bool(signals.get(name, False))
                                        for name in PROGRESS_SIGNALS},
              "selected": list(selected), "role": role_name,
              "has_progress": has_progress, "no_progress_count": count,
              "obligations_digest": obligations_digest,
              "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
              "ready_work": ready_work}
    if ready_work_signature is not _READY_WORK_UNSET:
        record.update(schema=3, ready_work_signature=ready_work_signature)
    if wait_resolution_tokens is not _READY_WORK_UNSET:
        record.update(schema=4, wait_resolution_tokens=wait_resolution_tokens)
    if wait_resolution_high_water is not _READY_WORK_UNSET:
        record.update(schema=5, wait_resolution_high_water=wait_resolution_high_water)
    return validate_progress(record)


def _writer_next_task(project, snapshot, role_name) -> str | None:
    """Resolve one durable writer handoff from the exact published parent pair.

    Only continue/bounded work with progress selected by the current role policy
    may declare readiness. Changed policy and malformed evidence fail closed.
    Repeating the previous typed step is not a new declaration; publishing the
    successor consumes this handoff. Reads at most one parent and never changes
    WorkItems or wait authority.
    """
    if snapshot.get("outcome") not in ("continue", "bounded"):
        return None
    progress = snapshot.get("progress")
    try:
        step = validate_next_step(snapshot.get("next_step"))
        if not isinstance(progress, dict):
            return None
        selected, signals = progress.get("selected"), progress.get("signals")
        if (not isinstance(selected, list) or not isinstance(signals, dict)
                or not any(name in selected and signals.get(name) is True
                           for name in WRITER_HANDOFF_PROGRESS_SIGNALS)
                or progress_disposition(
                    progress, snapshot["outcome"], project.config, role_name) != "progress_requires_next"):
            return None
        parent_id = snapshot.get("parent")
        if not parent_id:
            return None
        parent = project.snapshots.get(parent_id)
        return step if step != parent.get("next_step") else None
    except (Denied, OSError, ValueError):
        return None


def _resolved_wait_row(row) -> bool:
    """Whether one ready row carries an externally resolved wait."""
    if not isinstance(row, dict) or row.get("wait_satisfied") is True:
        return True
    liveness = row.get("wait_liveness") if isinstance(row, dict) else None
    return (isinstance(liveness, dict)
            and liveness.get("state") in ("due", "terminal"))


class ContextFinishMixin:
    def _finish_progress(self, captured, finished, *, ready_work=None):
        return _progress_record(
            self.snapshot, self.role.name, tuple(role_progress_signals(self.config, self.role)),
            {"tool": self.progress_tool,
             "code": captured["code_digest"] != self.snapshot["code_digest"],
             "state": finished["state"] != self.snapshot["state"],
             "decision": self.progress_decision, "insight": self.progress_insight,
             "input": self.progress_input}, finished["outcome"],
            obligations_digest=getattr(self, "progress_obligations_digest", None), ready_work=ready_work,
            ready_work_signature=getattr(self, "ready_work_signature", None),
            wait_resolution_tokens=getattr(self, "wait_resolution_tokens", _READY_WORK_UNSET),
            wait_resolution_high_water=getattr(self, "wait_resolution_high_water", _READY_WORK_UNSET))

    def _check_writer_parking(self, finished):
        """Validate finish-time ready evidence; the host decides parking.

        A host-imposed bounded seal is an automatic yield and may retain ready
        work. A validated next_step handoff is not parking. A wait or blocked
        finish with ready work is accepted: the prompt-bound ready-input
        signature is kept, so the row stays visible but an unchanged retry is
        not a fresh wake. Both admission gates compare that real identity, and
        the unchanged unit counts toward no-progress handling, fresh-session
        recovery and parking until the set changes or an independent wake
        arrives. Other finishes require a complete
        empty role-scoped inventory under the page API maximum. Unknown waits
        and overflow are evidence gaps, never dispositions.
        """
        if finished["outcome"] == "bounded" and self.bound_hit is not None:
            return
        if finished["outcome"] not in ("wait", "blocked", "continue", "bounded"):
            return
        if (finished["outcome"] in ("continue", "bounded")
                and finished.get("next_step") is not None):
            captured = self.project.snapshots.capture_files(self.workspace, persist=False)
            candidate = {**finished, "parent": self.snapshot["id"],
                         "progress": self._finish_progress(captured, finished)}
            if _writer_next_task(self.project, candidate, self.role.name):
                return
        try:
            page = self.project.work_items.page_ready(
                role_name=self.role.name, limit=MAX_OPEN_PAGE_ROWS)
            if (page.get("coverage_complete") is not True or page.get("truncated") is not False
                    or type(page.get("total")) is not int or page["total"] < 0
                    or not isinstance(page.get("items"), list)
                    or len(page["items"]) != page["total"]
                    or not isinstance(page.get("signature"), str)
                    or not DIGEST.fullmatch(page["signature"])):
                raise Denied("Ready inventory is incomplete, unknown, or exceeds the bounded page")
        except (Busy, Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError,
                AttributeError, RecursionError) as exc:
            raise Denied("Ready evidence unavailable or incomplete at finish.") from exc
        if page["total"]:
            if finished["outcome"] in ("wait", "blocked"):
                # Keep the signature captured with this unit's ready input.
                # An unchanged row remains visible but is not fresh progress:
                # both admission gates and the no-progress record compare the
                # real ready identity, so unchanged retries reach the existing
                # recovery threshold and park until a genuine change or wake.
                # A resolved wait is that independent wake: its liveness state
                # is new evidence even when the ready membership is unchanged.
                if any(_resolved_wait_row(row) for row in page["items"]):
                    self.ready_work_signature = None
                return
            ids = ", ".join(str(row.get("id", "unknown"))[:64] for row in page["items"][:3])
            raise Denied(f"{page['total']} ready item(s), including {ids}.")
        # Anchor the next event probe to the validated finish-time empty set,
        # not the opening set whose removals were already handled by this run.
        self.ready_work_signature = page["signature"]

    def _op_finish(self, args: dict) -> dict:
        if args["outcome"] not in self.role.finish_outcomes:
            raise Denied(f"Role {self.role.name} does not allow finish outcome {args['outcome']!r}")
        if not args["summary"].strip():
            raise Denied("A nonempty work summary is required")
        if ("next_step" in args
                or (self.role.workspace == "write" and args["outcome"] == "continue")):
            validate_next_step(args.get("next_step"))
        review = reviews.finish(self, args["review_verdict"]) if "review_verdict" in args else None
        tokens = args.get("ack_events", [])
        decisions = {event["ack_token"]: event for event in self.decision_events}
        terminal = {event["ack_token"]: event for event in self.work_item_events}
        waits = {event["ack_token"]: event for event in self.wait_events
                 if event.get("kind") in structured_wait_events.WAIT_KINDS
                 and "registration_id" in event and "ack_token" in event}
        obligations = {event["ack_token"]: event for event in self.obligations
                       if "registration_id" in event and "ack_token" in event}
        records = {entry.ack_token for entry in self.operator_record_manifest}
        ci_tokens = {entry.ack_token for entry in self.ci_wait_manifest}
        proposal_tokens = {entry.ack_token for entry in self.proposal_wait_manifest}
        delivered = proposal_tokens | records | decisions.keys() | terminal.keys() | waits.keys() | obligations.keys() | ci_tokens
        if (not isinstance(tokens, list) or len(tokens) > 512
                or any(not isinstance(token, str) or len(token) > 64 for token in tokens)
                or len(set(tokens)) != len(tokens)
                or any(token not in delivered for token in tokens)):
            raise Denied("ack_events must select unique event tokens delivered to this role in this unit")
        # Recheck the immutable decision binding, not mutable topic state: the
        # role may have revised/withdrawn a topic as part of handling it.
        decision_tokens = [token for token in tokens if token in decisions]
        authority = decision_delivery.authority_kind(self.project.insights.root)
        if self.shared_decisions:
            decision_delivery.validate(self, decision_tokens)
        elif authority == "queues" and decision_tokens:
            queue_decision_delivery.validate(
                self.project, self.role.name, [decisions[token] for token in decision_tokens])
        selected_ci = [token for token in tokens if token in ci_tokens]
        if selected_ci:
            ci_waits.validate_selection(self, selected_ci)
        proposal_waits.validate_selection(self, [token for token in tokens if token in proposal_tokens])
        record_tokens = [token for token in tokens if token in records]
        if queue_record_delivery.authority_kind(self.project.insights.root) == "queues":
            queue_record_delivery.validate(self, record_tokens)
        else:
            operator_records.validate(self, record_tokens)
        for token in tokens:
            if token in records or token in ci_tokens or token in proposal_tokens:
                continue
            if token in decisions:
                if self.shared_decisions or authority == "queues":
                    continue  # Validated as one bounded batch above.
                if self.project.insights._decision_token(self.role.name, decisions[token]) != token:
                    raise Denied("Decision acknowledgement binding changed")
            elif token in terminal:
                work_item_events.acknowledgement_payload(
                    self.project.work_items, self.role.name, terminal[token])
            elif token in waits:
                structured_wait_events.validate(self.role.name, structured_wait_events.WAIT, waits[token])
            else:
                structured_wait_events.validate(
                    self.role.name, structured_wait_events.OBLIGATION, obligations[token])
                structured_wait_events.received(self.project.insights.root, self.role.name, obligations[token])
        if args.get('wait_for_check'):
            if 'check_job' not in self.role.capabilities or args['outcome'] != 'wait':
                raise Denied('Check wait requires check_job and outcome=wait')
            job = check_jobs.read(self.project, args['wait_for_check'])
            if self.role.name != job['role']:
                raise Denied('Check wait belongs to another role')
        releases = args.get('release_checks', [])
        if releases:
            if 'check_job' not in self.role.capabilities:
                raise Denied('Releasing checks requires check_job')
            if args.get('wait_for_check') in releases:
                raise Denied('Cannot wait for a released check')
            for job_id in releases:
                if check_jobs.read(self.project, job_id)['role'] != self.role.name:
                    raise Denied('Check release belongs to another role')
        from .routing import check_recommendation
        profile, reason = check_recommendation(args.get("next_profile"), args.get("next_reason"))
        if profile is not None and profile not in self.config.profiles:
            raise Denied("next_profile names no configured profile")
        args = {k: v for k, v in args.items() if k not in ("next_profile", "next_reason")}
        if profile is not None:
            args["next_profile"] = profile
        if reason is not None:
            args["next_reason"] = reason
        if self.role.workspace == "write":
            if not args.get("state", "").strip():
                raise Denied("Writable roles must provide the updated short state")
            if args["outcome"] == "done" and not (self.verification and self.verification["passed"]):
                raise Denied("Completion requires passing the configured acceptance commands")
            # The final snapshot must be representable. Check it here, while
            # the writer can still remove or ignore the offending file: a
            # capture limit that only surfaced at publication ended the unit
            # without naming the cause.
            try:
                self.project.snapshots.check_bounds(self.workspace)
            except LimitExceeded as exc:
                self.capture_limit = str(exc)[:512]
                raise Denied(self.capture_limit) from exc
            # The workspace is representable again: a recorded cause no longer
            # applies to this role.
            self.capture_limit = None
            self.capture_resolved = True
        wait = min(args.get("wait_seconds", self.config.limits.default_wait_seconds),
                   self.config.limits.maximum_wait_seconds)
        if self.bound_hit is not None:
            # A seal after any harness per-run bound is a bounded end, never
            # a fresh verdict: the unit saved what it could and runs again.
            args = {**args, "outcome": "bounded"}
        if self.role.workspace == "write":
            self._check_writer_parking(args)
            if args["outcome"] == "wait":
                args["wait_liveness"] = self._writer_wait_liveness(releases=releases)
        self.close_exec()
        # State is optional for readers; persist its protocol default so run
        # lifecycle records and immutable review snapshots agree.
        finished = {"state": "", **args, "wait_seconds": wait, "recorded_at": now(),
                    **({"bound": self.bound_hit} if self.bound_hit is not None else {})}
        if review is not None and finished["outcome"] != "bounded":
            finished["review"] = review
        write_json(self.run_dir / "finish-intent.json", finished)
        self.finished = finished
        return {"sealed": True}
