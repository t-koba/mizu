#!/usr/bin/env python3
"""Operator-run host gate that selects queue-backed decision authority.

This is example userland, not a runtime tool. It applies the idempotent
one-time conversion, requires the read-only stage-2 verify gate to pass, and
only then publishes the durable ``queues`` decision-authority marker through
the host-only production API. No legacy file receipt is written and no shared
authority is activated. An exact retry returns the retained marker.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

import convert_legacy  # noqa: E402  (example userland module)
import stage2_verify  # noqa: E402  (example userland module)
from mizu.config import load  # noqa: E402
from mizu.decision_reconciliation import activate_queue_decision_delivery  # noqa: E402
from mizu.errors import MizuError  # noqa: E402
from mizu.project import Project  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", help="Deployment configuration declaring the stage-2 queues")
    parser.add_argument("--project", required=True, help="Managed project name")
    args = parser.parse_args(argv)
    try:
        config = load(Path(args.config))
        for queue in convert_legacy.QUEUES:
            if queue not in config.queues:
                raise convert_legacy.ConversionError(
                    f"Configuration does not declare the {queue} queue")
        project = Project(config, args.project)
        conversion = convert_legacy.apply(project)
        verification = stage2_verify.verify(project)
        marker = activate_queue_decision_delivery(
            project, conversion=conversion, verification=verification)
    except (MizuError, convert_legacy.ConversionError, stage2_verify.VerifyError,
            OSError, ValueError, TypeError) as exc:
        print(f"queue activation refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"authority": marker["authority"], "conversion": conversion,
                      "verification": verification}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
