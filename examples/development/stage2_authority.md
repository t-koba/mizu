# Stage-2 authority switch

This note records the remaining stage-2 work after the conversion and its
checks. It is example userland, not a kernel contract. Queue names, message
meanings, roles, wakes and policy stay in `queues.toml`, `policies/stage2.md`
and this directory; `src/mizu/kernel/` does not learn any of them.

## Preconditions

The switch is only safe after `convert_legacy.py --apply` and
`stage2_verify.py` agree: every current proposal, decision, delayed revisit,
role question, answer and notice exists in the declared queues with the same
id, author, body, refs and delivery instant as the legacy authority. Until the
switch, the legacy stores remain authoritative; the queue messages are a
conversion of them, not a second live record.

## What changes at the switch

The switch moves *reads* and *writes* of the stage-2 families from dedicated
modules to `QueueStore` at one commit. It does not add a dual-write or a
feature flag. The same commit removes the legacy modules listed for stage 2 in
[docs/os-migration.md](../../docs/os-migration.md).

Two userland adapters, kept outside the kernel, carry the workflow meaning:

1. **Decision adapter** — reads the `proposals` and `decisions` queues and
   returns the exact page shapes consumed by `decision_delivery.pending`:
   proposal identity and revision, effective decision fields, delayed
   revisit fields, source/addressee, event ids, limits, truncation flags and
   manifests. Writes use `queue_write` to append a decision referencing the
   proposal, and a delayed self-addressed revisit when the decision is a
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

- Decision adapter reads only: reproduce `decision_delivery.pending` page
  shapes from a fixture project containing converted queues; compare with the
  legacy page byte for byte.
- Decision adapter writes only: reproduce `Insights.decide` effects as queue
  appends, including deferred revisits and idempotent retries; compare the
  resulting queue state with the legacy effect.
- Switch the decision read/write call sites and delete the decision-specific
  modules in one commit; keep `stage2_verify.py` green for migrated fixtures.
- Record adapter reads and writes, then the same one-commit switch for
  questions, answers and notices; delete the record-specific modules.
- Shared-module branches for those families are removed only after both
  switches, together with the imports they served.

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
