# Stage-2 authority switches

This is example userland, not a kernel contract. Queue names, message meanings,
roles, wakes and policy stay in `queues.toml`, `policies/stage2.md` and this
directory; `src/mizu/kernel/` does not learn any of them.

## Marker states

`decision-authority.json` is the durable, host-only authority marker:

- absent: legacy decision reads and per-token file receipts remain authoritative;
- `"shared"`: the SharedRecords branch selected by exact legacy-receipt
  reconciliation, unchanged by this switch;
- `"queues"`: non-shared reads and acknowledgements use the declared
  `proposals`, `decisions` and `receipts` queues.

The two activations are mutually exclusive. Shared precedence in reads is
unchanged: the shared branch is always tested first. There is no runtime
boolean flag and no dual-write; the marker is the authority state.

## Gate and activation

The switch is only safe after `convert_legacy.py --apply` and
`stage2_verify.py` agree that every current proposal, decision, delayed
revisit, role question, answer, notice and converted receipt exists in the
declared queues with the same id, author, body, refs and delivery instant.

```sh
python3 examples/development/stage2_activate.py config/config.toml --project NAME
```

This operator-run command reapplies the idempotent conversion, requires the
verify gate to pass, pins both reports as create-only plan/report bytes and
publishes the marker last. An exact retry returns the retained marker; a
changed report or a shared marker refuses. The production entry point is
`mizu.decision_reconciliation.activate_queue_decision_delivery`; no model tool
exposes it.

## What switched

`mizu.queue_decision_delivery` is the production stage-2 intermediate outside
the kernel. It reconstructs the exact non-shared page with the same tokens,
diagnostics, limits and truncation as the legacy reader, using only the three
declared decision queues through a trusted role actor. `stage2_decision_adapter.py`
is now a compatibility shim that re-exports it for the stage-2 parity tests.

Every non-shared read site (decision delivery, idle-cache fingerprint, runtime
wake/selection probes, selection and work order) follows the marker, as does
`unit_finish` token validation. Acknowledgement appends a schema-valid settled
receipt to the host-writable `receipts` queue with an exact token revalidation
against the queue candidates and a stable id for idempotence. The legacy
Insight receipt writer refuses its file write once authority is `"queues"`.
`decision_read` tools and the shared branch are unchanged.

## Record authority switch

Record delivery has a separate `record-authority.json` marker with the same
schema and fail-closed validation. Absence keeps the legacy question, answer,
notice and per-record acknowledgement path. `"queues"` selects
`mizu.queue_record_delivery`, which reconstructs those families from the
declared `questions`, `answers`, `notices` and `record-receipts` queues with the
same envelopes, ordering, diagnostics and token bindings.

```sh
python3 examples/development/stage2_record_activate.py config/config.toml --project NAME
```

The command rechecks the conversion and verification reports for all seven
queues, then publishes the marker last. Decision and record authority markers
are independent; a shared branch, a malformed marker or a stale report always
takes precedence or refuses rather than falling back. Model tools never select
either authority.

## Remaining slices

Shared branches for the record families are removed only after their own
switch is verified, together with their imports. Until then the dedicated
modules named in `docs/os-migration.md` stay in place.

## Invariants

Authentication, append-only history, bounded reads, explicit acknowledgement,
fail-closed validation and the single-holder writable workspace are unchanged.
Runtime routing is never silently redirected: a switch is a passing conversion
gate plus a durable host-only marker. No kernel module names a role, a record
kind, a message meaning, a queue meaning, a workflow state or a wake rule.
Legacy facts are never rewritten by the adapters.
