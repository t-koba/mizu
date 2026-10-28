"""Writer work-order tool operation (one narrow write)."""
from __future__ import annotations

from . import work_order as _plan
from .errors import Denied
from .fs import canonical, digest, write_json


def record(context, args: dict) -> dict:
    if "work_order" not in context.role.capabilities:
        raise Denied("Work-order recording requires the work_order grant")
    opening = _plan._frozen_generation(context)
    if opening is None:
        raise Denied("Work-order review is unavailable; re-read before recording")
    # Protocol already bounds items/rationale/rev/generation shapes; the
    # store revalidates exact IDs, order-uniqueness and byte bounds.
    out = _plan.write_plan(
        context.project, actor=context.role.name, run=context.run_dir.name,
        items=args["items"], rationale=args["rationale"],
        expected_rev=args["expected_rev"],
        expected_generation=args["expected_generation"],
        opening_generation=opening)
    # Every acknowledged revision gets an immutable, bounded receipt. The
    # digest keeps the run-retained path fixed-length even as revisions grow.
    evidence = f"work-order-{digest(canonical(out))}.json"
    write_json(context.run_dir / evidence, out, exclusive=True)
    return {"rev": out["rev"], "generation": out["generation"],
            "evidence": evidence}
