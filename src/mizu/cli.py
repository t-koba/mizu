"""The CLI is the human control plane. JSON goes to stdout; errors to stderr.
One-shot commands print a single JSON document; daemon streams JSON Lines."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import uuid
from pathlib import Path

from . import __version__
from . import platform as _platform
from .config import load
from .errors import ConfigError, Denied, MizuError
from .fs import PREVIEW_BYTES, atomic_write, mkdir

ROOT = Path(__file__).resolve().parents[2]


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="mizu", description="Small mechanisms. Explicit policies. Autonomous work.")
    cli.add_argument("--version", action="version", version="mizu " + __version__)
    cli.add_argument("--config", type=Path, default=_platform.default_config_file(), help="Private config file (default: platform config dir)")
    sub = cli.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure", help="Create a private configuration without overwriting existing files")
    configure.add_argument("--pi-command-json", default=None, help="Pi argv as a JSON string array")
    initialize = sub.add_parser("init", help="Import a source tree into a managed project (unarmed unless --armed)")
    initialize.add_argument("project", help="New project name")
    initialize.add_argument("--source", type=Path, required=True, help="Clean source directory or git tree")
    initialize.add_argument("--goal", type=Path, required=True, help="Goal Markdown file")
    # No hardcoded role assumption: omitted --roles resolves to the single
    # configured role, else to worker when present, else requires explicit.
    initialize.add_argument("--roles", default=None, help="Comma-separated configured roles (default: single role, else worker when present)")
    initialize.add_argument("--armed", action="store_true",
                            help="Start armed (operator has reviewed the source at init time)")
    initialize.add_argument("--verify", action="append", default=[], help="Operator-owned acceptance command; repeatable")
    for name in ("status", "arm", "pause", "resume", "wake", "disarm"):
        p = sub.add_parser(name, help={"status": "Show control, snapshot, budget and health",
                                       "arm": "Arm after reviewing configuration",
                                       "pause": "Pause without disarming",
                                       "resume": "Resume an armed project",
                                       "wake": "Request another work unit",
                                       "disarm": "Disarm and pause"}[name])
        p.add_argument("project", help="Managed project name")
    for name in ("run", "daemon", "cleanup"):
        p = sub.add_parser(name, help={"run": "Run one work unit (single JSON to stdout)",
                                       "daemon": "Poll and run; streams JSON Lines per unit (result objects and run_deferred events)",
                                       "cleanup": "Remove leftover labelled containers"}[name])
        p.add_argument("project", help="Managed project name")
        # No hardcoded role assumption: omitted --role resolves to the single
        # configured role, else to worker when present, else requires explicit.
        p.add_argument("--role", default=None, help="Configured role (default: single role, else worker when present)")
        if name == "run":
            p.add_argument("--attributes", type=Path, help="Operator JSON attributes for this execution")
    p = sub.add_parser("selection", help="Preview selection or manage operator observations; no inference")
    selection = p.add_subparsers(dest="selection_command", required=True)
    p = selection.add_parser("preview")
    p.add_argument("project")
    p.add_argument("--role")
    p.add_argument("--attributes", type=Path)
    p = selection.add_parser("observe")
    p.add_argument("--file", type=Path, required=True, help="Observation JSON file, or - for stdin")
    selection.add_parser("status")
    p = selection.add_parser("delete")
    p.add_argument("group")
    p = sub.add_parser("doctor", help="Check platform, versions, isolation prerequisites and budget file")
    p.add_argument("--sandbox", action="store_true", help="Actually execute the rootless isolation smoke")
    p = sub.add_parser("smoke", help="Paid read-only live probe; never touches a real project")
    p.add_argument("--live", action="store_true", required=True, help="Explicit consent to a paid, read-only Pi/provider probe")
    p.add_argument("--profile", help="Model profile for the probe")
    p.add_argument("--role", default=None, help="Read-only probe role (default: consult when present, else single role)")
    p = sub.add_parser("insight", help="Submit, list, read, decide or ingest proposals")
    p.add_argument("action", choices=("submit", "list", "read", "decide", "ingest", "revise", "history", "withdraw"), help="Proposal operation")
    p.add_argument("project", help="Managed project name")
    p.add_argument("--id", help="Proposal ID for read/submit/decide/withdraw")
    p.add_argument("--title", help="Proposal title for submit")
    p.add_argument("--body", type=Path, help="Read Markdown body from this file; '-' reads stdin")
    p.add_argument("--decision", help="Decision action for decide (accept/modify/defer/reject)")
    p.add_argument("--reason", default="", help="Decision reason for decide, withdrawal reason for withdraw")
    p.add_argument("--revisit", default="", help="Revisit condition for defer")
    p.add_argument("--expected-rev", type=int, default=None, help="Compare-and-swap revision for revise")
    p = sub.add_parser("editor", help="Export a capsule or serve the Editor MCP")
    esub = p.add_subparsers(dest="editor_command", required=True)
    p = esub.add_parser("export", help="Export an immutable Editor capsule")
    p.add_argument("project", help="Managed project name")
    p.add_argument("destination", type=Path, help="New bundle directory")
    p = esub.add_parser("mcp", help="Serve the capsule MCP over stdio")
    p.add_argument("--bundle", required=True, type=Path, help="Exported bundle directory")
    p.add_argument("--outbox", required=True, type=Path, help="Separate outbox directory")
    p = sub.add_parser("service", help="Render service definitions for this platform (systemd/launchd/Task Scheduler); nothing is started")
    p.add_argument("project", help="Managed project name")
    p.add_argument("--directory", type=Path, help="Unit output directory")
    p.add_argument("--executable", type=Path, default=Path(sys.argv[0]).resolve(), help="mizu executable")
    p = sub.add_parser("report", help="Materialize the latest recorded snapshot as a static artifact; no LLM required")
    p.add_argument("project", help="Managed project name")
    p = sub.add_parser("dashboard", help="Publish a bounded static dashboard payload from already-recorded facts; no LLM required")
    p.add_argument("project", help="Managed project name")
    p = sub.add_parser("usage", help="Summarize provider-reported token facts from completed runs; no LLM required")
    p.add_argument("project", help="Managed project name")
    p = sub.add_parser("backup", help="Write a paused checkpoint archive outside the tree")
    p.add_argument("project", help="Managed project name")
    p.add_argument("destination", type=Path, help="New archive file")
    p.add_argument("--verify", action="store_true", help="Re-list archive members after writing")
    p = sub.add_parser("restore", help="Restore a private checkpoint into a new, unarmed project")
    p.add_argument("project", help="New project name")
    p.add_argument("--archive", type=Path, required=True, help="Backup archive")
    p.add_argument("--max-bytes", type=int, default=1073741824, help="Restore byte limit")
    p = sub.add_parser("storage", help="Report snapshot/object retention accounting; dry-run preview unless --apply")
    p.add_argument("project", help="Managed project name")
    p.add_argument("--apply", action="store_true", help="Remove unreferenced manifests and orphan objects under quiescence")
    p = sub.add_parser("prune", help="List (or apply) removal of reproducible inputs, old dashboard generations, and expired web cache")
    p.add_argument("project", help="Managed project name")
    p.add_argument("--apply", action="store_true", help="Actually remove candidates")
    from .storage import DEFAULT_KEEP_ARTIFACTS
    p.add_argument("--keep-artifacts", type=int, default=DEFAULT_KEEP_ARTIFACTS,
                   help="Keep this many recent published artifacts")
    p = sub.add_parser("budget", help="Show per-project and shared day request budget usage")
    p.add_argument("project", help="Managed project name")
    return cli


def configure(file: Path, pi_command: str | None) -> dict:
    file = file.expanduser().resolve()
    if file.exists():
        return {"config": str(file), "status": "unchanged", "note": "Existing settings and policies are never overwritten"}
    mkdir(file.parent)
    text = (ROOT / "config/config.example.toml").read_text(encoding="utf-8")
    if pi_command:
        argv = json.loads(pi_command)
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v for v in argv):
            raise ConfigError("--pi-command-json must be a nonempty string array")
        text = text.replace('command = ["node"]', "command = " + json.dumps(argv))
    for policy in sorted((ROOT / "policies").glob("*.md")):
        target = file.parent / "policies" / policy.name
        if not target.exists():
            atomic_write(target, policy.read_bytes(), exclusive=True)
    atomic_write(file, text.encode(), exclusive=True)
    credentials = file.parent / "credentials.env"
    if not credentials.exists():
        atomic_write(credentials, b"# Literal KEY=value entries; no shell expansion. Mode 0600. Never commit this file.\n", exclusive=True)
    return {"config": str(file), "status": "created", "armed": False, "daily_requests": 0}


def _resolve_role(config, preferred: str | None, *, probe: bool = False) -> str:
    """Resolve an omitted --role without hardcoding role names.

    Single-role configurations default to that role; otherwise prefer
    `consult` (probe) or `worker` (run/daemon) when present; else require
    explicit --role. Role names stay configuration, not a class hierarchy.
    """
    if preferred:
        return preferred
    if len(config.roles) == 1:
        return next(iter(config.roles))
    fallback = "consult" if probe else "worker"
    if fallback in config.roles:
        return fallback
    raise ConfigError("Specify --role explicitly: no single or fallback role is configured")


def _cmd_status(config, project, args):
    return project.status()


def _cmd_arm(config, project, args):
    if config.limits.daily_requests <= 0:
        raise ConfigError("Set a positive daily_requests limit before arming")
    from .selection import role_profiles
    for name in project.roles:
        for profile in role_profiles(config, config.roles[name]):
            config.model(profile)
    return project.set_control(armed=True, paused=False, reason="Operator armed", wake_generation=uuid.uuid4().hex)


def _cmd_disarm(config, project, args):
    return project.set_control(armed=False, paused=True, reason="Operator disarmed")


def _cmd_pause(config, project, args):
    return project.set_control(paused=True, reason="Operator paused")


def _cmd_resume(config, project, args):
    if not project.control().get("armed"):
        raise Denied("Project is unarmed; use arm after reviewing its configuration")
    project.reset_health()
    return project.set_control(paused=False, reason="Operator resumed", wake_generation=uuid.uuid4().hex)


def _cmd_wake(config, project, args):
    return project.set_control(wake_generation=uuid.uuid4().hex, reason="Operator requested another work unit")


def _cmd_run(config, project, args):
    if _platform.is_root():
        raise Denied("Never run agents as root")
    from .runtime import Engine
    role = _resolve_role(config, args.role)
    stop = threading.Event()
    watched = [sig for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)) if sig is not None]
    previous = {sig: signal.signal(sig, lambda s, f: stop.set()) for sig in watched}
    try:
        return Engine(config, stop=stop).run(project, role, attributes=_attribute_input(getattr(args, "attributes", None)))
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _cmd_daemon(config, project, args):
    if _platform.is_root():
        raise Denied("Never run agents as root")
    from .runtime import daemon
    daemon(config, project.name, _resolve_role(config, args.role))
    return None


def _cmd_cleanup(config, project, args):
    from .sandbox import cleanup
    return cleanup(config, project.root, args.role)


def _cmd_insight(config, project, args):
    if args.action == "list":
        return project.insights.list(pending=False, limit=1000)
    if args.action == "read":
        if not args.id:
            raise ConfigError("--id is required")
        return project.insights.read(args.id)
    if args.action == "history":
        if not args.id:
            raise ConfigError("--id is required")
        return project.insights.history(args.id)
    if args.action == "revise":
        if not args.id:
            raise ConfigError("--id is required")
        if not args.title or args.body is None:
            raise ConfigError("Insight revision requires --title and --body")
        if str(args.body) == "-":
            import sys as _sys
            from .fs import PREVIEW_BYTES as _PREVIEW
            body = _sys.stdin.read(_PREVIEW + 1)
        else:
            if args.body.stat().st_size > PREVIEW_BYTES:
                raise Denied("Insight body file exceeds byte limit")
            body = args.body.read_text(encoding="utf-8")
        return project.insights.revise(args.id, source="operator", title=args.title, body=body,
                                       base_snapshot=project.snapshots.get()["id"],
                                       expected_rev=args.expected_rev)
    if args.action == "decide":
        if not args.id or not args.decision:
            raise ConfigError("Insight decide requires --id and --decision")
        return project.insights.decide(args.id, args.decision, args.reason or "", args.revisit or "", "operator")
    if args.action == "withdraw":
        if not args.id or not args.reason.strip():
            raise ConfigError("Insight withdraw requires --id and --reason")
        return project.insights.withdraw(args.id, source="operator", reason=args.reason,
                                         expected_rev=args.expected_rev)
    if args.action == "ingest":
        return project.insights.ingest_editor()
    if not args.title or args.body is None:
        raise ConfigError("Insight submission requires --title and --body")
    if str(args.body) == "-":
        body = sys.stdin.read(PREVIEW_BYTES + 1)
    else:
        if args.body.stat().st_size > PREVIEW_BYTES:
            raise Denied("Insight body file exceeds byte limit")
        body = args.body.read_text(encoding="utf-8")
    return project.insights.submit(source="operator", title=args.title, body=body,
                                   base_snapshot=project.snapshots.get()["id"], insight_id=args.id)


def _cmd_editor_export(config, project, args):
    from .editor import export
    return export(project, args.destination)


def _cmd_service(config, project, args):
    from .services import install
    return install(config, project, args.executable, args.directory)


def _cmd_report(config, project, args):
    from .report import publish
    return publish(project, project.snapshots.get(), None, run_id=None)


def _cmd_dashboard(config, project, args):
    from .dashboard import publish
    return publish(project)


def _cmd_usage(config, project, args):
    from .usage import summarize
    return summarize(project)


def _cmd_backup(config, project, args):
    from .storage import backup
    return backup(project, args.destination, verify=args.verify)


def _cmd_prune(config, project, args):
    from .storage import prune
    return prune(project, apply=args.apply, keep_artifacts=args.keep_artifacts)


def _cmd_storage(config, project, args):
    from .storage import audit, reclaim
    if getattr(args, "apply", False):
        return reclaim(project, apply=True)
    return audit(project)


_PROJECT_COMMANDS = {
    "status": _cmd_status, "arm": _cmd_arm, "disarm": _cmd_disarm,
    "pause": _cmd_pause, "resume": _cmd_resume, "wake": _cmd_wake,
    "run": _cmd_run, "daemon": _cmd_daemon, "cleanup": _cmd_cleanup,
    "insight": _cmd_insight, "service": _cmd_service, "report": _cmd_report,
    "dashboard": _cmd_dashboard, "usage": _cmd_usage, "backup": _cmd_backup,
    "prune": _cmd_prune, "storage": _cmd_storage,
}


def _selection_json(path):
    from .selection import MAX_BYTES, bounded
    try:
        if str(path) == "-":
            text = sys.stdin.read(MAX_BYTES + 1)
        else:
            if path.stat().st_size > MAX_BYTES:
                raise ConfigError("Selection input exceeds 1 MiB")
            with path.open(encoding="utf-8") as stream:
                text = stream.read(MAX_BYTES + 1)
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise ConfigError("Selection input exceeds 1 MiB")
        return bounded(json.loads(text))
    except (OSError, ValueError, RecursionError) as exc:
        raise ConfigError("Cannot read selection JSON") from exc


def _attribute_input(path):
    from .selection import attributes
    return attributes(_selection_json(path)) if path else {}


def execute(args):
    if args.command == "configure":
        return configure(args.config, args.pi_command_json)
    if args.command == "editor" and args.editor_command == "mcp":
        from .editor import serve
        serve(args.bundle, args.outbox)
        return None
    config = load(args.config)
    if args.command == "selection":
        from .selection import State
        state = State(config)
        if args.selection_command == "observe":
            return state.observe(_selection_json(args.file))
        if args.selection_command == "status":
            return state.read()
        if args.selection_command == "delete":
            return state.delete(args.group)
        from .project import Project
        from .runtime import Engine
        return Engine(config).preview(Project(config, args.project), _resolve_role(config, args.role), _attribute_input(args.attributes))
    if args.command == "doctor":
        from .doctor import check
        return check(config, sandbox=args.sandbox)
    if args.command == "smoke":
        from .smoke import live
        return live(config, args.profile, _resolve_role(config, args.role, probe=True))
    from .project import Project, initialize
    if args.command == "init":
        roles = args.roles.split(",") if args.roles else _resolve_role(config, None).split(",")
        # _resolve_role returns one role name; init accepts a comma list.
        # Single-role configs resolve to that role, otherwise worker fallback.
        project = initialize(config, args.project, args.source, args.goal, roles,
                             args.verify, armed=args.armed)
        return project.status()
    if args.command == "restore":
        from .storage import restore
        return restore(config, args.project, args.archive, max_bytes=args.max_bytes)
    if args.command == "budget":
        # Per-project usage plus the shared day total; the name is
        # validated but the project need not exist (counts are keyed by name).
        from .budget import Budget
        from .fs import identifier
        name = identifier(args.project)
        usage = Budget(config.data / "budget", config.limits.daily_requests,
                       config.limits.retention_days,
                       config.limits.shared_daily_requests, config.timezone).usage(name)
        return {"project": name, **usage}
    if args.command == "editor":
        # `editor export` needs a project; `editor mcp` handled above.
        project = Project(config, args.project)
        return _cmd_editor_export(config, project, args)
    project = Project(config, args.project)
    handler = _PROJECT_COMMANDS.get(args.command)
    if handler is None:
        raise ConfigError("Unhandled command")
    return handler(config, project, args)


def main(argv=None) -> int:
    try:
        _platform.set_umask()
        args = parser().parse_args(argv)
        result = execute(args)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("ok") is False else 0
    except KeyboardInterrupt:
        return 130
    except (MizuError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return getattr(exc, "code", 1)
