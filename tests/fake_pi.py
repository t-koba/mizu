"""Test-only RPC peer. This is not Pi, an LLM, or a production fallback."""
import json
import os
import socket
import sys
import time
from pathlib import Path


def flag(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def emit(data):
    sys.stdout.buffer.write(json.dumps(data, ensure_ascii=False).encode() + b"\n")
    sys.stdout.buffer.flush()


config = json.load(open(os.environ["MIZU_BRIDGE_CONFIG"]))
effective = json.load(open(sys.argv[-1]))
scenario = flag("--fake-scenario", "normal")


def bridge(operation, args):
    # The harness serves a Unix socket where the platform supports one and
    # loopback TCP elsewhere; fake_pi dials whichever bridge.json records.
    transport = config.get("transport", "unix")
    if transport == "tcp":
        connection = socket.create_connection((config["host"], config["port"]))
    else:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(config["socket"])
    with connection:
        connection.sendall(json.dumps({"token": config["token"], "operation": operation, "arguments": args}).encode() + b"\n")
        stream = connection.makefile("rb")
        result = json.loads(stream.readline())
        if not result["ok"]:
            raise RuntimeError(result["error"])
        return result["result"]


if scenario == "exit":
    raise SystemExit(7)
if scenario == "bad-json":
    print("not JSON", flush=True)
    raise SystemExit(0)
command_path = os.environ.get("MIZU_FAKE_PI_LIVE_COMMANDS")
bridge("_hello", {})
for raw in sys.stdin.buffer:
    command = json.loads(raw)
    if command_path:
        with Path(command_path).open("a", encoding="utf-8") as stream:
            stream.write(command["type"] + "\n")
    if command["type"] == "get_state":
        if scenario == "silent-state":
            # The child never answers its first state request.
            continue
        if scenario == "refused-state":
            emit({"type": "response", "id": command["id"], "success": False,
                  "error": "synthetic refusal"})
            continue
        emit({"type": "response", "id": command["id"], "success": True,
              "data": {"sessionFile": str(Path(effective["sessionDir"]) / "fake.jsonl"), "model": {"provider": effective["provider"], "id": "wrong" if scenario == "wrong-model" else effective["model"]}}})
    elif command["type"] == "prompt":
        if scenario == "live-steer":
            prompt_path = os.environ.get("MIZU_FAKE_PI_LIVE_PROMPT")
            if prompt_path:
                Path(prompt_path).write_text(command["message"], encoding="utf-8")
        emit({"type": "response", "id": command["id"], "success": True,
              "data": {"disposition": "handled" if scenario == "handled" else "started"}})
        if scenario == "handled":
            continue
        if scenario == "live-steer":
            # Capture the real opening prompt while its check is still live,
            # then wait for the test to finish that check before the next SDK
            # boundary. This is a fake-RPC timing seam, not SDK evidence.
            bridge("_budget", {"sequence": 1})
            bridge("_model_usage", {"sequence": 2, "usage": {"input": 1, "output": 1}})
            emit({"type": "agent_start"})
            continue_path = os.environ.get("MIZU_FAKE_PI_LIVE_CONTINUE")
            deadline = time.monotonic() + 10
            while not (continue_path and Path(continue_path).exists()) and time.monotonic() < deadline:
                time.sleep(0.01)
            if not continue_path or not Path(continue_path).exists():
                emit({"type": "agent_settled", "aborted": True})
                continue
            emit({"type": "tool_execution_end"})
            continue
        bridge("_budget", {"sequence": 1})
        if scenario == "provider-error":
            emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "synthetic failure 429 Retry-After 60"}})
            continue
        bridge("_model_usage", {"sequence": 2, "usage": {"input": 1, "output": 1}})
        emit({"type": "message_end", "message": {"role": "assistant", "content": "Unicode\u2028line\u2029end", "usage": {"input": 1, "output": 1}}})
        emit({"type": "agent_end"})  # A premature completion interpretation must fail the test.
        should_finish = scenario != "missing-finish" and not (scenario == "finish-on-resume" and not effective.get("resume"))
        if should_finish:
            if scenario != "finish-on-resume":
                bridge("read", {"path": "app.py"})
            bridge("finish", {"outcome": "wait", "summary": "After agent_end: \u2028 verified transport"})
        bridge("_request_done", {"sequence": 1})
        if scenario == "aborted-settled":
            emit({"type": "agent_settled", "aborted": True})
        elif scenario == "aborted-nested":
            emit({"type": "agent_settled", "data": {"aborted": True}})
        else:
            emit({"type": "agent_settled"})
    elif command["type"] == "steer" and scenario == "live-steer":
        # Record the host injection at the same turn boundary the SDK would
        # deliver it, then settle without a finish tool call (driver-level test).
        with (Path(effective["sessionDir"]).parent / "steers.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(command["message"] + "\n")
        if scenario == "live-steer":
            content = "LIVE " + command["message"]
            turn_path = os.environ.get("MIZU_FAKE_PI_LIVE_TURN")
            if turn_path:
                with Path(turn_path).open("a", encoding="utf-8") as stream:
                    stream.write(content + "\n")
            emit({"type": "message_end", "message": {
                "role": "assistant", "content": content,
                "usage": {"input": 1, "output": 1}}})
        bridge("_request_done", {"sequence": 1})
        if scenario == "live-steer":
            emit({"type": "agent_settled"})
            continue
        emit({"type": "message_end", "message": {"role": "assistant", "content": "ok", "usage": {"input": 1, "output": 1}}})
        emit({"type": "agent_settled"})
