"""Daemon orchestration and per-project VCS poll coordination.

Owns the shared per-project poll lock name, the wall-clock event stamp,
single-tick poll coordination (``poll_project_vcs``) and the resident daemon
loop (``daemon``). ``mizu.runtime`` re-exports these names to preserve its
established import seams.
"""
from __future__ import annotations

import contextlib
import json
import signal
import threading
import time

from . import ci_waits, editor, queue_facts
from .ci_observation import POLL_STATE_FILE, _clean_ci_failures, _clean_ci_successes
from .ci_polling import _live_ci_branches, poll_ci
from .config import DAEMON_POLICIES, Config, load, role_daemon_spacing_seconds
from .errors import Busy, ConfigError, Denied, MizuError
from .fs import lock, now, read_json, write_json
from .project import Project
from .readiness_witness import settled as witness_settled, witness as state_witness
from .role_admission import should_run
from .vcs_poll_policy import upstream_fetch_due, vcs_poll_enabled, vcs_poll_interval
from .vcs_poll_state import _refresh_ci_cache, refresh_poll_state
from .vcs_proposal_poll import poll_proposals
from .vcs_upstream_poll import poll_upstream

#: Shared per-project poll lock name (state filename lives in ci_observation).
POLL_LOCK_NAME = "vcs-poll.lock"


def now_iso() -> str:
    """Wall-clock event stamp for CI polling."""
    return now()


def poll_project_vcs(config, project, state: dict, *, now: float, interval: float) -> list:
    """Poll upstream refs and CI once for a project under the shared lock.

    Schema: ``state`` is daemon in-memory poll state; ``now`` is wall-clock
    seconds; returns the due poll events (possibly empty). Bounds: at most one
    fetch, one CI tick, and one proposal page per call under timeout caps. Trust: host
    side only; refs and CI facts stay external-untrusted. Retry/cancellation:
    no retry; timestamps advance even on failure. Evidence: returned events and
    the shared ``vcs-poll.json`` timestamps. Failure: never raises; a missed
    lock or bad state yields no events.
    """
    try:
        with lock(project.root / "locks" / POLL_LOCK_NAME, blocking=False):
            try:
                shared = read_json(project.root / POLL_STATE_FILE, {})
            except (OSError, ValueError, TypeError):
                shared = {}
            ci_state = refresh_poll_state(state, shared, config, refresh_ci=False)
            events = []
            fetched = poll_upstream(config, project, state, now=now, interval=interval)
            if fetched is not None:
                events.append(fetched)
            # Fetch can delete refs. Discover their lifetime before cache capacity
            # selection, but keep skipped/disabled polls free of ref reads.
            # When the CI tick is not due there is no complete inventory, so
            # defer configured capacity selection (fixed envelope still
            # applies) to avoid evicting live rows via a stale wake ref.
            ci_due = bool(config.vcs.get("command")
                          and upstream_fetch_due(ci_state.get("last_poll"), now, interval))
            live = None
            if ci_due:
                try:
                    live = _live_ci_branches(config, project)
                except (Denied, OSError):
                    pass  # Unknown inventory must not imply branch deletion.
            _refresh_ci_cache(ci_state, shared, config, live_branches=live,
                              defer_branch_cap=not ci_due)
            polled = poll_ci(config, project, ci_state, now=now, interval=interval)
            if polled is not None:
                events.append(polled)
            waited = ci_waits.poll(config, project, state, now=now, interval=interval)
            if waited is not None:
                events.append(waited)
            proposals = poll_proposals(config, project, state, now=now,
                                       interval=interval)
            if proposals is not None:
                events.append(proposals)
            try:
                write_json(project.root / POLL_STATE_FILE,
                           {"last_fetch": state.get("last_fetch"),
                            "last_poll": ci_state.get("last_poll"),
                            "last_proposal_poll": state.get("last_proposal_poll"),
                            "proposal_cursor": state.get("proposal_cursor"),
                            "proposal_refs": state.get("proposal_refs", {}),
                            "proposal_complete_messages": state.get("proposal_complete_messages", {}),
                            "last_ci_wait_poll": state.get("last_ci_wait_poll"),
                            "ci_wait_cursor": state.get("ci_wait_cursor"),
                            "ci_changes_cursor": state.get("ci_changes_cursor"),
                            "ci_changes_pending": state.get("ci_changes_pending"),
                            "ci_changes_epoch": state.get("ci_changes_epoch"),
                            "ci_wait_invalidation": state.get("ci_wait_invalidation"),
                            "failures": _clean_ci_failures(
                                ci_state.get("failures"), config,
                                defer_branch_cap=not ci_due),
                            "successes": _clean_ci_successes(
                                ci_state.get("successes"), config,
                                defer_branch_cap=not ci_due)})
            except (OSError, ValueError, TypeError) as exc:
                # Do not report a proposal page as successful when its cursor
                # and tick timestamp could not be durably resumed. Other VCS
                # polling retains its existing best-effort state semantics.
                proposal_event = next((event for event in events
                                       if event.get("event") == "proposal_poll"), None)
                if proposal_event is not None and proposal_event.get("ok"):
                    message = str(exc) or type(exc).__name__
                    proposal_event.update(
                        ok=False,
                        error=f"Could not persist proposal poll state: {message}"[:512])
            return events
    except Busy:
        return []
    except (OSError, ValueError, TypeError):
        return []


