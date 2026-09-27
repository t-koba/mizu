"""Test-only RPC peer. This is not Pi, an LLM, or a production fallback."""
import json
import os
import socket
import sys


def flag(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def emit(data):
    sys.stdout.buffer.write(json.dumps(data, ensure_ascii=False).encode() + b"\n")
    sys.stdout.buffer.flush()


config = json.load(open(os.environ["MIZU_BRIDGE_CONFIG"]))
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
bridge("_hello", {"protocol": 1})
for raw in sys.stdin.buffer:
    command = json.loads(raw)
    if command["type"] == "get_state":
        emit({"type": "response", "id": command["id"], "success": True,
              "data": {"model": {"provider": flag("--provider"), "id": "wrong" if scenario == "wrong-model" else flag("--model")}}})
    elif command["type"] == "prompt":
        emit({"type": "response", "id": command["id"], "success": True,
              "data": {"disposition": "handled" if scenario == "handled" else "started"}})
        if scenario == "handled":
            continue
        bridge("_budget", {"sequence": 1})
        emit({"type": "message_end", "message": {"role": "assistant", "content": "Unicode\u2028line\u2029end", "usage": {"input": 1, "output": 1}}})
        emit({"type": "agent_end"})  # A premature completion interpretation must fail the test.
        if scenario != "missing-finish":
            bridge("read", {"path": "app.py"})
            bridge("finish", {"outcome": "wait", "summary": "After agent_end: \u2028 verified transport"})
        emit({"type": "agent_settled"})
