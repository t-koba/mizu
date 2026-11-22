"""Engine consultation and one-shot delegation child execution.

This mixin owns the two caller-side collaboration paths that remained in
``runtime`` after the engine extraction: consultation over retained inputs and
the live-workspace implementation child. ``Engine`` inherits it, so every
existing ``Engine.consult`` / ``Engine.delegate`` reference and
``patch.object(Engine, ...)`` call site keeps resolving unchanged. The gate
constants and checks stay in ``engine_delegation`` and are re-exported by
``runtime``. No behaviour, grant or default changes.
"""
from __future__ import annotations

import contextlib
import dataclasses
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - annotations only, never a runtime cycle.
    from .runtime import Context


class EngineCollaborationMixin:
    def consult(self, parent: Context, args: dict) -> dict:
        # Runtime still owns these bindings; resolve them at call time so
        # existing ``mizu.runtime`` patch seams keep intercepting this call.
        from .runtime import (AuthorityGrantMismatch, Busy, Cancelled, ConfigError,
                              DEFINITIONS, Context, Denied, RunDeadlineExceeded,
                              check_consult_role, delegation_gates, mkdir, now,
                              prompt_for, text_preview, validate, write_json)

        # Direct callers must pass the same fence as Context dispatch.
        validate(args, DEFINITIONS["consult"][1])
        delegation_gates.check_access(parent._reservation_view, parent.role.name,
                                      parent.snapshot, "consult", args)
        # The child unit takes its model from each answering role's own
        # profile or selector, exactly as a scheduled run of that role would:
        # fixed profiles run directly, selector roles resolve through a
        # bounded preview (no routing consumption, no classifier inference),
        # so retry/on_error/fallback apply with no new mechanism and the model
        # is stated once on the role instead of twice (role plus allowlist).
        if args.get("roles") is not None and args.get("role"):
            raise Denied("Consultation takes roles or role, not both")
        if args.get("roles") is not None:
            role_names = list(args.get("roles") or [])
            if (not role_names or len(set(role_names)) != len(role_names)
                    or any(not isinstance(n, str) for n in role_names)):
                raise Denied("Consultation requires unique answering roles")
        else:
            single = args.get("role") or getattr(self.config, "consult_role", "")
            if not single:
                raise Denied("Consultation role requires explicit roles or a configured consult_role")
            role_names = [single]
        for role_name in role_names:
            if role_name not in self.config.roles:
                raise ConfigError(f"Consultation role is not configured: {role_name}")
            check_consult_role(role_name, self.config.roles[role_name])
        if not parent._slot_lease or parent.closed:
            raise Busy("Consultation requires a live admitted caller execution slot")
        reserved = parent._reservation_view is not None
        if reserved:
            # get validates the retained manifest and its content-addressed ID.
            # Never touch the parent's live execution or workspace on this path.
            snap = parent.project.snapshots.get(args["source_snapshot"])
        else:
            parent.close_exec()
            capture = parent.project.snapshots.capture_files(parent.workspace)
            snap = parent.project.snapshots.create(capture, goal=parent.goal, state=parent.snapshot["state"],
                                                  run=parent.run_dir.name, outcome="wait", summary="Consultation input")

        @self.config.runtime_policy()
        def ask(role_name: str) -> dict:
            run_dir = parent.project.root / "runs" / uuid.uuid4().hex
            workspace = run_dir / "input"
            ctx = None
            role = self.config.roles[role_name]
            profile = ""
            try:
                mkdir(run_dir)
                if role.selector:
                    # Config-only gate: preview() also requires project
                    # enablement, but a consultation child is ephemeral
                    # (caller slot, no role lock, no dispatch gate), so any
                    # configured read-only role qualifies as the docs say.
                    # Resolve through the same bounded preview without the
                    # project-membership check and without consuming routing.
                    from .classification import prepare as _prepare_selection
                    decision = _prepare_selection(
                        self, parent.project, role,
                        snap if reserved else parent.project.snapshots.get(), None, preview=True)
                    profile = decision.get("profile") or ""
                    if not profile:
                        raise Denied("Consultation selector has no selectable profile: "
                                     + str(decision.get("reason") or "waiting"))
                    role = dataclasses.replace(role, profile=profile, selector="")
                else:
                    profile = role.profile
                write_json(run_dir / "started.json", {"run": run_dir.name,
                           "parent_run": parent.run_dir.name, "role": role.name,
                           "profile": profile, "snapshot": snap["id"], "started_at": now()})
                parent.project.snapshots.materialize(snap, workspace)
                ctx = Context(self.config, parent.project, role,
                              run_dir, snap, workspace, stop=parent.stop, goal=parent.goal)
                ctx.ephemeral = True
                ctx.parent_run = parent.run_dir.name
                ctx.dispatch_class = "consult"
                model = self._execute(ctx, self.resolve(profile), prompt_for(ctx) + "\n\nRequest:\n" + args["request"], profile=profile)
                if ctx.host_cancelled() or (ctx.finished is None and ctx.cancelled()):
                    raise Cancelled("Consultation cancelled before completion")
                if ctx.finished is None:
                    raise Denied("Consultation did not finish")
                result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                          "role": role.name, "status": "completed", "finished_at": now(),
                          "profile": profile, "snapshot": snap["id"],
                          "answer": ctx.finished["summary"], "model": model,
                          "activity": ctx.activity_summary()}
                write_json(run_dir / "consultation.json", result)
                return result
            except Exception as exc:
                result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                          "role": role_name, "status": "bounded" if isinstance(exc, (RunDeadlineExceeded, AuthorityGrantMismatch)) else "interrupted",
                          "finished_at": now(), "profile": profile, "snapshot": snap["id"], "error": str(exc)}
                if ctx is not None:
                    result["activity"] = ctx.activity_summary()
                    if hasattr(ctx, "model_evidence"):
                        result["model"] = ctx.model_evidence
                try:
                    mkdir(run_dir)
                    write_json(run_dir / "error.json", result)
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
                return result
            finally:
                if ctx is not None:
                    ctx.closed = True
                    ctx.close_exec()
                shutil.rmtree(workspace, ignore_errors=True)
        with ThreadPoolExecutor(max_workers=self.config.limits.parallel_consults) as pool:
            results = list(pool.map(ask, role_names))
        record = {"snapshot": snap["id"], "question": args["request"], "roles": list(role_names), "answers": [{"run": r["run"], "role": r["role"], "profile": r["profile"], **({"error": r["error"]} if "error" in r else {})} for r in results]}
        write_json(parent.run_dir / "consultations" / f"{uuid.uuid4().hex}.json", record)
        answers=[]
        for result in results:
            answer={k:result[k] for k in ('run','role','profile','snapshot') if k in result}
            if 'answer' in result:
                answer['answer']=result['answer']
            if 'error' in result:
                answer['error'],answer['error_truncated']=text_preview(result['error'],2048)
            answers.append(answer)
        return {**record,'answers':answers}

    def delegate(self, parent: Context, args: dict) -> dict:
        # Runtime still owns these bindings; resolve them at call time so
        # existing ``mizu.runtime`` patch seams keep intercepting this call.
        from .runtime import (Busy, Denied, delegation_gates)
        from .fs import canonical, digest

        # One-shot implementation child in the caller's live workspace. The
        # caller waits holding its workspace lock, so exactly one writer
        # touches the tree; verification and publication stay with the
        # caller. The child takes grants, profile or selector, and policy
        # from the delegate role's own configuration, minus the forbidden
        # set, plus a mechanism-provided terminal finish seal that returns
        # to the caller. Spend counts against the caller's unit through the
        # shared counters; nested delegation and consultation stay disabled
        # so no spend escapes the unit.
        role = self._delegate_role(parent.role, args.get("role"))
        task = args.get("task") or ""
        if not task.strip():
            raise Denied("Delegation needs a self-contained task")
        if not parent._slot_lease or parent.closed:
            raise Busy("Delegation requires a live admitted caller execution slot")
        delegation_gates.refuse_live_tree(parent._reservation_view, "Delegation capture")
        if args.get("async_mode") is True:
            # Opt-in only, and the caller must be able to join what it starts:
            # refuse before any reservation or child launch when the grant is
            # absent, never leaving an unjoinable child holding the tree.
            if "delegate_join" not in parent.role.capabilities:
                raise Denied("Async delegation requires the delegate_join capability")
            # The durable reservation pins the caller's own admitted snapshot;
            # no extra live capture competes with the child. The default path
            # below stays byte-for-byte synchronous.
            from .delegation_async import start
            snap = parent.snapshot
            key = digest(canonical([parent.run_dir.name, role.name, task, snap["id"]]))[:32]
            return start(self, parent, key, role.name, task, snap)
        parent.close_exec()
        capture = parent.project.snapshots.capture_files(parent.workspace)
        snap = parent.project.snapshots.create(capture, goal=parent.goal, state=parent.snapshot["state"],
                                              run=parent.run_dir.name, outcome="wait", summary="Delegation input")
        run_dir = parent.project.root / "runs" / uuid.uuid4().hex
        return self._delegate_child(parent, role, task, snap, run_dir)

    def _delegate_child(self, parent: Context, role, task: str, snap: dict, run_dir,
                        *, resolved_profile: str | None = None, context_sink=None,
                        role_guard=None) -> dict:
        """One ephemeral child execution, shared by sync and in-process async."""
        from .runtime import (AuthorityGrantMismatch, Busy, Cancelled, Denied,
                              DrainYieldExceeded, RequestYieldExceeded,
                              RunDeadlineExceeded, ToolYieldExceeded, Context,
                              check_jobs, cleanup, container_runtime,
                              lock, mkdir, now, prompt_for, role_exclusion,
                              write_json)

        role_name = role.name
        ctx = None
        profile = ""
        # The child is an invocation of its own role, so it takes that role's
        # exclusion for its whole life: a sibling cleanup cannot remove the
        # child's containers while it runs, and a role runs once at a time in
        # a project, delegated children included.
        guard = role_guard
        if guard is None:
            guard = contextlib.ExitStack()
            try:
                guard.enter_context(lock(role_exclusion(parent.project.root, role_name),
                                         blocking=False))
            except Busy as exc:
                guard.close()
                raise Denied("The delegate role is active; delegation needs the role free") from exc
        try:
            mkdir(run_dir)
            # Synchronous routing still uses the published current snapshot,
            # exactly as before the queued-reservation path existed; the new
            # unpublished capture stays the child's input snapshot only. An
            # async child instead uses the profile resolved into its durable
            # request, so a later configuration drift cannot reselect it.
            if resolved_profile is None:
                role = self._delegate_profile(parent.project, role, parent.project.snapshots.get())
            else:
                role = dataclasses.replace(role, profile=resolved_profile, selector="")
            profile = role.profile
            write_json(run_dir / "started.json", {"run": run_dir.name,
                       "parent_run": parent.run_dir.name, "delegation": True,
                       "role": role.name, "profile": profile, "snapshot": snap["id"],
                       "workspace": str(parent.workspace), "started_at": now()})
            driver = self.resolve(profile)
            if getattr(driver, "requires_sandbox", True) and any(
                    c in role.capabilities for c in ("exec", "experiment", "verify")):
                container_runtime(self.config)
                cleanup(self.config, parent.project.root, role_name, owner=True)
            ctx = Context(self.config, parent.project, role,
                          run_dir, snap, parent.workspace, stop=parent.stop, goal=parent.goal)
            ctx.ephemeral = True
            ctx.parent_run = parent.run_dir.name
            ctx.dispatch_class = "delegate"
            ctx.consult = None
            ctx.spend_role = parent.role
            ctx.delegated_child = True
            ctx._spend = parent._spend
            if context_sink is not None:
                context_sink(ctx)
            try:
                model = self._execute(ctx, driver, prompt_for(ctx) + "\n\nTask:\n" + task
                                       + "\n\nCall finish once with a result summary (and short state) "
                                       + "when done; that seal returns to the caller, it publishes nothing.")
            finally:
                # Shared spend bounds still constrain the caller's seal (a
                # child's deadline is local). Pending admission refusals are
                # invocation-local: the returned child result is their evidence,
                # not authority to relabel a later caller provider failure.
                if ctx.bound_hit != "deadline":
                    parent.bound_hit = parent.bound_hit or ctx.bound_hit
            if ctx.host_cancelled() or (ctx.finished is None and ctx.cancelled()):
                raise Cancelled("Delegation cancelled before completion")
            if ctx.finished is None:
                raise Denied("Delegation did not finish")
            result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                      "role": role.name,
                      "status": "bounded" if ctx.finished["outcome"] == "bounded" else "completed",
                      **({"bound": ctx.finished["bound"]} if "bound" in ctx.finished else {}),
                      "finished_at": now(), "profile": profile, "snapshot": snap["id"],
                      "answer": ctx.finished["summary"], "model": model,
                      "activity": ctx.activity_summary(),
                      "ci_requests": ctx.ci_request_summary()}
            result["check_jobs"] = [j for j in check_jobs.list_jobs(parent.project, parent.role.name) if j["run"] == run_dir.name]
            write_json(run_dir / "delegation.json", result)
            record = {"task": task, "role": role.name, "run": run_dir.name,
                      "snapshot": snap["id"], "profile": profile, "answer": ctx.finished["summary"],
                      "status": result["status"],
                      **({"bound": result["bound"]} if "bound" in result else {}),
                      "check_jobs": result["check_jobs"]}
            write_json(parent.run_dir / "delegations" / f"{uuid.uuid4().hex}.json", record)
            return record
        except Exception as exc:
            bound = next((kind for error, kind in (
                (RunDeadlineExceeded, "deadline"), (RequestYieldExceeded, "requests"),
                (ToolYieldExceeded, "tools"), (DrainYieldExceeded, "drain"),
                (AuthorityGrantMismatch, "authority")) if isinstance(exc, error)), None)
            result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                      "role": role_name, "status": "bounded" if bound is not None else "interrupted",
                      **({"bound": bound} if bound is not None else {}),
                      "finished_at": now(), "profile": profile, "snapshot": snap["id"], "error": str(exc)}
            if ctx is not None:
                result["activity"] = ctx.activity_summary()
                result["ci_requests"] = ctx.ci_request_summary()
                if hasattr(ctx, "model_evidence"):
                    result["model"] = ctx.model_evidence
            try:
                mkdir(run_dir)
                write_json(run_dir / "error.json", result)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            return result
        finally:
            try:
                if ctx is not None:
                    ctx.closed = True
                    ctx.close_exec()
            finally:
                guard.close()