def daemon(config: Config, name: str, role_name: str) -> None:
    from .runtime import Engine  # runtime seam: patched Engine still takes effect
    stop = threading.Event()
    def stopped(signum, frame):
        stop.set()
    watched = [sig for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)) if sig is not None]
    old = {s: signal.signal(s, stopped) for s in watched}
    try:
        with contextlib.ExitStack() as lock_scope, \
                lock(config.data / "locks" / f"daemon-{name}-{role_name}.lock", blocking=False):
            lock_scope.enter_context(config.runtime_policy())
            upstream_state: dict = {"last_fetch": None}
            ordinary_after = 0.0
            project = None
            # (state witness, monotonic re-evaluation deadline) of the last
            # negative readiness result; see readiness_recheck_seconds.
            readiness_memo = None
            while not stop.is_set():
                try:
                    updated = load(config.file)
                    if updated.data != config.data:
                        raise ConfigError("Changing data_dir requires a daemon restart")
                    config = updated
                    lock_scope.close()
                    lock_scope.enter_context(config.runtime_policy())
                    from .probes import service_due, next_deadline
                    # Retain process-local history caches across unchanged reloads,
                    # but revalidate mutable project settings on every daemon poll.
                    if project is None or project.config != config:
                        project = Project(config, name)
                        readiness_memo = None
                    else:
                        project.refresh_settings()
                    role = config.roles[role_name]
                    policy = getattr(role, "daemon_policy", None)
                    if policy not in DAEMON_POLICIES:
                        raise ConfigError("Invalid daemon_policy; reload validated configuration")
                    control = project.control()
                    ready = False
                    if not control.get("armed") or control.get("paused"):
                        readiness_memo = None
                    # An unarmed or paused project has no work: probes, editor
                    # ingestion, VCS polling and readiness are skipped entirely.
                    if control.get("armed") and not control.get("paused"):
                        probe = service_due(config, name, stop)
                        if probe is not None:
                            print(json.dumps(probe, ensure_ascii=False), flush=True)
                        editor.ingest(project)
                        if vcs_poll_enabled(config):
                            interval = vcs_poll_interval(config)
                            for event in poll_project_vcs(config, project, upstream_state,
                                                          now=time.time(), interval=interval):
                                print(json.dumps(event, ensure_ascii=False), flush=True)
                        if policy == "signals" and config.limits.readiness_recheck_seconds:
                            observed = state_witness(project.root)
                            if (readiness_memo is not None and readiness_memo[0] == observed[0]
                                    and time.monotonic() < readiness_memo[1]):
                                pass
                            else:
                                # A positive result or an error is never reused.
                                readiness_memo = None
                                ready = should_run(project, project.snapshots.get(),
                                                   role_name=role_name)
                                if (not ready and witness_settled(observed[1], observed[2])
                                        and state_witness(project.root)[0] == observed[0]):
                                    readiness_memo = (observed[0], time.monotonic()
                                                      + config.limits.readiness_recheck_seconds)
                        elif policy == "signals":
                            ready = should_run(project, project.snapshots.get(), role_name=role_name)
                        else:
                            ready = not control.get("draining")
                    if ready and time.monotonic() >= ordinary_after:
                        result = Engine(config, stop=stop).run(project, role_name, probes_serviced=True,
                                        enqueue_busy=True)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        delay = role_daemon_spacing_seconds(config, role)
                        if result.get("status") in ("waiting", "deferred"):
                            # Poll at idle cadence as well, to observe operator fact/config updates.
                            delay = min(config.limits.idle_seconds, max(0.1, result["next_evaluation_at"] - time.time()))
                        # Probe polling must not accelerate ordinary cooldowns.
                        ordinary_after = time.monotonic() + delay
                        delay = min(delay, config.limits.idle_seconds)
                        probe_at = next_deadline(config, name)
                        if probe_at is not None:
                            delay = min(delay, max(0.1, probe_at - time.time()))
                        try:
                            delivery_at = queue_facts.next_delivery_at(project, role_name)
                        except (MizuError, OSError, ValueError):
                            delivery_at = None
                        if delivery_at is not None:
                            delay = min(delay, max(0.1, delivery_at - time.time()))
                        stop.wait(delay)
                        continue
                except (MizuError, OSError, ValueError) as exc:
                    print(json.dumps({"event": "run_deferred", "error": str(exc), "time": now()}), flush=True)
                from .probes import next_deadline
                delay = config.limits.idle_seconds
                if ordinary_after > time.monotonic():
                    delay = min(delay, ordinary_after - time.monotonic())
                try:
                    probe_at = next_deadline(config, name)
                    if probe_at is not None:
                        delay = min(delay, max(0.1, probe_at - time.time()))
                except (MizuError, OSError, ValueError):
                    pass
                # `project` is still None when load/Project failed on the first
                # pass; that block intentionally depends only on config.
                if project is not None:
                    try:
                        delivery_at = queue_facts.next_delivery_at(project, role_name)
                    except (MizuError, OSError, ValueError):
                        delivery_at = None
                    if delivery_at is not None:
                        delay = min(delay, max(0.1, delivery_at - time.time()))
                stop.wait(delay)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
