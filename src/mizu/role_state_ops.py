"""Role research-state tool operations."""
from __future__ import annotations

import json

from .errors import Denied
from .fs import write_json


def research_read(context, args: dict) -> dict:
    # Own role's current research record with its generation: the
    # read half of the replace cycle. No evidence file: read-only,
    # and the run already records the call. Other roles' records
    # are never addressable; the role name is the caller's own.
    seen = context.project.role_state.read(context.role.name)
    return {"role": context.role.name, **seen}


def research(context, args: dict) -> dict:
    # Replacement-only update of the own role's record: the state
    # arrives as a JSON string (protocol schemas cannot express a
    # free-form bounded object), parses here, then replaces under
    # the generation the caller read. Stale, invalid, or
    # unavailable states are Denied with the stored record
    # untouched; the receipt carries the new generation plus the
    # stored state as run-record audit.
    try:
        state = json.loads(args["state"], parse_constant=_reject_constant)
    except ValueError as exc:
        raise Denied("Research state must parse as JSON") from exc
    out = context.project.role_state.replace(
        context.role.name, state,
        expected_generation=args["expected_generation"],
        run=context.run_dir.name)
    record = {"role": context.role.name, "generation": out["generation"],
              "updated_at": out["updated_at"], "state": state,
              "evidence": "research-state.json"}
    write_json(context.run_dir / "research-state.json", record)
    return record


def _reject_constant(value: str):
    """Reject non-finite JSON constants at parse time (matches ``fs``)."""
    raise ValueError(f"Non-finite JSON value: {value}")
