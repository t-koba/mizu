"""Keep DNS, HTTP parsing and search plugins behind a killable time boundary."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import MAX_INCLUDE_BYTES
from .errors import Denied
from .fs import canonical
from .process import environment, run
from .web import (PROBE_RESPONSE_HEADERS_MAX, PROBE_RESPONSE_NAME_MAX,
                  PROBE_RESPONSE_VALUE_MAX, Web)

# TOML strings with literal tabs/newlines can double when canonicalized; reserve
# two config budgets plus one budget for the schema-bounded escaped probe frame.
MAX_WEB_WORKER_INPUT_BYTES = 3 * MAX_INCLUDE_BYTES


def _output_limit(operation: str, max_bytes: int) -> int:
    """Finite combined stdout/stderr ceiling, including serialized receipts."""
    floor = 2 * 1024 * 1024
    if operation not in ("fetch", "probe"):
        # Search keeps its existing finite aggregate cap, not a body envelope.
        return floor
    metadata_reserve = 64 * 1024
    if operation == "probe":
        metadata_reserve += 6 * PROBE_RESPONSE_HEADERS_MAX * (
            PROBE_RESPONSE_NAME_MAX + PROBE_RESPONSE_VALUE_MAX)
    # Six bytes per raw body byte covers JSON control escaping and UTF-8
    # replacement decoding. Generated receipt metadata has fixed bounds.
    return max(floor, 6 * max_bytes + metadata_reserve)


def bounded(config, run_dir, operation, value, cancel, role: str = ""):
    root = Path(__file__).resolve().parents[1]
    request = {"settings": config.web, "cache": str(config.data / "web-cache"),
               "receipts": str(run_dir / "sources"), "operation": operation, "value": value,
               "role": role}
    # Search may fetch multiple feeds, but remains bounded by the parent run deadline.
    input_data = canonical(request)
    if len(input_data) > MAX_WEB_WORKER_INPUT_BYTES:
        raise Denied("Web worker request exceeds input byte limit")
    timeout = config.web["timeout_seconds"] * (len(config.web["feeds"]) + 1) if operation == "search" else config.web["timeout_seconds"] + 2
    result = run([sys.executable, "-m", "mizu.web_worker"], timeout=min(timeout, config.limits.run_seconds),
                 maximum=_output_limit(operation, config.web["max_bytes"]),
                 cancel=cancel, input_data=input_data,
                 env=environment(extra={"PYTHONPATH": str(root)}))
    if result.exit_code != 0 or result.reason != "exited":
        raise Denied(f"Web operation failed ({result.reason}): {result.stderr[-2000:]}")
    return json.loads(result.stdout)


def main():
    try:
        input_data = sys.stdin.buffer.read(MAX_WEB_WORKER_INPUT_BYTES + 1)
        if len(input_data) > MAX_WEB_WORKER_INPUT_BYTES:
            raise Denied("Web worker input exceeds byte limit")
        data = json.loads(input_data)
        web = Web(data["settings"], Path(data["cache"]), Path(data["receipts"]),
                  role=data.get("role", ""))
        if data["operation"] == "fetch":
            result = web.fetch(data["value"])
        elif data["operation"] == "probe":
            result = web.probe(data["value"])
        else:
            result = web.search(data["value"])
        sys.stdout.buffer.write(canonical(result))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
