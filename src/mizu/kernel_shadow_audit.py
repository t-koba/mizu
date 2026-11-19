"""Bounded read-only audit of the stage-2 kernel shadow projections.

The authoritative legacy insights, decisions and records stay the source of
truth; this module only compares them with the derived kernel objects through
the declared :mod:`mizu.kernel_shadow` verifiers. It never writes, repairs or
reprojects anything, and no legacy read consults a kernel object to decide its
result. The report is a finite JSON document: exact counts of the bounded sets
examined, up to ``MAX_SHADOW_FAILURES`` mismatch rows, and explicit truncation
flags. ``enabled`` is false when the project has no shadow, in which case no
store is scanned.

Schema: ``{enabled, counts: {insights, notices, role_questions, answers},
insights, notices, role_questions, answers, mismatches: [{record, kind,
result, mismatches}], truncated, answers_complete}``; the named counts repeat
``counts`` for direct readers. ``answers_complete`` is false when any answer
scan is exhausted or unreadable, with a bounded mismatch row, so a partial
scan is never full stage-2 proof. Bounds: live insight pages use
``insight_tools.INSIGHT_RETRIEVAL_LIMIT``; each record scan uses
``[limits].operator_record_catalog_scan_entries``; mismatch rows use
``MAX_SHADOW_FAILURES`` and overflow sets ``truncated``. Trust: only the
authoritative legacy files decide what the projections should contain; kernel
objects are read through the declared verifiers and never written. Retry: a
pure read, safe to repeat; per-record read or verification faults become
bounded mismatch rows and scan faults become one bounded scan row, so a
damaged store cannot masquerade as a clean projection.
"""
from __future__ import annotations

from . import host_records, insight_tools, operator_records
from .errors import Denied, LimitExceeded
from .kernel_shadow import MAX_SHADOW_FAILURES, published_answer
from .shared_records import ScanProgress

_READ_ERRORS = (Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError, AttributeError)


def _counts() -> dict:
    return {"insights": 0, "notices": 0, "role_questions": 0, "answers": 0}


def _report(enabled: bool) -> dict:
    counts = _counts()
    return {"enabled": enabled, "counts": counts, **counts,
            "mismatches": [], "truncated": False, "answers_complete": True}


def _note(report: dict, record: str, kind: str, result: dict | None = None,
          reason: str | None = None) -> None:
    """Retain one bounded mismatch row; overflow is reported as truncation."""
    if len(report["mismatches"]) >= MAX_SHADOW_FAILURES:
        report["truncated"] = True
        return
    messages = list(result["mismatches"]) if result is not None else [str(reason)]
    report["mismatches"].append({"record": str(record), "kind": kind,
                                 "result": kind, "mismatches": messages})


def _check(report: dict, record: str, kind: str, result: dict) -> None:
    if not result["ok"]:
        _note(report, record, kind, result)


def _insights(project, shadow, report: dict, insight_ids: set) -> None:
    """Enumerate bounded live insights and verify their declared projections."""
    cursor = None
    examined = 0
    limit = insight_tools.INSIGHT_RETRIEVAL_LIMIT
    while True:
        try:
            page = project.insights.list_page(scope="live", limit=limit, cursor=cursor)
        except _READ_ERRORS as exc:
            _note(report, "insights", "scan",
                  reason=f"insight page scan failed: {type(exc).__name__}")
            return
        for summary in page["items"]:
            if examined >= limit:
                report["truncated"] = True
                break
            examined += 1
            insight_id = summary["id"]
            insight_ids.add(insight_id)
            report["counts"]["insights"] += 1
            try:
                insight = project.insights.read(insight_id)
                decision = project.insights.decision(insight_id)
            except _READ_ERRORS as exc:
                _note(report, insight_id, "insight",
                      reason=f"insight unavailable: {type(exc).__name__}")
                continue
            try:
                _check(report, insight_id, "insight",
                       shadow.verify(insight, decision))
            except _READ_ERRORS as exc:
                _note(report, insight_id, "insight",
                      reason=f"insight verification failed: {type(exc).__name__}")
            try:
                answers, complete = operator_records.insight_answers(project, insight_id)
            except _READ_ERRORS as exc:
                report["answers_complete"] = False
                _note(report, insight_id, "answer",
                      reason=f"insight answers unavailable: {type(exc).__name__}")
                continue
            if not complete:
                report["answers_complete"] = False
                _note(report, insight_id, "answer",
                      reason="insight answer scan incomplete; only an available prefix was examined")
            if insight.get("addressee") == "operator":
                subject = {"type": "insight", "id": insight_id,
                           "rev": insight.get("rev", 1)}
                answered = published_answer(answers, subject, complete)
                if answered is None:
                    _note(report, insight_id, "question",
                          reason="insight answer scan incomplete; question answer state is unknown")
                try:
                    _check(report, insight_id, "question",
                           shadow.verify_question(insight, insight=insight,
                                                  answered=answered))
                except _READ_ERRORS as exc:
                    _note(report, insight_id, "question",
                          reason=f"question verification failed: {type(exc).__name__}")
            for record, _ in answers:
                report["counts"]["answers"] += 1
                try:
                    _check(report, record["id"], "answer",
                           shadow.verify_answer(record, insight))
                except _READ_ERRORS as exc:
                    _note(report, record["id"], "answer",
                          reason=f"answer verification failed: {type(exc).__name__}")
        if examined >= limit or page["next_cursor"] is None:
            if examined >= limit:
                report["truncated"] = True
            break
        cursor = page["next_cursor"]


