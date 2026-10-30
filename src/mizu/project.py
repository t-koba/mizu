"""Project lifecycle and operator-owned control files, separate from workspaces."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
import tomllib
import uuid
from pathlib import Path

from .budget import Budget
from .config import Config, _lineage_target, keys, strings
from .errors import ConfigError, Denied, LimitExceeded
from .fs import ID, SCRIPT_MAX, atomic_write, identifier, lock, mkdir, now, read_json, safe_read, sync_dir, write_json
from .insights import Insights
from .process import environment, run, stop_note
from .snapshot import (no_progress_recovery_due, open_store,
                       progress_disposition, progress_projection, validate_progress)
from .work_items import WorkItems


def session_persistence_scope(config: Config, role_name: str | None) -> str:
    """Classify whether every, some, or no role profile can retain a session.

    Selector profiles are considered statically; mixed choices remain
    conditional rather than presenting a rotation as certainly actionable.
    """
    role = config.roles.get(role_name) if isinstance(role_name, str) else None
    if role is None:
        return "unknown"
    if role.selector:
        spec = config.selectors.get(role.selector)
        if not isinstance(spec, dict):
            return "unknown"
        profiles = {candidate.get("profile")
                    for rule in spec.get("rules", ()) if isinstance(rule, dict)
                    for candidate in rule.get("candidates", ()) if isinstance(candidate, dict)}
    else:
        profiles = {role.profile}
    if not profiles:
        return "unknown"
    sessions = set()
    for profile in profiles:
        raw = config.profiles.get(profile) if isinstance(profile, str) else None
        session = raw.get("session") if isinstance(raw, dict) else None
        if session not in ("ephemeral", "persistent"):
            return "unknown"
        sessions.add(session)
    if sessions == {"persistent"}:
        return "persistent"
    if sessions == {"ephemeral"}:
        return "ephemeral"
    return "conditional"


#: Completed outcomes whose no-progress finish can idle with actionable work.
#: ``done`` never counts: the objective is complete even with open ledger rows.
#: ``bounded`` is not named by the accepted scope, so it never marks idle.
IDLE_NO_PROGRESS_OUTCOMES = ("wait", "continue")


def idle_with_open_work(snapshot: dict) -> dict:
    """Project the recorded idle-with-open-work diagnostic from durable facts.

    The published snapshot carries the outcome and progress record. Schema-2
    progress also carries the ready-work existence observation bound at writer
    completion: True/False from one bounded ``page_ready(limit=1)`` probe, or
    None if that probe was unreadable. No live ledger read happens here, so
    later work creation, drops, or due transitions cannot retroactively mark
    the old run idle. No work-item ids, titles or descriptions are included.

    Malformed or missing facts fail closed with a naming reason. Schema-1
    progress is readable but predates the observation. Only ``wait`` and
    ``continue`` can mark idle; ``bounded`` never does. This is diagnostic
    only and cannot affect FIFO ordering, locks, transitions, recovery or
    publication.
    """
    snapshot_id = snapshot.get("id") if isinstance(snapshot, dict) else None
    outcome = snapshot.get("outcome") if isinstance(snapshot, dict) else None
    progress = snapshot.get("progress") if isinstance(snapshot, dict) else None
    if not isinstance(snapshot_id, str):
        snapshot_id = None
    if not isinstance(outcome, str):
        outcome = None

    def result(reason, role=None, has_progress=None, ready_work=None):
        return {"active": reason == "no_progress_with_ready_work", "reason": reason,
                "snapshot": snapshot_id, "outcome": outcome,
                "role": role, "has_progress": has_progress,
                "ready_work": ready_work}

    if progress is None:
        return result("legacy_no_progress_record")
    if not isinstance(progress, dict):
        return result("invalid_progress_record")
    try:
        validate_progress(progress)
    except Denied:
        role = progress.get("role")
        return result("invalid_progress_record", role if isinstance(role, str) else None)

    role, has_progress = progress["role"], progress["has_progress"]
    if progress["schema"] == 1:
        return result("legacy_no_progress_record", role, has_progress)
    ready_work = progress["ready_work"]
    if outcome not in IDLE_NO_PROGRESS_OUTCOMES:
        return result("outcome_not_applicable", role, has_progress, ready_work)
    if has_progress:
        return result("has_progress", role, has_progress, ready_work)
    if ready_work is None:
        return result("unreadable_work_state", role, has_progress, None)
    if not ready_work:
        return result("no_ready_work", role, has_progress, False)
    return result("no_progress_with_ready_work", role, has_progress, True)


class Project:
    def __init__(self, config: Config, name: str, *, read_only: bool = False):
        self.config, self.name = config, identifier(name)
        self.root = config.data / "projects" / name
        self.workspace = self.root / "workspace"
        try:
            if read_only:
                raw_settings = safe_read(self.root, "project.toml", 65536)
                self.settings = tomllib.loads(raw_settings.decode("utf-8"))
            else:
                self.settings = tomllib.loads((self.root / "project.toml").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, Denied, LimitExceeded) as exc:
            complaint = "Cannot read bounded project settings" if read_only else f"Project is not initialized: {name}"
            raise ConfigError(complaint) from exc
        (self.roles, self.verify, self.recorded_batches,
         self.lineage_target) = check_project_settings(self.settings)
        if not self.roles or any(r not in config.roles for r in self.roles):
            raise ConfigError("Project roles must refer to configured roles")
        self.snapshots = open_store(self.root, config, create_dirs=not read_only)
        self.insights = Insights(self.root, retention_days=config.limits.retention_days,
                                 open_work_items=config.limits.open_work_items,
                                 producer_inputs_per_source=config.limits.producer_inputs_per_source,
                                 cursor_ttl_seconds=config.limits.insight_cursor_ttl_seconds,
                                 work_item_intent_default=config.insight_decisions["work_item_intent_default"],
                                 work_item_intent_by_source=config.insight_decisions["work_item_intent_by_source"])
        self.insights.snapshots = self.snapshots
        # Late acceptance is a separate projection; immutable snapshots stay unchanged.
        self.work_items = WorkItems(self.root, self.snapshots, self.insights, vcs=config.vcs,
                                    effective_verification=self.effective_verification,
                                    doing_limit=config.limits.work_items_doing,
                                    doing_limits={name: role.work_items_doing for name, role in config.roles.items()
                                                  if role.work_items_doing is not None},
                                    stale_seconds=config.limits.work_items_stale_seconds)
        self.insights.work_items = self.work_items
        from .role_state import RoleStateStore
        self.role_state = RoleStateStore(self.root / "role-state",
                                         max_bytes=config.limits.role_state_bytes)

    def vcs_settings(self, config: Config | None = None) -> dict:
        """Shared adapter policy with this project's trusted target, if configured."""
        return {**(config or self.config).vcs, "lineage_target": self.lineage_target}

    @property
    def goal(self) -> str:
        return read_goal(self.root / "PROJECT.md", "PROJECT.md must contain a nonempty goal within 64 KiB")

    def control(self) -> dict:
        return read_json(self.root / "control.json", {"armed": False, "paused": True, "wake_generation": "", "draining": False})

    def set_control(self, **updates) -> dict:
        """Merge control updates, owning the ``pause_cause`` lifecycle.

        ``pause_cause`` is present only for the current automatic
        failure-brake pause written by the health brake (``kind``
        ``failure_brake`` with bounded ``error_class`` and ``roles``; no
        error message). Any explicit ``paused`` update without a new
        ``pause_cause`` clears a prior field, so a manual pause and a
        resume never retain a stale cause; unrelated control updates
        preserve it.
        """
        with lock(self.root / "locks" / "control.lock"):
            value = {**self.control(), **updates, "updated_at": now()}
            if "paused" in updates and "pause_cause" not in updates:
                value.pop("pause_cause", None)
            write_json(self.root / "control.json", value)
            return value

    def reset_health(self) -> dict:
        """Reset per-role consecutive failure counters (resume rebase).

        Both the failure brake and the consecutive-missing-finish rotation
        streak reset to zero.

        Schema: returns ``{role: 0}`` for each reset record. Bounds: health
        dir only, ``*.json`` regular files. Trust: local operator state.
        Retry: best-effort per file, never raises. Evidence: rewritten health
        records keep ``last_run`` and note the reset. Failure: never raises;
        unreadable files are skipped.
        """
        reset: dict = {}
        try:
            paths = list((self.root / "health").glob("*.json"))
        except OSError:
            return reset
        for path in paths:
            try:
                if path.is_symlink():
                    continue
                record = read_json(path, {})
                if not isinstance(record, dict):
                    continue
                record = {**record, "consecutive_failures": 0,
                          "consecutive_missing_finish": 0,
                          "updated_at": now(), "resumed": True}
                write_json(path, record)
                reset[path.stem] = 0
            except (OSError, ValueError, TypeError):
                continue
        return reset

    def effective_verification(self, snapshot):
        from .acceptance import effective
        return effective(self, snapshot)

    def backpressure(self) -> dict:
        """Report configured producer/open-work congestion and current load.

        Both configured limits, the authoritative open/doing/blocked count,
        per-producer unresolved live input counts (undecided plus deferred;
        resolved and archive-only history excluded), and the currently held
        producers with their role names, reasons, and exact thresholds. Sources
        are generic role names; writable roles (the project writer) and
        operator/vcs/editor channels are never producers. Counts come only
        from the current project's Insights live inbox and WorkItems ledger;
        no private or ignored state is read. Both metrics are attempted for
        visibility even with disabled bounds. An unreadable metric reports
        ``None`` and ``load_ok`` False; admission is held only when the
        corresponding per-source bound or explicit per-role open-work policy
        enables that metric. Invalid relevant limits fail closed.
        """
        from .scheduling import ProducerFacts, evaluate_producer

        def _checked_limit(name):
            value = getattr(self.insights, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise Denied(f"Invalid {name}")
            if not 0 <= value <= 1073741824:
                raise Denied(f"Invalid {name}")
            return value

        def _raw_limit(name):
            try:
                return getattr(self.insights, name)
            except Exception:
                return None

        producers = []
        producer_policy = {}
        try:
            for name in self.roles:
                role = self.config.roles.get(name)
                if (role is not None and getattr(role, "workspace", None) != "write"
                        and "submit_insight" in set(getattr(role, "capabilities", ()) or ())):
                    held_at_capacity = getattr(role, "held_at_capacity", False)
                    producer_policy[name] = (held_at_capacity if type(held_at_capacity) is bool else False,
                                             type(held_at_capacity) is bool)
                    producers.append(name)
            producers.sort()
        except Exception:
            producers = []
            producer_policy = {}
        per_source_valid = open_limit_valid = True
        try:
            per_source_limit = _checked_limit("producer_inputs_per_source")
        except Exception:
            per_source_valid = False
            per_source_limit = _raw_limit("producer_inputs_per_source")
        try:
            open_limit = _checked_limit("open_work_items")
        except Exception:
            open_limit_valid = False
            open_limit = _raw_limit("open_work_items")
        limits_ok = per_source_valid and open_limit_valid
        counts: dict = {}
        open_count = None
        counts_ok = open_ok = True
        try:
            # One inbox scan for all configured producers, even when disabled.
            counts = dict(self.insights.unresolved_counts(producers)) if producers else {}
        except Exception:
            counts_ok = False
        try:
            open_count = self.work_items.count_open_readonly()
        except Exception:
            open_ok = False
        # Status readability is independent of the metrics admission needs.
        held = []
        for name in producers:
            held_at_capacity, policy_valid = producer_policy[name]
            role_open_limit = open_limit if held_at_capacity and open_limit_valid else 0
            role_load_ok = (per_source_valid and (not per_source_limit or counts_ok)
                            and policy_valid
                            and (not held_at_capacity or
                                 (open_limit_valid and (not open_limit or open_ok))))
            decision = evaluate_producer(ProducerFacts(
                is_producer=True, per_source_limit=per_source_limit,
                open_work_limit=role_open_limit, held_at_capacity=held_at_capacity,
                source_pending=counts.get(name, 0), open_count=open_count or 0,
                load_ok=role_load_ok))
            if decision.held:
                thresholds = {}
                if "per_source_full" in decision.reasons:
                    thresholds["producer_inputs_per_source"] = per_source_limit
                if "open_work_full" in decision.reasons:
                    thresholds["open_work_items"] = open_limit
                if "load_unavailable" in decision.reasons:
                    if not per_source_valid or per_source_limit:
                        thresholds["producer_inputs_per_source"] = per_source_limit
                    if held_at_capacity and (not open_limit_valid or open_limit):
                        thresholds["open_work_items"] = open_limit
                held.append({"source": name, "reasons": list(decision.reasons),
                             "pending": counts.get(name), "thresholds": thresholds})
        return {"limits": {"producer_inputs_per_source": per_source_limit,
                           "open_work_items": open_limit},
                "open_work_count": open_count,
                "producer_inputs": [{"source": name, "pending": counts.get(name)} for name in producers],
                "held": held, "load_ok": limits_ok and counts_ok and open_ok}

    def work_order_status(self) -> dict:
        """Operator-readable freshness for the writer's explicit order.

        Never writes or refreshes the work-order plan, or changes scheduling.
        Fresh source projections retain their existing lock, recovery and
        pruning behavior. ``absent`` means no plan, ``current`` means the
        stored generation equals the present writer inputs, ``review_due``
        means inputs moved, and ``unavailable`` means corrupt/unsafe state or
        an uncomputable present (fail closed, never empty/current).
        """
        try:
            from . import work_order as _plan
            return _plan.status_for_project(self)
        except Exception:
            return {"status": "unavailable", "rev": None, "generation": None}

    def status(self) -> dict:
        from .admission import waiting as admission_waiting
        from .check_jobs import deferrals, outstanding
        from .selection import project_status
        from .run_health import finish_only_rates
        snapshot = self.snapshots.get()
        from .acceptance import effective_projection
        effective, effective_diagnostic = effective_projection(self, snapshot)
        budget = Budget(self.config.data / "budget", self.config.limits.daily_requests,
                        self.config.limits.retention_days,
                        self.config.limits.shared_daily_requests, self.config.timezone).usage(self.name)
        progress = snapshot.get("progress")
        progress_count = progress.get("no_progress_count", 0) if isinstance(progress, dict) else 0
        progress_role = progress.get("role") if isinstance(progress, dict) else None
        recovery_threshold_due = no_progress_recovery_due(
            progress, snapshot["outcome"], self.config, progress_role)
        session_scope = session_persistence_scope(self.config, progress_role)
        recovery_status = ("not_due" if not recovery_threshold_due else
                           "due" if session_scope == "persistent" else
                           "not_applicable" if session_scope == "ephemeral" else
                           "conditional" if session_scope == "conditional" else "unknown")
        progress_disposition_value = progress_disposition(
            progress, snapshot["outcome"], self.config, progress_role)
        # Recorded-at-finish diagnostic: no live ledger read, so later work
        # creation, drops, or due transitions cannot retroactively mark the
        # old run idle. The observation was bound into the progress record
        # when this snapshot was created.
        idle_diagnostic = idle_with_open_work(snapshot)
        return {"project": self.name, "control": self.control(), "snapshot": snapshot["id"],
                "code_digest": snapshot["code_digest"], "created_at": snapshot["created_at"],
                "outcome": snapshot["outcome"], "summary": snapshot["summary"],
                "verification": snapshot["verification"],
                "effective_verification": effective,
                "effective_verification_diagnostic": effective_diagnostic, "state": snapshot["state"],
                "progress": {"status": "current" if progress is not None else "legacy",
                             "record": progress_projection(progress),
                             "disposition": progress_disposition_value,
                             "recovery": {"status": recovery_status,
                                          "threshold_due": recovery_threshold_due,
                                          "pending_work": recovery_threshold_due,
                                          "session_scope": session_scope,
                                          "no_progress_count": progress_count,
                                          "after": self.config.limits.no_progress_recovery_after}},
                "idle_with_open_work": idle_diagnostic,
                "finish_only_unit_rate": finish_only_rates(self),
                "goal_digest": snapshot.get("goal_digest"), "wake_at": snapshot.get("wake_at"),
                # A role the check queue holds now, with the bound that ends it.
                "check_waits": deferrals(self),
                "check_results": outstanding(self),
                # The roles waiting for an execution slot now, so a waiter is
                # distinguishable from a running role and from one that was
                # never admitted.
                "admission": admission_waiting(self.config, self.name),
                "selection": project_status(self.config, self),
                "backpressure": self.backpressure(),
                "health": {p.stem: read_json(p) for p in (self.root / "health").glob("*.json") if not p.is_symlink()},
                "active": {p.stem: read_json(p) for p in (self.root / "active").glob("*.json") if not p.is_symlink()},
                "pending_insights": self.insights.list(limit=self.config.limits.pending_insights),
                "work_order": self.work_order_status(),
                "blocked_work": self.work_items.blocked_liveness_readonly(),
                "budget": {"used_requests": budget["used"], "limit_requests": budget["limit"],
                           "shared_used_requests": budget["shared_used"],
                           "shared_limit_requests": budget["shared_limit"],
                           "day": budget["day"], "bytes": budget["bytes"],
                           "reaped": budget["reaped"]}}


#: Maximum operator acceptance commands per project (each spawns a container).
MAX_VERIFY_COMMANDS = 32


def check_verify(value) -> tuple[str, ...]:
    """Validate acceptance commands: typed, counted, and TOML-safe."""
    commands = strings(value, "project.verify")
    if len(commands) > MAX_VERIFY_COMMANDS:
        raise ConfigError("project.verify holds too many commands")
    if any("\x00" in command or "\n" in command for command in commands):
        raise ConfigError("project.verify commands must not contain NUL or newlines")
    return commands


#: Maximum named recorded batches per project (operator-owned reusable batch definitions).
MAX_RECORDED_BATCHES = 32

#: Maximum commands per named recorded batch (matches the ordered-batch bound).
MAX_RECORDED_BATCH_COMMANDS = 32


def check_recorded_batches(value) -> dict[str, tuple[str, ...]]:
    """Validate operator-owned named custom batches: typed, counted, TOML-safe.

    The table maps lowercase identifiers (the existing ``ID`` convention) to
    nonempty ordered lists of 1-32 shell commands. Each command is nonblank,
    NUL-free, valid UTF-8 and at most ``SCRIPT_MAX`` UTF-8 bytes. Missing
    (``None``) means no named batches. Anything malformed fails closed with
    ``ConfigError``; the ``project.toml`` 64 KiB size bound is unchanged.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("project.recorded_batches must be a table")
    if len(value) > MAX_RECORDED_BATCHES:
        raise ConfigError("project.recorded_batches holds too many definitions")
    batches: dict[str, tuple[str, ...]] = {}
    for name, commands in value.items():
        if not isinstance(name, str) or ID.fullmatch(name) is None:
            raise ConfigError("project.recorded_batches names must be lowercase identifiers")
        if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_RECORDED_BATCH_COMMANDS:
            raise ConfigError(f"project.recorded_batches[{name}] must list 1-32 commands")
        normalized: list[str] = []
        for entry in commands:
            if not isinstance(entry, str):
                raise ConfigError(f"project.recorded_batches[{name}] commands must be strings")
            try:
                encoded = entry.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ConfigError(f"project.recorded_batches[{name}] commands must be valid UTF-8") from exc
            if "\x00" in entry or not entry.strip() or len(encoded) > SCRIPT_MAX:
                raise ConfigError(f"project.recorded_batches[{name}] commands must be nonblank within 64 KiB")
            normalized.append(entry)
        batches[name] = tuple(normalized)
    return batches


def check_project_settings(
        settings) -> tuple[list[str], tuple[str, ...], dict[str, tuple[str, ...]], dict | None]:
    """Validate the complete operator-owned project.toml schema in one place."""
    keys(settings, {"roles", "verify", "attributes", "recorded_batches", "vcs", "ci_acceptance"}, "project")
    from .selection import attributes
    attributes(settings.get("attributes", {}))
    roles = strings(settings.get("roles", []), "project.roles")
    verify = check_verify(settings.get("verify", []))
    from .ci_acceptance import configured
    configured(settings, verify)
    recorded_batches = check_recorded_batches(settings.get("recorded_batches"))
    project_vcs = settings.get("vcs", {})
    keys(project_vcs, {"lineage_target"}, "project.vcs")
    lineage_target = (_lineage_target(project_vcs["lineage_target"], "project.vcs.lineage_target")
                      if "lineage_target" in project_vcs else None)
    return roles, verify, recorded_batches, lineage_target


def read_goal(path: Path, complaint: str) -> str:
    """Read an operator goal file with a pre-read size cap, then validate."""
    try:
        oversized = path.stat().st_size > 65536
    except OSError:
        oversized = False
    if oversized:
        raise ConfigError(complaint)
    value = path.read_text(encoding="utf-8")
    if not value.strip() or len(value.encode()) > 65536:
        raise ConfigError(complaint)
    return value


def git_env() -> dict[str, str]:
    # Refuse nested bare-repo discovery (GHSA-9ccr-r5hg-74gf): only
    # explicitly allowlisted bare repos are used. Highest precedence
    # over all config-file sources via GIT_CONFIG_* env.
    return environment(extra={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                              "GIT_TERMINAL_PROMPT": "0",
                              "GIT_CONFIG_COUNT": "1",
                              "GIT_CONFIG_KEY_0": "safe.bareRepository",
                              "GIT_CONFIG_VALUE_0": "explicit"})


def config_managed_repo(workspace: Path, env: dict[str, str]) -> None:
    for key, value in (("user.name", "Mizu"), ("user.email", "automation@localhost"),
                       ("core.hooksPath", os.devnull)):
        configured = run(["git", "-C", str(workspace), "config", key, value],
                         timeout=15, maximum=8192, env=env)
        if configured.exit_code != 0:
            raise Denied("Cannot configure managed repository"
                         + (": " + stop_note(configured) if stop_note(configured) else ""))


def _import_git_tree(source: Path, workspace: Path, env: dict[str, str]) -> None:
    """Clone a clean git tree without history leakage. Loud failure only."""
    if not shutil.which("git"):
        raise Denied("Git is required for git-tree import; make a separate plain-directory export first")
    status = run(["git", "-c", "safe.bareRepository=explicit", "-c", "core.fsmonitor=false",
                  "-c", f"core.hooksPath={os.devnull}",
                  "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
                 # Only emptiness matters; truncation preserves truthiness.
                 timeout=30, maximum=65536, env=env)
    if status.exit_code != 0 or status.stdout.strip():
        # An incomplete status output cannot prove the tree is clean.
        raise Denied("Git import requires a clean source tree; commit or make a separate plain-directory export first"
                     + (": " + stop_note(status) if stop_note(status) else ""))
    result = run(["git", "-c", "safe.bareRepository=explicit", "-c", "core.fsmonitor=false",
                  "-c", f"core.hooksPath={os.devnull}",
                  "clone", "--no-local",
                  "--no-hardlinks", "--", str(source), str(workspace)],
                 timeout=300, maximum=1048576, env=env)
    if result.exit_code != 0:
        raise Denied(f"Repository import failed: {stop_note(result) or result.stderr}")
    run(["git", "-C", str(workspace), "remote", "remove", "origin"],
        timeout=15, maximum=8192, env=env)
    config_managed_repo(workspace, env)


def _import_plain_directory(source: Path, workspace: Path, stage: Path, config: Config,
                            *, exclude_dirs: tuple[str, ...] = ()) -> None:
    """Copy only the visible snapshot, not caches or secrets."""
    temporary_store = open_store(stage, config)
    captured = temporary_store.capture_files(source, exclude_dirs=exclude_dirs)
    if captured["skipped"]:
        raise Denied("Plain-directory import contains unsupported links or special files: " + ", ".join(captured["skipped"][:10]))
    if captured.get("reserved"):
        raise Denied("Plain-directory import hit the reserved virtual-ref prefix refs/remotes/upstream/: "
                     + ", ".join(captured["reserved"][:10])
                     + "; remove the legacy subtree or relocate genuine files out of it first")
    temporary_store.materialize(captured, workspace)


@contextlib.contextmanager
def _import_source(config: Config, source: Path):
    """Fence managed source capture, without initializing delegation state.

    Inspect lexical and resolved paths: aliases into managed roots/workspaces
    (or escapes out of workspaces) must not bypass the fence. Collection-level
    imports are ambiguous and refused without traversing the collection.
    Only path metadata is inspected before workspace.lock; content inspection
    and capture stay inside it, ordered before the delegation store lock.
    """
    projects = (config.data / "projects").absolute()
    roots = set()
    try:
        base = projects.resolve()
        absolute = source.absolute()
        for prefix in (absolute, *absolute.parents):
            for path in (prefix, prefix.resolve()):
                for parent in (projects, base):
                    if path.is_relative_to(parent):
                        parts = path.relative_to(parent).parts
                        if len(parts) >= 2 and parts[1] == "workspace":
                            roots.add(base / parts[0])
        resolved = source.resolve()
        # Do not recursively discover projects or choose one lock for a whole
        # collection. Even an empty collection is not an external source.
        for path in (absolute, resolved):
            for parent in (projects, base):
                if parent.is_relative_to(path):
                    raise Denied("Ambiguous managed import source contains project collection")
        # Only an exact project-root source recursively contains workspace.
        # Do not classify unrelated subdirectories (for example snapshots)
        # merely because one of their ancestors is the project root.
        root_imports = set()
        for path in (absolute, resolved):
            for parent in (projects, base):
                if path.is_relative_to(parent):
                    parts = path.relative_to(parent).parts
                    if len(parts) == 1:
                        root_imports.add(base / parts[0])
        root_import = bool(root_imports)
        roots.update(root_imports)
    except (OSError, RuntimeError):
        raise Denied("Unreadable import source path") from None
    if not roots:
        yield resolved, ()
        return
    try:
        if len(roots) != 1:
            raise Denied("Ambiguous managed import source")
        root = roots.pop()
        workspace = root / "workspace"
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise Denied("Unsafe managed import source")
        locks = root / "locks"
        if locks.is_symlink() or (locks.exists() and not stat.S_ISDIR(locks.lstat().st_mode)):
            raise Denied("Unsafe managed import locks")
        with lock(locks / "workspace.lock"):
            resolved = source.resolve()
            if (not stat.S_ISDIR(workspace.lstat().st_mode)
                    or not (resolved.is_relative_to(workspace)
                            or (root_import and resolved == root))):
                raise Denied("Unsafe managed import source")
            from .delegation_jobs import DelegationJobs
            DelegationJobs.guard(root)
            # A missing/unsafe initialization marker is unknown, not external.
            safe_read(root, "project.toml", 65536)
            # Internal locks are control state, not imported content. Never
            # reopen the held workspace lock during a project-root capture;
            # same-named directories inside actual source remain visible.
            yield resolved, ("locks",) if resolved == root else ()
    except (OSError, RuntimeError, LimitExceeded):
        raise Denied("Unreadable managed import source or reservation") from None


def initialize(config: Config, name: str, source: Path, goal_file: Path,
               roles: list[str], verification: list[str], *, armed: bool = False) -> Project:
    identifier(name)
    with _import_source(config, source) as (source, exclude_dirs):
        goal_file = goal_file.resolve()
        if not source.is_dir() or not goal_file.is_file():
            raise ConfigError("Source directory and goal file must exist")
        goal = read_goal(goal_file, "Goal must be nonempty and at most 64 KiB")
        if not roles or any(role not in config.roles for role in roles):
            raise ConfigError("Choose existing roles")
        check_verify(verification)
        parent = config.data / "projects"
        mkdir(parent)
        destination = parent / name
        with lock(config.data / "locks" / f"init-{name}.lock", blocking=False):
            if destination.exists():
                raise Denied("Project already exists; initialization never overwrites it")
            stage = Path(tempfile.mkdtemp(prefix=".init-", dir=parent))
            try:
                env = git_env()
                if (source / ".git").exists():
                    _import_git_tree(source, stage / "workspace", env)
                else:
                    _import_plain_directory(source, stage / "workspace", stage, config,
                                            exclude_dirs=exclude_dirs)
                atomic_write(stage / "PROJECT.md", goal.encode())
                text = "roles = " + json.dumps(roles) + "\nverify = " + json.dumps(verification) + "\n"
                atomic_write(stage / "project.toml", text.encode())
                write_json(stage / "control.json", {"armed": armed, "paused": not armed,
                                                    "draining": False,
                                                    "wake_generation": uuid.uuid4().hex, "updated_at": now(),
                                                    "reason": "Operator initialized armed" if armed else "Initialized unarmed; operator arm required"})
                mkdir(stage / "spool" / "editor")
                atomic_write(stage / "spool/editor/.mizu-outbox", b"Mizu Editor proposal outbox\n")
                store = open_store(stage, config)
                captured = store.capture_files(stage / "workspace")
                initial = store.create(captured, goal=goal, state="Initialized; no autonomous work has run.",
                                       run=None, outcome="wait", summary="Project initialized, unarmed.")
                store.publish(initial)
                os.rename(stage, destination)
                sync_dir(parent)
            except BaseException:
                shutil.rmtree(stage, ignore_errors=True)
                raise
        return Project(config, name)
