"""A small deterministic project for first-run acceptance; no network or dependencies."""
from __future__ import annotations


def summarize(values: list[float]) -> dict[str, float | int]:
    """Return count, sum and arithmetic mean; define an empty mean as zero."""
    count = len(values)
    total = sum(values)
    return {'count': count, 'sum': total, 'mean': total / count if count else 0.0}
