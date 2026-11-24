"""Daemon orchestration and per-project VCS poll coordination.

Owns the shared per-project poll lock name, the wall-clock event stamp,
single-tick poll coordination (``poll_project_vcs``) and the resident daemon
loop (``daemon``). ``mizu.runtime`` re-exports these names to preserve its
established import seams.
"""
from __future__ import annotations

import contextlib
import json
import math
import signal
import threading
import time

from . import check_jobs, ci_waits, daemon_wake, editor, queue_facts
from .ci_observation import POLL_STATE_FILE, _clean_ci_failures, _clean_ci_successes
from .ci_polling import _live_ci_branches, poll_ci
from .config import DAEMON_POLICIES, Config, load, role_daemon_spacing_seconds
from .errors import Busy, ConfigError, Denied, MizuError
from .fs import lock, now, read_json, write_json
from .project import Project
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


def _config_stamp(config) -> tuple:
    """Cheap identity of the on-disk policy that can change admission."""
    try:
        st = config.file.stat()
        return (st.st_dev, st.st_ino, st.st_mtime_ns)
    except (OSError, AttributeError, TypeError, ValueError):
        return ()


def _wake_deadline(config, project, name, role_name, ordinary_after,
                   baseline=(), delivery_cache=None) -> float | None:
    """Earliest monotonic instant at which a quiet daemon re-checks state.

    Every deadline comes from a real fact with no watched-file write: cooldown,
    delayed delivery, probe eligibility, an explicit wait or a check wait.
    Writers publish a fingerprint change for every other state change, so there
    is no fixed idle re-evaluation. Wall-clock facts are converted to the
    monotonic clock exactly once, at computation time.
    """
    now_mono = time.monotonic()
    now_wall = time.time()
    candidates: list[float] = []
    if ordinary_after > now_mono:
        candidates.append(ordinary_after)
    try:
        from .probes import next_deadline
        probe_at = next_deadline(config, name)
        if probe_at is not None:
            candidates.append(now_mono + max(0.1, probe_at - now_wall))
    except (MizuError, OSError, ValueError):
        pass
    if delivery_cache is not None:
        cached = delivery_cache.get("baseline") == baseline
        delivery_at = delivery_cache.get("at") if cached else None
        if cached and delivery_at is not None and delivery_at <= now_wall:
            cached = False  # lapsed: recompute for the next pending delivery
    else:
        cached = False
    if not cached:
        # Scanning queued messages is proportional to retained backlog. Reuse
        # the result until a writer changes the state fingerprint; future
        # delayed appends bump the wake directory so this is recomputed.
        try:
            delivery_at = (queue_facts.next_delivery_at(project, role_name)
                           if project is not None else None)
        except (MizuError, OSError, ValueError):
            delivery_at = None
        if delivery_cache is not None:
            delivery_cache["baseline"] = baseline
            delivery_cache["at"] = delivery_at
    if delivery_at is not None:
        candidates.append(now_mono + max(0.1, delivery_at - now_wall))
    if project is not None:
        try:
            # A restart resets cooldown, but a persisted check wait still holds
            # the role until its own bound; fingerprint changes alone cannot
            # wake it then.
            hold = check_jobs.deferral(project, role_name)
            if hold is not None:
                wait_until = hold.get("wait_until")
                if (type(wait_until) in (int, float) and math.isfinite(wait_until)
                        and wait_until > 0):
                    candidates.append(now_mono + max(0.1, wait_until - now_wall))
        except (MizuError, OSError, ValueError):
            pass
    if project is not None:
        try:
            snapshot = project.snapshots.get()
            if snapshot.get("state") == "wait" and snapshot.get("wake_at") is not None:
                candidates.append(now_mono + max(0.1, snapshot["wake_at"] - now_wall))
        except (MizuError, OSError, ValueError):
            pass
    return min(candidates) if candidates else None


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
            policy = None
            delivery_cache: dict = {}
            readiness_memo: dict = {}
            while not stop.is_set():
                # Arm before the first state read: anything that changes during
                # the read/render/pass leaves a fingerprint difference that ends
                # the wait immediately, so no wake is lost between look and sleep.
                root = project.root if project is not None else None
                baseline = daemon_wake.fingerprint(root)
                try:
                    updated = load(config.file)
                    if updated.data != config.data:
                        raise ConfigError("Changing data_dir requires a daemon restart")
                    config = updated
                    lock_scope.close()
                    lock_scope.enter_context(config.runtime_policy())
                    from .probes import service_due
                    # Retain process-local history caches across unchanged reloads,
                    # but revalidate mutable project settings on every pass.
                    if project is None or project.config != config:
                        project = Project(config, name)
                    else:
                        project.refresh_settings()
                    role = config.roles[role_name]
                    policy = getattr(role, "daemon_policy", None)
                    if policy not in DAEMON_POLICIES:
                        raise ConfigError("Invalid daemon_policy; reload validated configuration")
                    control = project.control()
                    ready = False
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
                        if policy == "signals":
                            # Admission reads may scan retained records. A
                            # negative result is reused only while the state
                            # fingerprint and configuration are unchanged and
                            # no actionable deadline has lapsed: every wake
                            # that could change readiness invalidates it.
                            memo_key = (baseline, _config_stamp(config))
                            if readiness_memo.get("key") == memo_key:
                                ready = False
                            else:
                                ready = should_run(project, project.snapshots.get(),
                                                   role_name=role_name)
                                readiness_memo.clear()
                                if not ready:
                                    readiness_memo["key"] = memo_key
                        else:
                            ready = not control.get("draining")
                    if ready and time.monotonic() >= ordinary_after:
                        result = Engine(config, stop=stop).run(project, role_name, probes_serviced=True,
                                        enqueue_busy=True)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        delay = role_daemon_spacing_seconds(config, role)
                        if result.get("status") in ("waiting", "deferred"):
                            # The reported bound is the real reason to re-enter;
                            # an idle poll cannot replace it without starving it.
                            delay = max(0.1, result["next_evaluation_at"] - time.time())
                        # Cooldown gates dispatch, never just a state read.
                        ordinary_after = time.monotonic() + delay
                except (MizuError, OSError, ValueError) as exc:
                    print(json.dumps({"event": "run_deferred", "error": str(exc), "time": now()}), flush=True)
                # A single interruptible wait covers every idle path: a state
                # change, an explicit deadline, or a stop request ends it.
                root = project.root if project is not None else None
                deadline = _wake_deadline(config, project, name, role_name,
                                          ordinary_after, baseline, delivery_cache)
                reason = daemon_wake.wait(stop, root, baseline, deadline)
                if reason == "change" or reason == "deadline":
                    readiness_memo.clear()
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
