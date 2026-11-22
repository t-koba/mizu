"""Engine top-level run: admission, dispatch, one model unit and its commit.

This mixin owns the whole-run path that remained in ``runtime`` after the
execution/delegation/dispatch extraction: maintenance and probe service,
slot/reservation admission, pre/post-dispatch projection, the sealed model
turn, and the durable result/health records. ``Engine`` inherits it, so every
existing ``Engine._run`` reference and ``patch.object(Engine, '_run', ...)``
call site keeps resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import shutil
import time
import uuid
from typing import TYPE_CHECKING

from . import (check_jobs, ci_waits, delegation_gates, delivery_settlement,
               dispatch_evidence, editor, lock_diagnostics, proposal_waits,
               reviews, safe_point, work_item_events)
from . import platform as _platform
from .ci_observation import effective_ci_wake_branches
from .ci_wake import wake_facts_cover_due, write_ci_wake
from .config import role_cooldown_seconds, role_policy_bytes, role_progress_signals
from .context_lifecycle import WRAP_UP_OPS
from .delegation_jobs import DelegationJobs, _WorkspaceLease
from .errors import (AuthorityGrantMismatch, Busy, Cancelled, ConfigError, Denied,
                     DrainYieldExceeded, InfraExceeded, LimitExceeded, MissingFinish,
                     MizuError, ModelFailure, ProtocolError, RequestYieldExceeded,
                     RunDeadlineExceeded, ToolYieldExceeded)
from .evidence_records import MAX_STARTED_EVIDENCE_BYTES
from .fs import canonical, digest, mkdir
from .run_health import (_capture_cause, _clear_missing_finish_streak,
                         _record_health_failure, _record_missing_finish_only)
from .unit_finish import _READY_WORK_UNSET, _progress_record
from .wake_evidence import _operator_woke, _role_cursor, _wake_generation_digest

if TYPE_CHECKING:  # pragma: no cover - annotations only, never a runtime cycle.
    from .runtime import Context, Project


def _bound_started_evidence(record, queue, maximum=MAX_STARTED_EVIDENCE_BYTES):
    """Trim the optional queue sample to the retained-evidence byte cap.

    Keep the oldest observations and count every dropped row in
    ``queue["omitted"]``. If needed, omit choice candidates first, retaining
    the selected candidate when it fits and marking that set incomplete. This
    is called before every started.json write, including after the continuation
    identity is added. If the base record
    alone exceeds the cap, leave it unchanged: this owner only controls the
    optional sample, not the rest of the run record.
    """
    observations = queue["observations"]
    while True:
        candidate = dict(record)
        if observations or queue["omitted"]:
            candidate["queue_observations"] = {
                "version": 1,
                "observations": list(observations),
                "omitted": queue["omitted"],
            }
        else:
            candidate.pop("queue_observations", None)
        if len(canonical(candidate)) <= maximum:
            record.clear()
            record.update(candidate)
            return record
        if observations:
            choices = observations[-1].get("choice_set")
            if choices and choices["candidates"]:
                # Keep the selected candidate if possible; discard alternatives
                # as a batch so trimming work is linear in the bounded view.
                current = [row for row in choices["candidates"]
                           if row["admission_id"] == observations[-1]["admission_id"]]
                retained = current if len(current) < len(choices["candidates"]) else []
                choices["omitted"] += len(choices["candidates"]) - len(retained)
                choices["candidates"] = retained
                choices["complete"] = False
                continue
            observations.pop()
            queue["omitted"] += 1
            continue
        candidate.pop("queue_observations", None)
        record.clear()
        record.update(candidate)
        return record


def _epoch_text(epoch) -> str:
    """Name an epoch second in UTC, so a bound reads as a time."""
    try:
        return dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc).isoformat(timespec="seconds")
    except (OSError, OverflowError, TypeError, ValueError):
        return str(epoch)


class EngineRunMixin:
    def _run(self, project: Project, role_name: str, *, attributes=None, retry_busy: bool = False,
                enqueue_busy: bool = False, event_trigger: bool = False, probes_serviced: bool = False) -> dict:
        # Runtime still owns these bindings; resolve them at call time so
        # existing ``mizu.runtime`` patch seams keep intercepting this run.
        from .context_protocol import is_deferred
        from .runtime import (Context, cleanup, container_runtime,
                              now, publish, read_json, session_prompt, write_json)
        if role_name not in project.roles:
            raise Denied("Role is not enabled for this project")
        role = self.config.roles[role_name]
        control = project.control()
        if not control.get("armed") or control.get("paused"):
            raise Denied("Project is unarmed or paused")  # Before any maintenance or probe.
        DelegationJobs.guard(project.root)
        from .workspace_cache import maintain
        maintain(project)  # Before probes/admission take any execution locks.
        from .storage import maintain_automatic_prune
        maintain_automatic_prune(project)  # Due run retention; best-effort, never fails the run.
        from .smoke_retention import maintain_automatic_smoke_retention
        maintain_automatic_smoke_retention(project)  # Due shared-smoke sweep; best-effort, never fails the run.
        # Any top-level run services due recovery probes first — daemon,
        # one-shot, or smoke — so block recovery never depends on a resident
        # daemon for the sponsoring role or that role's execution mode. The
        # daemon loop already attempts the same service before its readiness
        # gate and passes probes_serviced=True, so a ready daemon iteration
        # services at most one event per top-level run. Standalone and
        # one-shot runs keep the default attempt. Best-effort: probe failures
        # never fail the run's own work, exactly like a skipped daemon iteration.
        if not probes_serviced:
            try:
                from .probes import service_due as _service_due_probes
                _service_due_probes(self.config, project.name, self.stop)
            except (MizuError, OSError, ValueError):
                pass
        with contextlib.ExitStack() as continuation_owner, contextlib.ExitStack() as stack:
            if "check_job" in role.capabilities:
                hold = check_jobs.deferral(project, role_name)
                if hold is not None:
                    raise Busy("Waiting for sandbox check " + hold["id"]
                               + " until " + _epoch_text(hold["wait_until"])
                               + "; no model slot acquired")
            self._check_admission(project)
            from . import continuations
            intent = (continuations.ready(project.snapshots, role_name)
                      if role.workspace == "write" else None)
            lock_diagnostics.phase("editor-ingestion")
            editor.ingest(project)
            pre_dispatch_wait_reconciliation = False
            pre_dispatch_wait_resolution_pending = False
            lock_diagnostics.phase("pre-admission-dispatch")
            if (event_trigger or role.on_change) and not (
                    role.workspace == "write" and safe_point.pending_hint(project.snapshots)):
                dispatch_state = self._dispatch_state(project, role_name, role, event_trigger=event_trigger,
                                                      continuation=intent is not None)
                pre_dispatch_wait_reconciliation = bool(dispatch_state.get("_waits_reconciled"))
                pre_dispatch_wait_resolution_pending = bool(
                    dispatch_state.get("_pending_wait_resolution"))
                if dispatch_state.get("skip"):
                    return dispatch_state["skip"]
            # Bounded run-local evidence, not a scheduling input. Keep the
            # configured number of contended admissions, then trim the sample to
            # the started.json retained-read cap; count every dropped row.
            queue_observations = {"version": 1, "observations": [], "omitted": 0}
            queue_observation_limit = self.config.limits.queue_observations
            started_evidence_cap = min(self.config.limits.file_bytes,
                                       MAX_STARTED_EVIDENCE_BYTES)
            started = None
            def record_admission(row):
                # Patches installed during the model turn replace the runtime
                # binding, so re-resolve it at write time, not at method entry.
                from .runtime import write_json
                if len(queue_observations["observations"]) < queue_observation_limit:
                    queue_observations["observations"].append(row)
                else:
                    queue_observations["omitted"] += 1
                if started is not None:
                    _bound_started_evidence(started, queue_observations,
                                             started_evidence_cap)
                    write_json(run_dir / "started.json", started)
            admission_identity = None
            def bind_admission(identity):
                nonlocal admission_identity
                admission_identity = identity
            continuation = None
            lock_diagnostics.phase("admission")
            if intent is not None:
                continuation = stack.enter_context(continuations.reservation(
                    self, project, role_name, wait=retry_busy, enqueue=enqueue_busy,
                    observation_sink=record_admission, identity_sink=bind_admission,
                    expected=intent, owner_stack=continuation_owner))
                if continuation is None:
                    return {"skipped": "stale-continuation"}
                admission_elapsed = continuation["admission_elapsed_seconds"]
                recovered = continuations.recover(project.snapshots, continuation)
                if recovered is not None:
                    return recovered
            else:
                admission_elapsed = self._admit(project, role_name, role, stack, wait=retry_busy,
                                               enqueue=enqueue_busy, observation_sink=record_admission,
                                               identity_sink=bind_admission)
                # A yield may have committed while this ordinary wake queued.
                # Do not allocate a different identity for the same demand.
                if role.workspace == "write" and continuations.ready(project.snapshots, role_name):
                    return {"skipped": "continuation-pending"}
                if role.workspace == "write" and safe_point.read(project.snapshots) is None:
                    # Admission already owns role/workspace exclusions. Cleanup
                    # takes publication last; ambiguous evidence cannot hold up
                    # this ordinary writer or discard its unsealed workspace.
                    with contextlib.suppress(MizuError, OSError, ValueError):
                        continuations.reconcile_prepared(project.snapshots, role_name)
            recovery = (safe_point.guard(project.snapshots, role_name)
                        if role.workspace == "write" else None)
            recovery_authority = role
            if recovery is not None:
                recovery_profiles = {role.profile}
                if role.selector:
                    recovery_profiles.update(c["profile"]
                        for rule in self.config.selectors[role.selector].get("rules", [])
                        for c in rule.get("candidates", []))
                if (self.ephemeral or "finish" not in role.capabilities or role.engine_tools
                        or recovery["profile"] not in self.config.profiles
                        or recovery["profile"] not in recovery_profiles
                        or self.config.engine(recovery["profile"]) not in ("claude", "codex")):
                    raise Denied("Safe-point recovery requires original writer finish authority")
                role = dataclasses.replace(role, profile=recovery["profile"], selector="", engine_tools=(),
                    capabilities=tuple(c for c in role.capabilities if c in ("files", "read", "diff", "finish")))
            dispatch_state = self._dispatch_state(
                project, role_name, role, event_trigger=event_trigger,
                continuation=continuation is not None or recovery is not None,
                prior_wait_resolution_pending=pre_dispatch_wait_resolution_pending,
                reconcile_waits=not pre_dispatch_wait_reconciliation)
            if dispatch_state.get("skip"):
                return dispatch_state["skip"]
            snapshot = dispatch_state['snapshot']
            terminal_events = dispatch_state['terminal_events']
            terminal_page = dispatch_state['terminal_page']
            wait_events = dispatch_state['wait_events']
            ci_present = dispatch_state['ci_present']
            ci_failures = dispatch_state['ci_failures']
            prior_wait_expired = dispatch_state['prior_wait_expired']
            queues = dispatch_state['queues']
            _dispatch = dispatch_state['_dispatch']
            # Pre-execution selection probe: evaluate before allocating a run
            # directory so unchanged waits coalesce onto one audit run
            # instead of one directory per idle poll. A changed wait reason
            # still audits as a new run; a dispatch clears the record.
            probed = None
            probe_error = None
            if role.selector:
                try:
                    from .classification import prepare as _prepare_selection
                    from .selection import read_wait as _read_wait
                    from .selection import wait_fingerprint as _wait_fingerprint
                    probed = _prepare_selection(self, project, role, snapshot, attributes)
                    if not probed["profile"]:
                        _fingerprint = _wait_fingerprint(probed)
                        _last = _read_wait(project, role_name)
                        if (continuation is None and isinstance(_last, dict) and _last.get("fingerprint") == _fingerprint
                                and isinstance(_last.get("run"), str)):
                            return {"run": _last["run"], "role": role_name, "status": "waiting",
                                    "finished_at": now(), "selection": probed,
                                    "next_evaluation_at": probed["next_evaluation_at"],
                                    "coalesced": True, "snapshot": snapshot["id"],
                                    "code_digest": snapshot["code_digest"]}
                except Exception as exc:
                    if probed is not None:
                        raise
                    probe_error = exc
            if continuation is not None:
                captured = project.snapshots.capture_files(project.workspace)
                if captured["skipped"] or captured["code_digest"] != snapshot["code_digest"]:
                    raise Denied("Continuation requires the current published workspace tree")
            active = project.root / "active" / f"{role_name}.json"
            run_id = continuation["successor"] if continuation is not None else uuid.uuid4().hex
            run_dir = project.root / "runs" / run_id
            mkdir(run_dir)
            context = None
            decision = None
            selection_role = None
            selection_retries: list = []
            max_selection_attempts = 1
            selection_attempt = 0
            recorded_selection_failure = None
            model_result = None
            result = None
            workspace = project.workspace if role.workspace == "write" else run_dir / "input"
            try:
                if continuation is None:
                    write_json(active, {"run": run_id, "role": role_name, "started_at": now()})
                admission = _dispatch.admission
                _ci_wake_branches = effective_ci_wake_branches(self.config)
                started = {"run": run_id, "role": role_name,
                            "snapshot": snapshot["id"], "started_at": now(),
                            "admission_elapsed_seconds": round(admission_elapsed, 6),
                            "admission": admission,
                            "queue_messages": sum(row.get("count", 0) for row in queues),
                            "work_item_events": len(terminal_events),
                            "wait_events": len(wait_events),
                            "ci_failures": len(ci_present),
                            "config_sha256": digest(self.config.file.read_bytes()),
                            "policy_sha256": digest(role_policy_bytes(role))}
                continuation_identity = (continuations.identity(continuation)
                                         if continuation is not None else None)
                if continuation_identity is not None:
                    started["continuation"] = continuation_identity
                _bound_started_evidence(started, queue_observations, started_evidence_cap)
                if continuation is not None:
                    continuations.start(project.snapshots, continuation, started)
                    started["continuation"] = continuation_identity
                else:
                    write_json(run_dir / "started.json", started)
                if continuation is not None:
                    write_json(active, {"run": run_id, "role": role_name, "started_at": now()})
                if terminal_events:
                    write_json(run_dir / "work-item-events.json",
                               {"run": run_id, "role": role_name, "admission": admission,
                                **terminal_page})
                if wait_events:
                    write_json(run_dir / "wait-events.json",
                               {"run": run_id, "role": role_name, "admission": admission,
                                "events": wait_events})
                if role.selector:
                    from .classification import prepare
                    from .selection import clear_wait as _clear_wait
                    from .selection import invalidate_wait as _invalidate_wait
                    from .selection import wait_fingerprint as _wait_fingerprint2
                    from .selection import write_wait as _write_wait
                    if probe_error is not None:
                        raise probe_error
                    decision = probed if probed is not None else prepare(self, project, role, snapshot, attributes)
                    write_json(run_dir / "selection.json", decision)
                    if not decision["profile"]:
                        result = {"run": run_id, "role": role_name, "status": "waiting",
                                  "finished_at": now(), "selection": decision,
                                  "next_evaluation_at": decision["next_evaluation_at"],
                                  **({"snapshot": snapshot["id"],
                                      "continuation": continuations.identity(continuation)}
                                     if continuation is not None else {})}
                        write_json(run_dir / "result.json", result)
                        with contextlib.suppress(OSError, ValueError, TypeError):
                            _write_wait(project, role_name, _wait_fingerprint2(decision),
                                        run_id, decision["next_evaluation_at"])
                        return result
                    try:
                        _clear_wait(project, role_name)
                    except (OSError, ValueError, TypeError):
                        # A failed clear must not leave a stale pointer that
                        # falsely coalesces the next wait onto a pre-dispatch
                        # audit run; poison the record so it re-audits.
                        _invalidate_wait(project, role_name)
                    # Single-use routing: a dispatched unit consumes the fresh
                    # recommendation so the next unit falls back unless a newer
                    # one is recorded. Waiting selections above return early and
                    # never consume; preview never reaches this path. Sticky
                    # selectors keep the file. Best-effort, never fatal.
                    try:
                        validity = self.config.selectors.get(role.selector, {}).get(
                            'recommendation_validity', 'once')
                        if validity != 'sticky' and decision.get('recommendation', {}).get('status') == 'fresh':
                            from .routing import consume as _consume_routing
                            _consume_routing(project, role_name)
                    except Exception:
                        pass
                    selection_role = role
                    try:
                        _spec = self.config.selectors.get(role.selector, {})
                        _profiles = {c.get("profile") for _rule in _spec.get("rules", [])
                                     for c in _rule.get("candidates", [])}
                        max_selection_attempts = len(_profiles) + 1
                    except Exception:
                        max_selection_attempts = 1
                    role = dataclasses.replace(role, profile=decision["profile"], selector="")
                if self.stop.is_set() or not project.control().get("armed") or project.control().get("paused"):
                    raise Cancelled("Run stopped before model dispatch")
                driver = self.resolve(role.profile)
                if getattr(driver, "requires_sandbox", True) and any(c in role.capabilities for c in ("exec", "experiment", "verify")):
                    container_runtime(self.config)
                    cleanup(self.config, project.root, role_name, owner=True)
                workspace = project.workspace
                if role.workspace != "write":
                    workspace = run_dir / "input"
                    if role.workspace == "none":
                        mkdir(workspace)
                    else:
                        project.snapshots.materialize(snapshot, workspace)
                context = Context(self.config, project, role, run_dir, snapshot, workspace,
                                  stop=self.stop, consult=self.consult, delegate=self.delegate)
                context.external_check_wake_facts = [
                    dict(fact) for fact in dispatch_state["external_check_wake_facts"]]
                if role.workspace == "write":
                    context._workspace_lease = _WorkspaceLease(self, context, stack)
                context.admission = admission
                context._slot_lease = True
                context._admission_id = admission_identity
                context._admission_observation_sink = record_admission
                context.ephemeral = self.ephemeral
                context.reselection_role = selection_role
                context.reselection_decision = decision
                if recovery is not None:
                    safe_point.recover(project.snapshots, recovery, run_id)
                    context.safe_point_yield_requested = True
                    context.turn_authority_role = recovery_authority
                    context.spend_role = recovery_authority
                context.queues = queues
                context.work_item_events = terminal_events
                context.wait_events = wait_events
                context.ci_wait_manifest = dispatch_state["ci_manifest"]
                context.proposal_wait_manifest = dispatch_state["proposal_manifest"]
                role_cursor = _role_cursor(project, role_name) if role.workspace != "write" else snapshot
                context.progress_input = (
                    _operator_woke({"wake_generation": context.wake_generation}, role_cursor,
                                   observer=role.workspace != "write")
                    or bool(queues or terminal_events or wait_events or ci_failures)
                    or (role.workspace == "write" and prior_wait_expired))
                # Capture the completion index with the opening prompt: its
                # terminal IDs are seeded as seen and filtered as `already`,
                # while changes omitted by the bounded prompt stay eligible.
                # The generation binds this position to the index generation,
                # so a rebuild re-enrolls instead of trusting a stale seq.
                position = check_jobs.completion_position(project)
                context.check_steer_seq = position.seq
                context.check_steer_generation = position.generation
                prompt, prompt_mode, session_key, rotation, session_dir = session_prompt(context, role, run_dir)
                # A pin must be durable before dispatch-adjacent delivery state
                # changes, so a setup failure cannot consume an unseen wake.
                delivery_settlement.register(context)
                dispatch_evidence.capture_projection(context, role.profile, prompt_mode,
                                                     session_key, rotation, session_dir)
                decoded = json.loads(prompt)
                work_items_page = decoded.get("open_work_items")
                write_json(run_dir / "prompt_projection.json",
                           {"run": run_id, "role": role_name, "profile": role.profile, "snapshot": snapshot["id"],
                            "recent_snapshots": len(decoded.get("recent_snapshots", [])),
                            "queues": len(decoded.get("queues", [])),
                            # None: this role's prompt carries no work-item inventory.
                            "open_work_items": None if work_items_page is None else {
                                "shown": len(work_items_page["items"]),
                                "total": work_items_page["total"],
                                "overflow_count": work_items_page["overflow_count"],
                                "truncated": work_items_page["truncated"]},
                            "work_item_events": len(decoded.get("work_item_events", [])),
                            "wait_events": len(decoded.get("wait_events", [])),
                            "acceptance_commands": len(decoded.get("acceptance_commands", [])),
                            "prompt_bytes": len(prompt.encode("utf-8")), "created_at": now(),
                            "prompt_mode": prompt_mode, "session_key": session_key,
                            "rotation": (rotation or {}).get("reason", "")})
                # A scheduling delivery is not acknowledgement of unread output.
                # The same opening-prompt facts seed this unit's in-run steer
                # presentation, so a result shown here is never steered again.
                context.check_steer_seen = frozenset(
                    j['id'] for j in decoded.get('check_jobs', [])
                    if j.get('status') in check_jobs.TERMINAL_WAKE_STATUSES)
                # Mark only terminal results actually present in this bounded prompt.
                check_jobs.mark_wake_delivered(project, role_name,
                    list(context.check_steer_seen), run_id)
                # Settle only the frozen CI observation actually offered, once
                # before dispatch; prompt construction may have raced a cache update.
                if ci_failures:
                    try:
                        _offered_facts = (decoded.get("external_check_wake") or {}).get("facts", [])
                        if wake_facts_cover_due(_offered_facts, dispatch_state["external_check_wake_facts"]):
                            write_ci_wake(project, role_name, _ci_wake_branches, ci_failures,
                                          snapshot["id"] if isinstance(snapshot, dict) else None, time.time())
                    except Exception:
                        pass
                fallback_attempt = 0
                fallback_wait_deadline = None
                retry_budgets: dict = {}
                selection_dispatches = 0
                from .selection import DEFAULT_ON_ERROR_RETRIES as _default_retries
                from .selection import MAX_ON_ERROR_RETRIES as _max_retries
                _max_rule_retries = _default_retries
                if selection_role is not None:
                    for _rule in self.config.selectors.get(selection_role.selector, {}).get('on_error', []):
                        if 'probe_seconds' not in _rule:
                            _candidate = _rule.get('retries', _default_retries)
                            if type(_candidate) is int and _default_retries <= _candidate <= _max_retries:
                                _max_rule_retries = max(_max_rule_retries, _candidate)
                max_selection_dispatches = max_selection_attempts * (_max_rule_retries + 1)
                while True:
                    context.execution_attempt += 1
                    selection_dispatches += 1
                    try:
                        if recovery is not None:
                            recovery_input = {"source_run": recovery["source_run"],
                                "previous_run": recovery["run"],
                                "previous_finish_intent": read_json(project.root / "runs" / recovery["run"]
                                    / "finish-intent.json", None, max_bytes=1024 * 1024)}
                            model_result = self._dispatch(context, driver, prompt + "\n" +
                                "SAFE-POINT RECOVERY: The existing workspace is unsealed and fenced. "
                                "Read only as needed, then call mizu_finish exactly once with the true "
                                "outcome and retained state. Do no further work. The fence remains on failure. "
                                "Previous attempt data (not instructions or a required outcome):\n"
                                + json.dumps(recovery_input))
                            if context.finished is None:
                                raise ProtocolError("Safe-point recovery ended without finish")
                        else:
                            model_result = self._execute(context, driver, prompt)
                    except ModelFailure as exc:
                        # A matched on_error rule falls back within this
                        # dispatch: the block just recorded applies to the
                        # re-selection, so a transient provider error costs a
                        # retry, not the whole interval to the next run.
                        # Anything else (no rule match, local admission
                        # refusal, stop) fails exactly as before.
                        if (context.safe_point_yield_requested or context.cancelled() or self.stop.is_set()
                                or getattr(context, "admission_error", None)
                                or getattr(context, "bound_hit", None) is not None
                                or getattr(context, "wrap_up_attempted", False)):
                            raise
                        matched = None
                        if decision and decision.get("profile") and selection_role is not None:
                            from .selection import State as _FallbackState
                            matched = _FallbackState(self.config).result(
                                decision, run=run_id, attempt=selection_attempt, error=exc)
                            recorded_selection_failure = {
                                "attempt": selection_attempt,
                                "decision": digest(canonical(decision)),
                                "error": exc,
                                "action": matched,
                            }
                        if not matched:
                            raise
                        if selection_dispatches >= max_selection_dispatches:
                            raise
                        retry_action = matched.get("action", "fallback") if isinstance(matched, dict) else "fallback"
                        retry_allowance = (matched.get("retries", _default_retries)
                                           if isinstance(matched, dict) else _default_retries)
                        if retry_action not in ("fallback", "retry"):
                            raise ConfigError("Invalid selector on_error retry policy")
                        if (type(retry_allowance) is not int
                                or not 0 <= retry_allowance <= _max_retries):
                            raise ConfigError("Invalid selector on_error retry policy")
                        if (retry_action == "retry" and "probe_seconds" not in matched):
                            retry_key = (decision.get("profile"), matched.get("rule"))
                            retry_left = retry_budgets.get(retry_key, retry_allowance)
                            if retry_left > 0:
                                retry_budgets[retry_key] = retry_left - 1
                                if (self.stop.is_set() or not project.control().get("armed")
                                        or project.control().get("paused")):
                                    raise Cancelled("Run stopped before selection retry")
                                retry_wait = max(0.0, (matched.get("until") or 0) - time.time())
                                # Provider timestamps are external-untrusted: bound any
                                # retry wait (seconds or provider until) at the
                                # operator-configured maximum wait. The verbatim
                                # provider value stays in the action evidence;
                                # only the wait is capped, then the retry fires.
                                retry_wait = min(retry_wait,
                                                 float(self.config.limits.maximum_wait_seconds))
                                if retry_wait > 0:
                                    # Bounded wait holding no slot and spending
                                    # nothing; abandonment propagates its own
                                    # error and drops the unit as today.
                                    context._slot_lease = False
                                    self._selection_retry_wait(context, project, stack, selection_role,
                                                               role_name, retry_wait)
                                selection_retries.append({"profile": decision["profile"],
                                                          "error": str(exc)})
                                context.operations_stopped.clear()
                                context.verification = None
                                context.commentary = None
                                driver = self.resolve(role.profile)
                                if getattr(driver, "requires_sandbox", True) and any(
                                        c in role.capabilities for c in ("exec", "experiment", "verify")):
                                    container_runtime(self.config)
                                    cleanup(self.config, project.root, role_name, owner=True)
                                continue
                        fallback_attempt += 1
                        if fallback_attempt >= max_selection_attempts:
                            raise
                        if (self.stop.is_set() or not project.control().get("armed")
                                or project.control().get("paused")):
                            raise Cancelled("Run stopped before fallback dispatch")
                        selection_retries.append({"profile": decision["profile"],
                                                  "error": str(exc)})
                        # A reselection fault (corrupt state, invalid selector)
                        # is a local invariant failure: it propagates as itself,
                        # chained to the model failure, never relabelled as a
                        # transient provider error.
                        reselected = prepare(self, project, selection_role, snapshot, attributes)
                        while not reselected.get("profile"):
                            # Only one-shot calls may wait for timed recovery.
                            # Keep one monotonic window across early wakes and
                            # fallback attempts; polling never renews it.
                            unblock_at = reselected.get("next_unblock_at")
                            if (not retry_busy or unblock_at is None
                                    or reselected.get("reason") == "classification_wait"):
                                raise exc
                            if fallback_wait_deadline is None:
                                fallback_wait_deadline = (time.monotonic()
                                                          + self.config.limits.busy_retry_seconds)
                            remaining = fallback_wait_deadline - time.monotonic()
                            wall_now = time.time()
                            if remaining <= 0 or unblock_at - wall_now > remaining:
                                # Preserve the failed decision and original
                                # provider error, with no extra model attempt.
                                raise exc
                            wake_at = min(unblock_at, reselected["next_evaluation_at"])
                            wait_seconds = min(self.config.limits.idle_seconds, remaining,
                                               max(0.0, wake_at - wall_now))
                            context._slot_lease = False
                            self._selection_retry_wait(context, project, stack, selection_role,
                                                       role_name, wait_seconds)
                            if time.monotonic() > fallback_wait_deadline:
                                raise exc
                            # Read-only preparation: a wake must not spend on
                            # classification or launch a recovery probe. Other
                            # executors may have supplied recovery meanwhile.
                            reselected = prepare(self, project, selection_role, snapshot,
                                                 attributes, preview=True)
                        decision = reselected
                        context.reselection_decision = decision
                        selection_attempt += 1
                        write_json(run_dir / "selection.json", decision)
                        role = dataclasses.replace(selection_role, profile=decision["profile"],
                                                   selector="")
                        context.role = role
                        # The failed attempt's bridge close cancelled this
                        # context's operations; the retry is a new engine
                        # connection, so reopen them. Deadline, budget, and
                        # tool counters stay accumulated on the reused context.
                        # Only unsealed failures reach fallback; the retry must
                        # finish and verify its own work.
                        # A terminal unanswered durable settlement is known
                        # not to be an in-flight recovery. Give the selected
                        # fallback its own idempotency key; every other error
                        # keeps the identity and therefore still fails closed
                        # if the candidate's grant changed.
                        if (exc.evidence.get("source") == "pi-durable"
                                and exc.evidence.get("kind") == "unanswered"):
                            context.attempt_ordinal += 1
                        context.operations_stopped.clear()
                        context.verification = None
                        context.commentary = None
                        driver = self.resolve(role.profile)
                        if getattr(driver, "requires_sandbox", True) and any(
                                c in role.capabilities for c in ("exec", "experiment", "verify")):
                            container_runtime(self.config)
                            cleanup(self.config, project.root, role_name, owner=True)
                        continue
                    except LimitExceeded as exc:
                        # A per-run spend bound with no finish sealed gets one
                        # wrap-up attempt under reduced grants instead of an
                        # error: the daily budget, an unmarked bound, an
                        # already-spent allowance still fail exactly as before.
                        # Sealed driver errors were retained by _execute.
                        if (context.safe_point_yield_requested
                                or isinstance(exc, (InfraExceeded, RunDeadlineExceeded, DrainYieldExceeded, ToolYieldExceeded))
                                or getattr(context, "bound_hit", None) is None
                                or getattr(context, "wrap_up_attempted", False)
                                or context.finished is not None):
                            raise
                        grants = [cap for cap in WRAP_UP_OPS if cap in role.capabilities]
                        if not grants:
                            raise
                        context.wrap_up_attempted = True
                        previous_authority = context.turn_authority_role
                        previous_continuation = context.turn_continuation
                        authority = previous_authority or role
                        context.turn_authority_role = authority
                        context.turn_continuation = True
                        previous_session_capabilities = context.session_identity_capabilities
                        if context.bound_hit == "drain":
                            context.session_identity_capabilities = authority.capabilities
                        try:
                            context.role = dataclasses.replace(
                                authority, capabilities=tuple(c for c in WRAP_UP_OPS if c in authority.capabilities),
                                engine_tools=(), selector="")
                            context.operations_stopped.clear()
                            driver = self.resolve(role.profile)
                            if getattr(driver, "requires_sandbox", True) and any(
                                    c in context.role.capabilities for c in ("exec", "experiment", "verify")):
                                container_runtime(self.config)
                                cleanup(self.config, project.root, role_name, owner=True)
                            notice = context._bound_message()
                            if context.bound_hit != "drain":
                                notice += (". Seal with finish (recorded as a bounded end). "
                                           "Keep the published state unless this run changed it.")
                            context.execution_attempt += 1
                            model_result = self._execute(context, driver, notice + "\n\n" + prompt)
                        finally:
                            context.turn_authority_role = previous_authority
                            context.turn_continuation = previous_continuation
                            context.session_identity_capabilities = previous_session_capabilities
                    break
                if context.host_cancelled():
                    raise Cancelled("Run stopped before publication")
                if context.finished is None:
                    if context.bound_hit == "deadline" or context.wall_expired():
                        raise RunDeadlineExceeded("Deadline-bounded run ended without finish")
                    if context.bound_hit == "drain" and context.wrap_up_attempted:
                        raise DrainYieldExceeded("Drain-bounded run ended without finish")
                    raise Denied("Missing finish intent")
                finished = context.finished
                check_wait = None
                if finished.get("wait_for_check"):
                    # The unit's own wait bound ends the deferral: a check
                    # wait holds the role for as long as the wait it sealed
                    # with, never longer, so it cannot outlive the bound the
                    # operator configured for that wait.
                    check_wait = {"id": finished["wait_for_check"],
                                  "wait_until": time.time() + float(
                                      finished.get("wait_seconds")
                                      or self.config.limits.default_wait_seconds)}
                if role.workspace == "write":
                    delegation_gates.refuse_live_tree(context._reservation_view,
                                                     "Final workspace capture")
                context.close_exec()
                if role.workspace == "write":
                    captured = project.snapshots.capture_files(workspace)
                    verification = context.verification
                    if verification and (verification["code_digest"] != captured["code_digest"] or captured["skipped"]):
                        verification = {**verification, "passed": False, "invalidated": "code changed or final snapshot is unrepresentable"}
                    if finished["outcome"] == "done" and not (verification and verification["passed"]):
                        raise Denied("Code changed after verification; completion refused")
                    # Diagnostic existence probe; parking was already checked before seal.
                    try:
                        ready_work_at_finish = bool(project.work_items.page_ready(
                            limit=1, role_name=role_name)["items"])
                    except (Denied, OSError):
                        ready_work_at_finish = None
                    progress = context._finish_progress(
                        captured, finished, ready_work=ready_work_at_finish)
                    snapshot = project.snapshots.create(
                        captured, goal=context.goal, state=finished["state"], run=run_id,
                        outcome=finished["outcome"], summary=finished["summary"], verification=verification,
                        wake_at=time.time() + finished["wait_seconds"] if finished["outcome"] == "wait" else None,
                        wake_generation=context.wake_generation, progress=progress,
                        next_step=finished.get("next_step"),
                        parent=context.snapshot["id"])
                    if context.safe_point_yield_requested:
                        safe_point.target(project.snapshots, run_id, snapshot["id"])
                    if (context.safe_point_yield_requested and finished["outcome"] == "continue"):
                        continuations.prepare(project.snapshots, role=role_name, source_run=run_id,
                                              snapshot=snapshot["id"])
                review_snapshot = reviews.record(context, finished)
                if review_snapshot is not None:
                    snapshot = review_snapshot
                if decision:
                    from .selection import State
                    State(self.config).result(decision, run=run_id, attempt=selection_attempt)
                result = {"run": run_id, "role": role_name, "status": "prepared", "finished_at": now(),
                          "finish": finished, "model": model_result, "snapshot": snapshot["id"],
                          "activity": context.activity_summary(),
                          "ci_requests": context.ci_request_summary(),
                          **({"continuation": continuations.identity(continuation)}
                             if continuation is not None else {}),
                          **({"selection": decision} if decision else {})}
                write_json(run_dir / "result.json", result)
                if role.workspace == "write":
                    project.snapshots.publish(snapshot)
                    if context.safe_point_yield_requested:
                        safe_point.guard(project.snapshots, role_name)
                    # Own publication moves the head without new CI facts: refresh
                    # the wake record to the published head so this run does not
                    # re-wake itself; a new sha/check or a later head still wakes.
                    try:
                        _refresh_present = ci_present
                        _refresh_branch = _ci_wake_branches
                    except Exception:
                        _refresh_present = []
                        _refresh_branch = None
                    if _refresh_present:
                        try:
                            write_ci_wake(project, role_name, _refresh_branch, _refresh_present,
                                          snapshot["id"] if isinstance(snapshot, dict) else None,
                                          time.time())
                        except Exception:
                            pass
                # Advisory hint: a failed routing write must not flip an
                # already-published unit to interrupted, like the other
                # best-effort post-publication records below.
                with contextlib.suppress(OSError):
                    from .routing import record as record_routing
                    record_routing(project, role_name, finished, snapshot, context.goal_digest, run_id)
                if context.commentary is not None:
                    result["artifact"] = publish(project, snapshot, context.commentary, run_id=run_id)
                # A retained host seal does not confirm session settlement.
                if session_dir is not None and model_result.get("settled") is not False:
                    with contextlib.suppress(OSError, ValueError, TypeError):
                        from . import prompt_sections
                        from .engine_config import write_prompt_state
                        from .unit_prompts import session_grant_evidence, session_row_evidence
                        grant = session_grant_evidence(context, session_dir)
                        write_prompt_state(session_dir, snapshot=snapshot["id"],
                                           broker_grant=grant,
                                           sections=sorted(prompt_sections.included(context.role)),
                                           row_details=session_row_evidence(context, session_dir, grant))
                result["status"] = "waiting" if finished.get("wait_for_check") else "completed"
                if check_wait is not None:
                    result["next_evaluation_at"] = time.time() + self.config.limits.idle_seconds
                    result["waiting_for_check"] = check_wait
                if getattr(context, "wrap_up_attempted", False):
                    result["wrap_up"] = {"bound": context.bound_hit}
                write_json(run_dir / "result.json", result)
                if finished.get("outcome") != "bounded" and not context.bound_hit:
                    released = set(finished.get("release_checks", []))
                    for job_id in released:
                        check_jobs.cancel(context, job_id)
                    with contextlib.suppress(OSError, ValueError, Denied):
                        check_jobs.acknowledge(project, role_name,
                                               set(getattr(context, "check_results_read", set())) | released)
                if not finished.get("wait_for_check"):
                    source = context.snapshot if review_snapshot is not None else snapshot
                    observed = {"snapshot": source["id"], "code_digest": source["code_digest"]}
                    if review_snapshot is not None:
                        observed["review_receipt"] = review_snapshot["id"]
                        observed["review"] = finished["review"]
                    if role.workspace != "write":
                        state_digest = digest(finished.get("state", "").encode())
                        signals = {
                            "tool": context.progress_tool, "code": False,
                            "state": state_digest != role_cursor.get(
                                "state_digest", digest(context.snapshot["state"].encode())),
                            "message": context.progress_message, "input": context.progress_input,
                        }
                        observed.update(
                            wake_generation_digest=_wake_generation_digest(context.wake_generation),
                            state_digest=state_digest, outcome=finished["outcome"],
                            progress=_progress_record(
                                role_cursor, role_name, tuple(role_progress_signals(self.config, role)),
                                signals, finished["outcome"],
                                ready_work=None,
                                ready_work_signature=getattr(context, "ready_work_signature", None),
                                wait_resolution_tokens=getattr(
                                    context, "wait_resolution_tokens", _READY_WORK_UNSET),
                                wait_resolution_high_water=getattr(
                                    context, "wait_resolution_high_water", _READY_WORK_UNSET)))
                    if role.workspace == "write":
                        write_json(project.root / "observed" / f"{role_name}.json", observed)
                delivery_settlement.begin(context, finished)
                selected_tokens = set(finished.get("ack_events", []))
                selected_terminal = [e for e in context.work_item_events if e["ack_token"] in selected_tokens]
                if selected_terminal:
                    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, Denied):
                        work_item_events.acknowledge(
                            project.work_items, role_name,
                            [work_item_events.acknowledgement_payload(project.work_items, role_name, e)
                             for e in selected_terminal])
                with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, Denied, LimitExceeded):
                    ci_waits.complete(context, finished)
                    delivery_settlement.succeeded(context, "ci")
                with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, Denied, LimitExceeded):
                    proposal_waits.complete(context, finished)
                    delivery_settlement.succeeded(context, "proposal")
                if check_wait is not None:
                    check_jobs.set_wait(project, role_name, check_wait["id"], check_wait["wait_until"], continuation={
                        "run": run_id, "snapshot": snapshot["id"], "code_digest": snapshot["code_digest"],
                        "summary": finished["summary"], "state": finished.get("state", "")})
                else:
                    with contextlib.suppress(OSError):
                        check_jobs.clear_wait(project, role_name)
                write_json(project.root / "health" / f"{role_name}.json",
                           {"consecutive_failures": 0, "consecutive_missing_finish": 0,
                            "last_run": run_id, "updated_at": now()})
                delivery_settlement.finalized(context)
                # Do not consume a read-only observation until finalization
                # succeeds: an interrupted finish must remain runnable.
                if role.workspace != "write" and not finished.get("wait_for_check"):
                    write_json(project.root / "observed" / f"{role_name}.json", observed)
                return result
            except BaseException as exc:
                if continuation is not None and context is None:
                    # Before model setup, leave only the create-only start
                    # (if any). A failed start must remain retryable.
                    raise
                if (isinstance(exc, (RunDeadlineExceeded, DrainYieldExceeded, RequestYieldExceeded, ToolYieldExceeded, AuthorityGrantMismatch))
                        and context is not None
                        and context.finished is None and not context.host_cancelled()):
                    result = {"run": run_id, "role": role_name, "status": "bounded",
                              **({"continuation": continuations.identity(continuation)}
                                 if continuation is not None else {}),
                              "bound": ("drain" if isinstance(exc, DrainYieldExceeded)
                                        else ("authority" if isinstance(exc, AuthorityGrantMismatch)
                                              else ("tools" if isinstance(exc, ToolYieldExceeded)
                                                    else ("requests" if isinstance(exc, RequestYieldExceeded) else "deadline")))),
                              "finished_at": now(), "error": str(exc),
                              "publication": "not_published", "snapshot": snapshot["id"],
                              "code_digest": snapshot["code_digest"],
                              "model": getattr(context, "model_evidence", {}),
                              "activity": context.activity_summary()}
                    try:
                        write_json(run_dir / "result.json", result)
                    except Exception as record_error:
                        # A bound is non-failing only when its bounded result
                        # is durably recorded. Preserve ordinary storage-fault
                        # handling (including health accounting) if that fails.
                        exc = record_error
                    else:
                        _clear_missing_finish_streak(project, role_name)
                        return result
                if isinstance(exc, ModelFailure) and context is not None and context.admission_error:
                    if getattr(context, "admission_wait", False):
                        exc = InfraExceeded(f"{self.config.timezone} daily model-request budget exhausted")
                    else:
                        exc = ProtocolError("Local admission failed: " + context.admission_error)
                handled = None
                if decision and decision.get("profile") and isinstance(exc, ModelFailure) and context is not None and not context.cancelled():
                    identity = {
                        "attempt": selection_attempt,
                        "decision": digest(canonical(decision)),
                    }
                    if (isinstance(recorded_selection_failure, dict) and
                            recorded_selection_failure.get("error") is exc and
                            all(recorded_selection_failure.get(key) == value
                                for key, value in identity.items())):
                        # The fallback loop already applied this exact failure
                        # before reselection. Reuse its result rather than
                        # extending a cooldown/block with a second timestamp.
                        handled = recorded_selection_failure["action"]
                    else:
                        from .selection import State
                        handled = State(self.config).result(decision, run=run_id, attempt=selection_attempt, error=exc)
                # Publication is an observed fact, even if subsequent work failed.
                with contextlib.suppress(OSError, ValueError, TypeError):
                    publication = "unknown"
                    try:
                        pointer = read_json(project.root / "current.json", {})
                        published = role.workspace == "write" and pointer.get("snapshot") == snapshot["id"] and snapshot.get("run") == run_id
                        publication = "published" if published else "not_published"
                    except (OSError, ValueError, TypeError, AttributeError):
                        pass
                    cause = _capture_cause(
                        exc, getattr(context, "capture_limit", None) if context is not None else None)
                    resolved = bool(getattr(context, "capture_resolved", False)) and not cause
                    error = {"run": run_id, "role": role_name,
                             "status": "interrupted", "error": str(exc), "finished_at": now(),
                             **({"capture_limit": cause} if cause else {}),
                             "publication": publication,
                             "durability": "unconfirmed" if isinstance(exc, OSError) or publication == "unknown" or _platform.IS_WINDOWS else "confirmed",
                             **({"activity": context.activity_summary()} if context is not None else {}),
                             **({"ci_requests": context.ci_request_summary()}
                                if context is not None else {}),
                             **({"selection": decision} if decision else {}),
                             **({"model_failure": exc.evidence} if isinstance(exc, ModelFailure) else {}),
                             **({"selection_action": handled} if handled else {}),
                             **({"selection_retries": selection_retries} if selection_retries else {}),
                             **({"missing_finish": getattr(exc, "evidence", None)
                                  or getattr(context, "missing_finish_evidence", None)}
                                if isinstance(exc, MissingFinish)
                                or getattr(context, "missing_finish_evidence", None) else {})}
                    if model_result is not None:
                        error["model"] = model_result
                    elif context is not None and hasattr(context, "model_evidence"):
                        error["model"] = context.model_evidence
                    write_json(run_dir / "error.json", error)
                    if result is not None:
                        write_json(run_dir / "result.json", {**result, "status": "interrupted", **error})
                if handled:
                    counts_as_failure = handled.get("counts_as_failure", False)
                    if type(counts_as_failure) is not bool:
                        raise ConfigError("Invalid selector counts_as_failure policy")
                    if counts_as_failure:
                        _record_health_failure(project, role_name, run_id, exc,
                                               self.config.limits.max_failures, cause=cause,
                                               resolved=resolved)
                    else:
                        _clear_missing_finish_streak(project, role_name)
                    deferred_cooldown = (role_cooldown_seconds(self.config, role)
                                             if role.workspace == "write"
                                             else self.config.limits.cooldown_seconds)
                    return {"run": run_id, "role": role_name, "status": "deferred",
                            "error": str(exc), "selection": decision, "selection_action": handled,
                            **({"selection_retries": selection_retries} if selection_retries else {}),
                            "next_evaluation_at": time.time() + deferred_cooldown}
                suppress_missing_finish_brake = (isinstance(exc, MissingFinish)
                                                 and not self.config.limits.missing_finish_counts_as_failure)
                if is_deferred(exc, context):
                    _clear_missing_finish_streak(project, role_name)
                elif suppress_missing_finish_brake:
                    _record_missing_finish_only(project, role_name, run_id, exc, cause=cause,
                                                resolved=resolved)
                else:
                    _record_health_failure(project, role_name, run_id, exc,
                                           self.config.limits.max_failures, cause=cause,
                                           resolved=resolved)
                raise exc
            finally:
                if continuation is not None:
                    # Failure leaves a pinned row for recovery. Never turn a
                    # completed execution into an error over pointer cleanup.
                    with contextlib.suppress(MizuError, OSError, ValueError):
                        continuations.settle(project.snapshots, continuation)
                if context is not None:
                    # Serialize shutdown with reservation commit/gate refresh.
                    # Revoke before dropping Context authority, not merely in
                    # the later ExitStack callback that releases admission.
                    with context.mutex:
                        if context._workspace_lease is not None:
                            context._workspace_lease.close()
                        context._slot_lease = False
                        context.closed = True
                    context.close_exec()
                    with contextlib.suppress(MizuError, OSError, ValueError):
                        delivery_settlement.closed(context)
                with contextlib.suppress(OSError):
                    active.unlink(missing_ok=True)
                # Read-only inputs are reproducible from the content-addressed snapshot.
                if role.workspace != "write":
                    shutil.rmtree(workspace, ignore_errors=True)
