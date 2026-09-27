"""Claude CLI driver. Trusted operator argv; model authority stays in the bridge.

Headless print mode (`-p`) with `stream-json` output against a per-run MCP
config that exposes only the capability-filtered `mizu_*` tools through
`mcp_proxy.py`. `--allowedTools` names those tools explicitly; the dangerous
skip-permissions flag is never passed. The role policy travels as the full
`--system-prompt`. Runs are one-shot and stateless: continuity comes from
Mizu snapshots and prompts, never from CLI sessions.

Authentication is operator-owned and out of band (`claude login`). Secrets
are never written into run records or mounted into command containers.

Counting mirrors the Codex driver: one invocation counts as one request
against the shared daily budget (see `drivers.admit_invocation`); token
usage comes from the `stream-json` events verbatim.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .bridge import Bridge
from .drivers import DIAGNOSTICS_TAIL_BYTES, EVENT_STREAM_BYTES, admit_invocation, settle_invocation, tool_names
from .errors import ConfigError
from .fs import atomic_write, mkdir, write_json
from .process import environment, run
from .protocol import tool_definitions

ROOT = Path(__file__).resolve().parents[2]
#: Raw `stream-json` event stream retained per run (evidence, not a bill).


def allowed_tools(capabilities: tuple[str, ...]) -> list[str]:
    """Explicit MCP tool allowlist for a capability set. No wildcards, no host tools.

    Claude Code addresses MCP tools as `mcp__<server>__<tool>`; the server key
    is `mizu` and proxied tool names follow the `mizu_*` convention.
    """
    return ["mcp__mizu__" + name for name in tool_names(capabilities)]


def build_mcp_config(bridge_file: Path) -> str:
    """Render the per-run MCP config. Pure text for tests and evidence."""
    servers = {"mizu": {
        "command": sys.executable,
        "args": ["-m", "mizu.mcp_proxy"],
        "env": {"MIZU_BRIDGE_CONFIG": str(bridge_file),
                "PYTHONPATH": str(ROOT / "src")},
    }}
    return json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2) + "\n"


def build_argv(command: tuple[str, ...], *, mcp_config: Path, tools: list[str],
               system_prompt: str, prompt: str) -> list[str]:
    """Trusted argv only. `--system-prompt` carries the role policy verbatim."""
    argv = [*command, "-p", prompt, "--output-format", "stream-json",
            "--mcp-config", str(mcp_config),
            "--allowedTools", ",".join(tools),
            "--system-prompt", system_prompt]
    for banned in ("--dangerously-skip-permissions", "--yolo"):
        if banned in argv:
            raise ConfigError("Refusing unsafe Claude invocation")
    return argv


def parse_events(stdout: str) -> dict:
    """Split a `stream-json` stream into session, usage, errors. Tolerant reader."""
    session, usages, errors, bad = None, [], [], 0
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
        for key in ("session_id", "sessionId"):
            if session is None and isinstance(event.get(key), str):
                session = event[key]
        usage = event.get("usage")
        if isinstance(usage, dict) and any(isinstance(v, (int, float)) and not isinstance(v, bool)
                                           for v in usage.values()):
            usages.append(usage)
        if event.get("type") == "result" and event.get("is_error"):
            errors.append(str(event.get("error") or event.get("subtype") or "result error"))
        elif event.get("type") == "error":
            errors.append(str(event.get("error") or event.get("message") or "error"))
    return {"session_id": session, "usage": usages, "errors": errors, "bad_lines": bad}


class ClaudeDriver:
    #: See PiDriver.requires_sandbox.
    requires_sandbox = True

    def __init__(self, config, runner=None):
        self.config = config
        self.runner = runner or run

    def prepare(self, role, run_dir: Path, bridge_file: Path) -> dict:
        """Write the per-run MCP config and argv. Returns paths and argv."""
        mcp_config = run_dir / "claude-mcp.json"
        atomic_write(mcp_config, build_mcp_config(bridge_file).encode())
        tools = allowed_tools(role.capabilities)
        system_prompt = role.policy.read_text()
        if not system_prompt.strip():
            raise ConfigError("Role policy is empty; refusing an ungoverned Claude run")
        return {"mcp_config": mcp_config, "tools": tools, "system_prompt": system_prompt}

    def execute(self, context, prompt: str, *, profile: str | None = None) -> dict:
        role, run_dir = context.role, context.run_dir
        profile = profile or role.profile
        model = self.config.model(profile)
        admit_invocation(context)
        started = time.monotonic()
        with Bridge(context.handle, tool_definitions(role.capabilities),
                     timeout=self.config.limits.run_seconds) as bridge:
            prepared = self.prepare(role, run_dir, bridge.config_file)
            argv = build_argv(tuple(self.config.claude_command), mcp_config=prepared["mcp_config"],
                              tools=prepared["tools"], system_prompt=prepared["system_prompt"],
                              prompt=prompt)
            cwd = run_dir / "controller"
            mkdir(cwd)
            env = environment()
            remaining = max(1.0, context.deadline - time.monotonic())
            result = self.runner(argv, timeout=remaining,
                                 maximum=EVENT_STREAM_BYTES, cwd=cwd, env=env,
                                 cancel=context.cancelled)
        atomic_write(run_dir / "claude-events.jsonl", result.stdout.encode("utf-8", "replace"))
        atomic_write(run_dir / "diagnostics.txt", result.stderr.encode("utf-8", "replace")[-DIAGNOSTICS_TAIL_BYTES:])
        write_json(run_dir / "claude-argv.json",
                   {"allowed_tools": prepared["tools"],
                    "prompt_chars": len(prompt),
                    "system_prompt_chars": len(prepared["system_prompt"])})
        parsed = parse_events(result.stdout)
        settle_invocation(context, result, parsed, engine="Claude")
        return {"profile": profile, "provider": model["provider"], "model": model["model"],
                "engine": "claude", "requests": context.request_count,
                "usage": parsed["usage"], "session_id": parsed["session_id"],
                "seconds": round(time.monotonic() - started, 3)}
