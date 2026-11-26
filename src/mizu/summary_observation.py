"""Trusted host gathering of raw CI aggregate evidence for an observation driver.

The host caller reads one coherent poll-cache generation and assembles only
evidence inputs: bounded retained failure triples, validated current check
summaries and the projection's incompleteness flag. A configured operator driver
owns the aggregation policy and projects those inputs into bounded prompt facts
through the generic host seam; this module never recomputes or repairs the
returned facts.

Schema: ``ci_summary_facts(project, config=None)`` returns the driver's list of
at most ``MAX_CI_WAKE_ROWS`` plain aggregate facts, each with exactly
``{subject, revision, outcome, non_passing_checks, may_be_incomplete}``, sorted
by ``(subject, revision)``. Bounds: one poll-cache read, at most
``MAX_CI_WAKE_ROWS`` failure triples and cleaned summaries sent as one request,
one driver call and one observation in reply. Trust: cache rows and driver
output are untrusted evidence; the kernel-owned queue records the observation
as external-untrusted. Retry/cancellation: one driver call, no replay. Evidence:
the driver observation is appended to its declared queue by the host seam.
Failure: an empty request, an undeclared/refused/failing driver, an invalid
observation or malformed facts returns ``[]``; no aggregate fact is invented in
the kernel.
"""
from __future__ import annotations

from . import ci_observation, host_drivers
from .ci_observation import MAX_CI_WAKE_ROWS, _valid_ci_summary_fact
from .context_vcs_read_tools import _RECORD_ENVELOPE_KEYS
from .fs import loads_finite, read_json


def summary_request(project, config=None) -> dict:
    """Assemble one bounded plain JSON request from one coherent cache read."""
    if config is None:
        config = getattr(project, "config", None)
    try:
        raw = read_json(project.root / ci_observation.POLL_STATE_FILE, {})
        rows, partial = ci_observation._ci_failures_projection(
            raw, limit=MAX_CI_WAKE_ROWS, branches=None, config=config)
        if not rows:
            return {}
        cleaned = ci_observation._clean_ci_check_summaries(
            raw.get("ci_check_summaries"), config)
        summaries = [[branch, entry["sha"], entry["non_passing_checks"],
                      entry["may_be_incomplete"]]
                     for branch in sorted(cleaned) for entry in cleaned[branch]]
        return {"rows": [[row["branch"], row["sha"], row["check"]] for row in rows],
                "summaries": summaries, "partial": partial}
    except Exception:
        return {}


def ci_summary_facts(project, config=None) -> list:
    """Return the driver's bounded aggregate facts, or ``[]`` when refused."""
    if config is None:
        config = getattr(project, "config", None)
    try:
        request = summary_request(project, config)
        if not request:
            return []
        response = host_drivers.observe(config, project, "ci_summary", request)
        observations = response.get("observations") if isinstance(response, dict) else None
        if (not isinstance(observations, list) or len(observations) != 1
                or type(observations[0]) is not dict
                or not isinstance(observations[0].get("detail"), str)):
            return []
        detail = loads_finite(observations[0]["detail"].encode("utf-8"))
        if (not isinstance(detail, dict) or set(detail) != {"facts"}
                or _RECORD_ENVELOPE_KEYS.intersection(detail)):
            return []
        facts = detail["facts"]
        if (not isinstance(facts, list) or len(facts) > MAX_CI_WAKE_ROWS
                or any(not _valid_ci_summary_fact(fact) for fact in facts)):
            return []
        subjects = [(fact["subject"], fact["revision"]) for fact in facts]
        if len(set(subjects)) != len(subjects) or subjects != sorted(subjects):
            return []
        return facts
    except Exception:
        return []
