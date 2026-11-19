# Stage-2 authority switch

This note records the remaining stage-2 work after the conversion and its
checks. It is example userland, not a kernel contract. Queue names, message
meanings, roles, wakes and policy stay in `queues.toml`, `policies/stage2.md`
and this directory; `src/mizu/kernel/` does not learn any of them.

## Preconditions

The switch is only safe after `convert_legacy.py --apply` and
`stage2_verify.py` agree: every current proposal, decision, delayed revisit,
role question, answer, notice and converted receipt exists in the declared
queues with the same
id, author, body, refs and delivery instant as the legacy authority. Until the
switch, the legacy stores remain authoritative; the queue messages are a
conversion of them, not a second live record.

## What changes at the switch

The switch moves *reads* and *writes* of the stage-2 families from dedicated
modules to `QueueStore` at one commit. It does not add a dual-write or a
feature flag. The same commit removes the legacy modules listed for stage 2 in
[docs/os-migration.md](../../docs/os-migration.md).

Two userland adapters, kept outside the kernel, carry the workflow meaning:

1. **Decision adapter** — reads the `proposals`, `decisions` and `receipts`
   queues and returns the exact page shapes consumed by
   `decision_delivery.pending`: proposal identity and revision, effective
   decision fields, delayed revisit fields, source/addressee, event ids,
   limits, truncation flags, receipt diagnostics and manifests. Writes use
   `queue_write` to append a decision referencing the proposal, and a delayed
   self-addressed revisit when the decision is a
   deferral with a parseable instant. The host binds the author from the
   authenticated unit; no adapter API accepts a principal.
2. **Record adapter** — reads `questions`, `answers` and `notices` and returns
   the exact rows consumed by question listings, answer evidence, due notices
   and the host inbox. Writes append an answer referencing the exact question
   or proposal id. Unboundable answers and ambiguous references fail closed;
   they are never guessed.

Both adapters read through bounded, explicit cursors and acknowledge only
after the caller has settled the corresponding event. They preserve the legacy
byte-level event digests and idempotency tokens where those are part of the
runtime contract. Queue writes remain authenticated appends; an adapter cannot
grant itself a queue right.

## Ordered slices

Each slice is independently verified and snapshot-bound.

- Decision adapter reads only (implemented): `stage2_decision_adapter.pending`
  reproduces `decision_delivery.pending` page shapes, receipt suppression and
  corrupt-receipt diagnostics from a fixture project containing converted
  queues; `tests/test_stage2_decision_adapter.py` compares canonical bytes with
  the legacy page.
- Decision adapter writes only: reproduce `Insights.decide` effects as queue
  appends, including deferred revisits and idempotent retries; compare the
  resulting queue state with the legacy effect.
- Switch the decision read/write call sites and delete the decision-specific
  modules in one commit; keep `stage2_verify.py` green for migrated fixtures.
- Record adapter reads and writes, then the same one-commit switch for
  questions, answers and notices; delete the record-specific modules.
- Shared-module branches for those families are removed only after both
  switches, together with the imports they served.

## Implemented decision read slice

`stage2_decision_adapter.py` is an example-userland module, outside
`src/mizu/kernel/`. Its only entry point is
`pending(project, role, *, actions=None, limit=None, include_insights=True,
sources=None")`; it returns the legacy dictionary (`shared`, `events`,
`diagnostics`, `page`, `manifest`) and accepts no actor or principal. The actor
presented to `QueueStore` is always `role:<role.name>` from the trusted role
object. An omitted `sources` follows `role.decision_sources`, or `own` when
unset, exactly as the legacy call does. It reads only the declared `proposals`,
`decisions` and `receipts` queues from sequence one with the kernel page size
and the stage-2 page-count bound, and never acknowledges, appends or advances a
cursor.

Conversion retains the metadata this reconstruction needs: proposal instant,
kind and urgency for `insight.pending` payloads, the declared `receipts`
messages for per-token settlement, corrupt reasons and enumeration order, and
the decision's exact history event id, decision key, run and canonical history
record for acknowledgement tokens. The event id is the `decision-history` filename: for
deferrals it is not the decision key. The whole record is retained because
work-item-bearing decisions carry further token-bound fields. Messages whose
metadata is malformed or does not bind its proposal are refused before any
event is built. Legacy page calls appear only in the comparison tests.

Because the queue has no per-message filter and any configured role can be an
insight addressee, the example `queues.toml` grants every role read of
`proposals` and `receipts`; writes and wakes are unchanged. Conversion binds
each receipt token to a current legacy candidate in legacy diagnostic order to
infer its action and fixed-width hex order, then uses the exact legacy receipt
reader to classify settlement or one of the three corrupt reasons. The adapter
suppresses settled candidates before applying limits and returns the legacy
diagnostic shape for corrupt ones ordered by that enumeration under the legacy
cap; it never opens a legacy receipt file. This read slice does
**not** switch a runtime call site, add a flag, dual-write or serve a
shared-authority page; the shared store remains a later slice of the same
switch.

## Invariants at every step

- Authentication, append-only history, bounded reads, explicit
  acknowledgement, fail-closed validation and the single-holder writable
  workspace are unchanged.
- Runtime routing and consumer cursors are never silently redirected: a
  switch is a source change plus a passing conversion gate.
- No kernel module names a role, a record kind, a message meaning, a queue
  meaning, a workflow state, a wake rule or a branch.
- Legacy facts are never rewritten by the adapters. Once switched, the legacy
  stores are no longer read for these families and are retired with their
  modules.
