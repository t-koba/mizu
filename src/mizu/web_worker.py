"""Keep DNS, HTTP parsing and search plugins behind a killable time boundary."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .errors import Denied
from .fs import PREVIEW_BYTES, canonical
from .process import environment, run
from .web import Web


def bounded(config, run_dir, operation, value, cancel):
    root = Path(__file__).resolve().parents[1]
    request = {"settings": config.web, "cache": str(config.data / "web-cache"),
               "receipts": str(run_dir / "sources"), "operation": operation, "value": value}
    # Search may fetch multiple feeds, but remains bounded by the parent run deadline.
    timeout = config.web["timeout_seconds"] * (min(len(config.web["feeds"]), 30) + 1) if operation == "search" else config.web["timeout_seconds"] + 2
    result = run([sys.executable, "-m", "mizu.web_worker"], timeout=min(timeout, config.limits.run_seconds),
                 maximum=2 * 1024 * 1024, cancel=cancel, input_data=canonical(request),
                 env=environment(extra={"PYTHONPATH": str(root)}))
    if result.exit_code != 0 or result.reason != "exited":
        raise Denied(f"Web operation failed ({result.reason}): {result.stderr[-2000:]}")
    return json.loads(result.stdout)


def main():
    try:
        data = json.loads(sys.stdin.buffer.readline(PREVIEW_BYTES + 1))
        web = Web(data["settings"], Path(data["cache"]), Path(data["receipts"]))
        result = web.fetch(data["value"]) if data["operation"] == "fetch" else web.search(data["value"])
        sys.stdout.buffer.write(canonical(result))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
