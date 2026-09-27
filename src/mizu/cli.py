"""The CLI is the human control plane. JSON goes to stdout; errors to stderr.
One-shot commands print a single JSON document; daemon streams JSON Lines."""
from __future__ import annotations

import argparse
import json
import os
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
    initialize.add_argument("--roles", default="worker", help="Comma-separated configured roles")
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
        p.add_argument("--role", default=None if name == "cleanup" else "worker", help="Configured role")
    p = sub.add_parser("doctor", help="Check platform, versions, isolation prerequisites and budget file")
    p.add_argument("--sandbox", action="store_true", help="Actually execute the rootless isolation smoke")
    p = sub.add_parser("smoke", help="Paid read-only live probe; never touches a real project")
    p.add_argument("--live", action="store_true", required=True, help="Explicit consent to a paid, read-only Pi/provider probe")
    p.add_argument("--profile", help="Model profile for the probe")
    p.add_argument("--role", default="consult", help="Read-only probe role (default: consult)")
    p = sub.add_parser("insight", help="Submit, list, read or ingest proposals")
    p.add_argument("action", choices=("submit", "list", "read", "ingest"), help="Proposal operation")
    p.add_argument("project", help="Managed project name")
    p.add_argument("--id", help="Proposal ID for read/submit")
    p.add_argument("--title", help="Proposal title for submit")
    p.add_argument("--body", type=Path, help="Read Markdown body from this file; '-' reads stdin")
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
    p = sub.add_parser("prune", help="List (or apply) removal of reproducible inputs")
    p.add_argument("project", help="Managed project name")
    p.add_argument("--apply", action="store_true", help="Actually remove candidates")
    p.add_argument("--keep-artifacts", type=int, default=30,
                   help="Keep this many recent published artifacts")
    p = sub.add_parser("budget", help="Show shared UTC-day request budget usage")
    p.add_argument("project", help="Managed project name")
    return cli


def configure(file: Path, pi_command: str | None) -> dict:
    file = file.expanduser().resolve()
    if file.exists():
        return {"config": str(file), "status": "unchanged", "note": "Existing settings and policies are never overwritten"}
    mkdir(file.parent)
    text = (ROOT / "config/config.example.toml").read_text()
    if pi_command:
        argv = json.loads(pi_command)
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v for v in argv):
            raise ConfigError("--pi-command-json must be a nonempty string array")
        text = text.replace('pi_command = ["pi"]', "pi_command = " + json.dumps(argv))
    for policy in sorted((ROOT / "policies").glob("*.md")):
        target = file.parent / "policies" / policy.name
        if not target.exists():
            atomic_write(target, policy.read_bytes(), exclusive=True)
    atomic_write(file, text.encode(), exclusive=True)
    credentials = file.parent / "credentials.env"
    if not credentials.exists():
        atomic_write(credentials, b"# Literal KEY=value entries; no shell expansion. Mode 0600. Never commit this file.\n", exclusive=True)
    return {"config": str(file), "status": "created", "armed": False, "daily_requests": 0}


def execute(args):
    if args.command == "configure":
        return configure(args.config, args.pi_command_json)
    if args.command == "editor" and args.editor_command == "mcp":
        from .editor import serve
        serve(args.bundle, args.outbox)
        return None
    config = load(args.config)
    if args.command == "doctor":
        from .doctor import check
        return check(config, sandbox=args.sandbox)
    if args.command == "smoke":
        from .smoke import live
        return live(config, args.profile, args.role)
    from .project import Project, initialize
    if args.command == "init":
        project = initialize(config, args.project, args.source, args.goal, args.roles.split(","),
                             args.verify, armed=args.armed)
        return project.status()
    if args.command == "restore":
        from .storage import restore
        return restore(config, args.project, args.archive, max_bytes=args.max_bytes)
    if args.command == "budget":
        # Global shared budget: no project construction or validation needed.
        from .budget import Budget
        return Budget(config.data / "budget", config.limits.daily_requests).usage()
    project = Project(config, args.project)
    if args.command == "status":
        return project.status()
    if args.command == "arm":
        if config.limits.daily_requests <= 0:
            raise ConfigError("Set a positive daily_requests limit before arming")
        for name in project.roles:
            config.model(config.roles[name].profile)
        return project.set_control(armed=True, paused=False, reason="Operator armed", wake_generation=uuid.uuid4().hex)
    if args.command == "disarm":
        return project.set_control(armed=False, paused=True, reason="Operator disarmed")
    if args.command == "pause":
        return project.set_control(paused=True, reason="Operator paused")
    if args.command == "resume":
        if not project.control().get("armed"):
            raise Denied("Project is unarmed; use arm after reviewing its configuration")
        return project.set_control(paused=False, reason="Operator resumed", wake_generation=uuid.uuid4().hex)
    if args.command == "wake":
        return project.set_control(wake_generation=uuid.uuid4().hex, reason="Operator requested another work unit")
    if args.command in ("run", "daemon"):
        if _platform.is_root():
            raise Denied("Never run agents as root")
        from .runtime import Engine, daemon
        if args.command == "daemon":
            daemon(config, project.name, args.role)
            return None
        stop = threading.Event()
        watched = [sig for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)) if sig is not None]
        previous = {sig: signal.signal(sig, lambda s, f: stop.set()) for sig in watched}
        try:
            return Engine(config, stop=stop).run(project, args.role)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    if args.command == "cleanup":
        from .sandbox import cleanup
        return cleanup(config, project.root, args.role)
    if args.command == "insight":
        if args.action == "list":
            return project.insights.list(pending=False, limit=1000)
        if args.action == "read":
            if not args.id:
                raise ConfigError("--id is required")
            return project.insights.read(args.id)
        if args.action == "ingest":
            return project.insights.ingest_editor()
        if not args.title or args.body is None:
            raise ConfigError("Insight submission requires --title and --body")
        if str(args.body) == "-":
            body = sys.stdin.read(PREVIEW_BYTES + 1)
        else:
            if args.body.stat().st_size > PREVIEW_BYTES:
                raise Denied("Insight body file exceeds byte limit")
            body = args.body.read_text()
        return project.insights.submit(source="operator", title=args.title, body=body,
                                       base_snapshot=project.snapshots.get()["id"], insight_id=args.id)
    if args.command == "editor":
        from .editor import export
        return export(project, args.destination)
    if args.command == "service":
        from .services import install
        return install(config, project, args.executable, args.directory)
    if args.command == "report":
        from .report import publish
        return publish(project, project.snapshots.get(), None, run_id=None)
    if args.command == "dashboard":
        from .dashboard import publish
        return publish(project)
    if args.command == "usage":
        from .usage import summarize
        return summarize(project)
    if args.command == "backup":
        from .storage import backup
        return backup(project, args.destination, verify=args.verify)
    if args.command == "prune":
        from .storage import prune
        return prune(project, apply=args.apply, keep_artifacts=args.keep_artifacts)
    raise ConfigError("Unhandled command")


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
