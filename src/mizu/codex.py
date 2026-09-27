"""Codex CLI driver. Trusted operator argv; model authority stays in the bridge.

The CLI runs with its tightest non-interactive posture (`read-only` sandbox,
no approvals to give, no user/project config layers, no session persistence)
against an isolated `CODEX_HOME` written by this driver. The only tools the
model receives are the capability-filtered `mizu_*` MCP tools served by
`mcp_proxy.py`, which forwards to `Context.handle`; the `mizu` MCP server is
`required`, so a bridge failure fails the run instead of continuing blind.
Model-driven commands therefore still execute only in Mizu's networkless
Podman sandbox. Never use `danger-full-access`/`--yolo` here.

Authentication is operator-owned and out of band (`codex login` file store,
OS keyring, or an explicitly exported `CODEX_API_KEY` which is forwarded to
this one invocation only). Secrets are never written into run records or
mounted into command containers.

Counting: one invocation counts as one request against the shared daily
budget (see `drivers.admit_invocation`). Token usage comes from the `--json`
event stream and is recorded verbatim for presentation policy.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .bridge import Bridge
from .drivers import DIAGNOSTICS_TAIL_BYTES, EVENT_STREAM_BYTES, admit_invocation, settle_invocation, tool_names
from .errors import ConfigError
from .fs import atomic_write, mkdir
from .process import environment, run
from .protocol import tool_definitions

ROOT = Path(__file__).resolve().parents[2]
#: Raw `--json` event stream retained per run (evidence, not a bill).


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_config_toml(*, model: str, instructions: Path,
                      capabilities: tuple[str, ...], bridge_file: Path) -> str:
    """Render the isolated Codex config. Pure text for tests and evidence."""
    lines = [
        "# Generated per run by Mizu; do not edit. Auth lives outside this file.",
        f"model = {_toml_string(model)}",
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        'web_search = "disabled"',
        "project_doc_max_bytes = 0",
        f"model_instructions_file = {_toml_string(str(instructions))}",
        "",
        "[history]",
        'persistence = "none"',
        "",
        "[agents]",
        "enabled = false",
        "",
        "[features]",
        "shell_tool = false",
        "multi_agent = false",
        "",
        "[mcp_servers.mizu]",
        f"command = {_toml_string(sys.executable)}",
        "args = [" + _toml_string("-m") + ", " + _toml_string("mizu.mcp_proxy") + "]",
        "required = true",
        "startup_timeout_sec = 10",
        "tool_timeout_sec = 60",
        'default_tools_approval_mode = "approve"',
        "enabled_tools = [" + ", ".join(_toml_string(n) for n in tool_names(capabilities)) + "]",
        "",
        "[mcp_servers.mizu.env]",
        f"MIZU_BRIDGE_CONFIG = {_toml_string(str(bridge_file))}",
        f"PYTHONPATH = {_toml_string(str(ROOT / 'src'))}",
        "",
    ]
    return "\n".join(lines) + "\n"


def build_argv(command: tuple[str, ...]) -> list[str]:
    """Trusted argv only. No shell, no user config, no persisted sessions."""
    argv = [*command, "exec", "--json", "--sandbox", "read-only",
            "--ask-for-approval", "never", "--skip-git-repo-check", "--ephemeral", "-"]
    for banned in ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "danger-full-access"):
        if banned in argv:
            raise ConfigError("Refusing unsafe Codex invocation")
    return argv


def parse_events(stdout: str) -> dict:
    """Split a `--json` stream into conversation, usage, errors. Tolerant reader."""
    conversation, usages, errors, bad = None, [], [], 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            bad += 1
            continue
        if not isinstance(event, dict):
            bad += 1
            continue
        kind = event.get("type")
        if kind == "thread.started" and conversation is None:
            conversation = event.get("thread_id")
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usages.append(event["usage"])
        elif kind == "error":
            errors.append(str(event.get("message") or event.get("error") or kind))
    return {"conversation_id": conversation, "usage": usages, "errors": errors, "bad_lines": bad}


class CodexDriver:
    #: See PiDriver.requires_sandbox.
    requires_sandbox = True

    def __init__(self, config, runner=None):
        self.config = config
        self.runner = runner or run

    def prepare(self, role, run_dir: Path, profile: str, bridge_file: Path) -> dict:
        """Write the isolated home, instructions and argv. Returns paths and argv."""
        model = self.config.model(profile)
        home = run_dir / "codex-home"
        mkdir(home)
        instructions = run_dir / "instructions.md"
        policy_text = role.policy.read_text()
        atomic_write(instructions, policy_text.encode())
        text = build_config_toml(model=model["model"], instructions=instructions,
                                 capabilities=role.capabilities,
                                 bridge_file=bridge_file)
        atomic_write(home / "config.toml", text.encode())
        link = home / "auth.json"
        source = Path.home() / ".codex" / "auth.json"
        if source.is_file() and not link.exists():
            try:
                os.symlink(source, link)
            except OSError as exc:
                raise ConfigError(f"Cannot link Codex credentials: {exc}") from exc
        return {"home": home, "instructions": instructions,
                "argv": build_argv(tuple(self.config.codex_command))}

    def execute(self, context, prompt: str, *, profile: str | None = None) -> dict:
        role, run_dir = context.role, context.run_dir
        profile = profile or role.profile
        model = self.config.model(profile)
        admit_invocation(context)
        started = time.monotonic()
        with Bridge(context.handle, tool_definitions(role.capabilities),
                     timeout=self.config.limits.run_seconds) as bridge:
            prepared = self.prepare(role, run_dir, profile, bridge.config_file)
            cwd = run_dir / "controller"
            mkdir(cwd)
            env = environment(extra={"CODEX_HOME": str(prepared["home"])})
            if os.environ.get("CODEX_API_KEY"):
                env["CODEX_API_KEY"] = os.environ["CODEX_API_KEY"]
            remaining = max(1.0, context.deadline - time.monotonic())
            result = self.runner(prepared["argv"], timeout=remaining,
                                 maximum=EVENT_STREAM_BYTES, cwd=cwd, env=env,
                                 cancel=context.cancelled,
                                 input_data=prompt.encode("utf-8"))
        atomic_write(run_dir / "codex-events.jsonl", result.stdout.encode("utf-8", "replace"))
        atomic_write(run_dir / "diagnostics.txt", result.stderr.encode("utf-8", "replace")[-DIAGNOSTICS_TAIL_BYTES:])
        parsed = parse_events(result.stdout)
        settle_invocation(context, result, parsed, engine="Codex")
        return {"profile": profile, "provider": model["provider"], "model": model["model"],
                "engine": "codex", "requests": context.request_count,
                "usage": parsed["usage"], "conversation_id": parsed["conversation_id"],
                "seconds": round(time.monotonic() - started, 3)}