def _operator_records(project, shadow, report: dict, insight_ids: set,
                      record_answers: list) -> bool:
    """Verify notices and collect role answers; return answer-scan completeness."""
    complete = True
    shared = operator_records.store(project)
    progress = ScanProgress()
    scan = shared.records(
        namespace="operator",
        scan_limit=project.config.limits.operator_record_catalog_scan_entries,
        scan_progress=progress)
    try:
        for envelope, computed in scan:
            kind = envelope.get("kind")
            if kind == "operator.notice":
                report["counts"]["notices"] += 1
                try:
                    _check(report, envelope["id"], "notice",
                           shadow.verify_notice(envelope, computed))
                except _READ_ERRORS as exc:
                    _note(report, envelope["id"], "notice",
                          reason=f"notice verification failed: {type(exc).__name__}")
            elif kind == "answer":
                subject = envelope.get("subject")
                target = subject.get("type") if isinstance(subject, dict) else None
                if target == "insight" and subject.get("id") in insight_ids:
                    continue  # verified and counted against the full insight above
                report["counts"]["answers"] += 1
                if target == "record":
                    record_answers.append(envelope)
                else:
                    _note(report, envelope["id"], "answer",
                          reason="answer subject is outside the bounded authoritative set")
    except _READ_ERRORS as exc:
        complete = False
        report["answers_complete"] = False
        _note(report, "operator-records", "scan",
              reason=f"operator record scan failed: {type(exc).__name__}")
    finally:
        scan.close()
    if progress.entries_examined >= project.config.limits.operator_record_catalog_scan_entries:
        report["truncated"] = True
        complete = False
        report["answers_complete"] = False
        _note(report, "operator-records", "answer",
              reason="operator record scan bound exhausted; answer coverage is incomplete")
    return complete


def _role_records(project, shadow, report: dict, record_answers: list,
                  answers_complete: bool) -> None:
    """Verify declared role questions and answers that address them."""
    policies = host_records.configuration(project.config.record_kinds)
    if not any(row["route"] == "operator_question" for row in policies.values()):
        for envelope in record_answers:
            _note(report, envelope["id"], "answer",
                  reason="record answer has no declared role question")
        return
    shared = host_records.store(project.root, project.snapshots, policies)
    progress = ScanProgress()
    scan = shared.records(
        namespace="role",
        scan_limit=project.config.limits.operator_record_catalog_scan_entries,
        scan_progress=progress)
    questions = {}
    try:
        for envelope, computed in scan:
            policy = policies.get(envelope.get("kind"))
            if policy is None or policy["route"] != "operator_question":
                continue
            report["counts"]["role_questions"] += 1
            questions[envelope["id"]] = computed
            subject = {"type": "record", "kind": envelope["kind"],
                       "id": envelope["id"], "digest": computed}
            answered = published_answer(record_answers, subject, answers_complete)
            if answered is None:
                _note(report, envelope["id"], "question",
                      reason="answer scan incomplete; question answer state is unknown")
            try:
                _check(report, envelope["id"], "question",
                       shadow.verify_question(envelope, computed, answered=answered))
            except _READ_ERRORS as exc:
                _note(report, envelope["id"], "question",
                      reason=f"question verification failed: {type(exc).__name__}")
        for envelope in record_answers:
            subject = envelope["subject"]
            question_digest = questions.get(subject.get("id"))
            if question_digest is None or question_digest != subject.get("digest"):
                _note(report, envelope["id"], "answer",
                      reason="answer subject is outside the bounded role-question set")
                continue
            try:
                _check(report, envelope["id"], "answer",
                       shadow.verify_answer(envelope))
            except _READ_ERRORS as exc:
                _note(report, envelope["id"], "answer",
                      reason=f"answer verification failed: {type(exc).__name__}")
    except _READ_ERRORS as exc:
        report["answers_complete"] = False
        _note(report, "role-records", "scan",
              reason=f"role record scan failed: {type(exc).__name__}")
    finally:
        scan.close()
    if progress.entries_examined >= project.config.limits.operator_record_catalog_scan_entries:
        report["truncated"] = True
        report["answers_complete"] = False
        _note(report, "role-records", "answer",
              reason="role record scan bound exhausted; answer coverage is incomplete")


def audit(project) -> dict:
    """Compare every declared stage-2 projection in one bounded read-only pass."""
    shadow = getattr(project.insights, "kernel_shadow", None)
    report = _report(shadow is not None)
    if shadow is None:
        return report
    insight_ids = set()
    record_answers = []
    _insights(project, shadow, report, insight_ids)
    answers_complete = _operator_records(project, shadow, report, insight_ids,
                                         record_answers)
    _role_records(project, shadow, report, record_answers, answers_complete)
    report.update(report["counts"])
    return report
