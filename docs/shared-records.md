# Shared record delivery: design, implementation and activation

**Status:** bounded immutable records, decision and addressed pending-insight
projection/pages/retrieval, exact legacy receipt reconciliation, and a host-only
one-way runtime activation for `insight.decision` including `insight.pending`
attention receipts. Legacy remains the sole delivery/wake/ack authority for
those kinds until their explicit, parity-verified durable marker; no model tool or
configuration default can activate that cutover. Operator-written `answer` and
`operator.notice` records are a separate active SharedRecords slice, enabled
only by their explicitly declared `operator_records` kind policy. Other source
families retain their existing authorities.

## Operator answers and notices (active without decision cutover)

The host `mizu record` CLI uses the same trusted human/operator control plane as
`mizu insight submit`: access to the private host configuration and managed data
is the authentication boundary. The CLI is not an RPC/model tool. Its writer is
always `operator.cli`; neither the operator command nor a model can supply a
writer field. Model sandbox files, Editor text and origin labels grant no writer
identity. No text is parsed to infer a kind. Insights remain proposals; these
records are never Insight decisions and do not mutate or resolve a question.

The configuration defaults (also in `config/config.example.toml`) are:

```toml
[operator_records.answer]
enabled = true
wake = true
route = "question_source"
retention = "permanent"
[operator_records."operator.notice"]
enabled = true
wake = true
route = "addressee"
retention = "permanent"
```

Only these two kinds, their stated routes, boolean enablement/wake and permanent
retention are supported in this slice. Unknown kinds/keys/routes/retention fail
configuration validation. An omitted kind table is disabled; only explicitly
declared kinds are enabled. Disabling a kind stops new writes and new deliveries,
not audit retrieval, already-frozen acknowledgements or existing receipts.
Permanent immutable retention is the only supported policy: no TTL, automatic
deletion, expiry or redaction. Future finite retention requires separate design
covering audit references, receipts, backup/restore, deletion eligibility and
dry-run reporting (operator answer `answer-c4114659257f48b790b42de30e32eecf-r1`).
Both content and closure records, plus exact receipts, remain in the existing
backup/restore audit members.

Pending enabled records with `wake = true` admit the addressed role through the
existing event and on-change gates (`operator_record` admission). Armed daemon readiness consults
SharedRecords directly, including after `done`; paused/draining controls still
win. Existing service-managed event/scheduled/on-change roles watch the sharded
`shared-records/operator` partition (systemd PathChanged and launchd WatchPaths
triggers are not recursive, so the parent directory needs no watch; Windows retains
schedule triggers or the existing bounded event-only recovery poll). Explicit-run-only roles remain explicit-run-only: use
`mizu run PROJECT ROLE` for them. `wake = false` retains delivery on independently
admitted units but stops record admission and wake hints. No new queue, scheduler service, assignment, review, publication or Insight authority is
introduced. Acknowledged or closed records cease to admit; omitted acknowledgements
remain eligible. Re-render operator-managed services to install the new watch;
these changes do not modify installed services.

For an answer, supply the full existing question/Insight ID **and exact revision**:

```sh
mizu record PROJECT publish --kind answer --id answer-event-1 \
  --subject-id FULL_INSIGHT_ID --subject-rev 1 --body 'The explicit answer'
```

`--id` is a stable event identifier (1–63 lowercase letters/digits/underscore/
hyphen), required for uncertain-response retries. Identity is
`operator/PROJECT/EVENT_ID` in the existing SharedRecords store. Canonical files
are sharded by the record ID's first component under
`shared-records/<namespace>/` (long namespace components use a bounded digest
directory; operator records live under `shared-records/operator/`); pre-partition flat `shared-records/*.json` files
remain readable by exact `get`, the unfiltered scan and operator dispatch without
gaining a second copy on replay. The generic `records(namespace="operator")`
scanner still covers only the operator partition; dispatch instead uses the
checked metadata projection described below. The kind `answer`
is single-component; other envelope IDs and writers remain namespaced. An answer
requires subject `{type="insight", id, rev}`; prefixes, missing revisions and
unavailable revisions are refused. A retained prior revision can be answered.
The asking role is the question's authenticated `source`, not its free-text body,
origin label or current holder. That source must be a configured project role.
Optional `--role` must equal that role; it cannot redirect the answer.

A notice requires an explicit configured project role. Its optional exact Insight
subject uses the same `--subject-id`/`--subject-rev` pair but does not change routing:

```sh
mizu record PROJECT publish --kind operator.notice --id notice-event-1 \
  --role worker --body 'An operator notice'
mizu record PROJECT supersede --kind operator.notice --id notice-event-2 \
  --role worker --target-id operator/PROJECT/notice-event-1 \
  --target-digest EXACT_ENVELOPE_DIGEST --body 'Replacement notice'
mizu record PROJECT withdraw --kind operator.notice --id notice-withdraw-1 \
  --role worker --target-id operator/PROJECT/notice-event-2 \
  --target-digest EXACT_REPLACEMENT_DIGEST
```

CLI output contains the immutable envelope and recomputed `record_digest`.
Notice supersession/withdrawal appends another `operator.notice` record with
payload `{action, body, addressee, target}`. Publish has null target; supersede
has replacement body and exact `{id, digest}` target; withdraw has empty body and
subject. Only the same authenticated writer can close a notice for the same
addressee. A notice can be closed once (an exact retry remains valid); answers,
withdrawals, self-targets, missing targets, and digest mismatches cannot be closed.
Withdrawal records are audit-only; supersession delivers the replacement content.
Originals are never edited or deleted.
No Insight decision is used for either kind.

Bodies are nonblank, at most 32768 Unicode characters (withdrawal excepted), and
the canonical envelope is at most 140000 UTF-8 bytes. All shape/key/type checks are
closed. Writes/closure selection and manifest freezing share a host lock; a
reused event ID must match every immutable field. Replay preserves the first
host timestamp; a lost response is retried with the **same event ID and content**.
No automatic retry, canonical-record repair, timestamp cursor, second queue or
mutable lifecycle pointer is introduced. Exact retries may reconstruct only the
disposable operator-routing projection from its unchanged canonical record.
Unsafe/corrupt records, projections or receipts fail closed.

### Derived operator routing metadata

`operator-record-index/<SHA256(record ID)>.json` is a disposable host-only
projection, outside generic `shared-records` scans and **excluded from backup**.
A restore rebuilds it from retained canonical envelopes on first lookup. Each
canonical JSON reference has exactly `{envelope, digest}`; `digest` is the
SHA-256 of the complete canonical bytes. Operator `envelope` retains all fields
except that `payload.body` is the empty string. Foreign legacy flat-file
projections omit payload and subject (both `{}`) and are used only to classify
identity, never to deliver another source's events. References are bounded by
the existing shared-record byte bound; they contain no copied bodies, receipts,
queue position or delivery cursor. Event identity remains globally
`operator/PROJECT/EVENT_ID`, **never** recipient-dependent.

Writes and lookups hold the existing operator-record lock. A write publishes the
immutable envelope first, then its projection; an exact retry repairs a missing
projection before returning. Lookup enumerates operator canonical filenames and
all legacy flat filenames, checks completeness against reference filenames, and
reconstructs only missing references by fully validating canonical content.
Already-present malformed, dangling, duplicate/misnamed, conflicting or unsafe
references refuse rather than being overwritten. Unpublished regular
`.new-XXXXXXXX` atomic-write scratch files are not references. Identical
legacy/sharded copies share one reference; conflicting copies refuse.

On **every** lookup, all covered canonical bytes are safely read and hashed.
For operator references, canonical JSON framing before and after the body value
must match the projected metadata byte-for-byte, authenticating routing, subject,
kind, time and closure target independently of the reference's digest. Foreign
legacy identity prefixes and writer suffixes are checked against canonical bytes too. Changed
canonical bytes or reference metadata fail closed, even for another recipient
or a disabled kind (if all kinds are disabled, delivery stays closed without a
lookup). Canonical bodies are fully decoded/validated on projection
creation/recovery and on selected manifest freeze/read; warmed lookups decode
only compact metadata for unrelated recipients. Closure uniqueness and exact
target routing/digests are checked from that same authenticated metadata set.

This bounds unrelated **body decoding**, not total history I/O: filename and
metadata work remain linear in retained history, as do canonical byte reads and
hashing, deliberately preserving global corruption detection without trusting
mtime. Cold/recovery lookup still decodes missing projections' bodies. The index
is not a second canonical source or queue; permanent envelopes and exact
role-bound receipts remain authoritative. The existing event watches still
observe canonical `shared-records/operator` publication, not index maintenance;
a waiting query holds the same lock and can recover a canonical-first write.

Unit full/delta prompts carry `operator_records` with up to 20 bounded references
across both kinds, `pending_count`, `truncated` and `wake_pending` (whether any eligible unacknowledged
record has wake enabled, including rows beyond the delivery page). Rows carry exact record ID,
digest, role and ack token, kind and subject; full body is not copied into the
prompt. Acknowledged rows are filtered before oldest-UTC-time/ID selection.
The host records the frozen page in the run's `operator-record-delivery.json`
(delivery evidence, not another authority). `record_read` is granted only for a
nonempty unit manifest and pages the exact envelope by ack token/character offset,
limit 1–8192. Reading does not acknowledge. The addressed role explicitly selects
handled tokens in `finish.ack_events`; omitted/partial selections acknowledge
nothing/only selected rows. Foreign, duplicate, tampered or undelivered tokens
are refused before finish intent. Already frozen deliveries remain readable and
acknowledgeable after notice closure, question revision or policy disablement;
this never reopens the notice or changes an Insight. Receipt retries preserve the
first exact acknowledgement.

## Scope and invariants

Use one small, append-only record mechanism for delivery, wake hints, and per-recipient acknowledgements. It does **not** replace any source's lifecycle or authority:

- `Insights` continues to own proposals, revisions, decisions, and decision waits. `WorkItems` continues to own task seeds, CAS transitions, holder, waits, history, and completion. A task marked `done` still requires passing verification bound to the exact published snapshot and the workspace tree it carries; `code_digest` remains the whole-capture digest.
- CI adapters, check jobs, review inputs, and publication receipts keep their existing evidence rules. Generic delivery cannot approve a publication, prove external-actor provenance, assign work, transition a task, or turn a diagnostic into a task/dependency.
- A delivered record is immutable and has a stable identity namespaced by project and source event identity, not its prose or a timestamp. A source retry with the same identity and same content is idempotent; a conflicting reuse fails closed. Record payloads are bounded and kind-validated.
- A record is routed only by current trusted kind policy over authenticated writer and structured subject/source fields, evaluated separately at delivery and wake time. The writer cannot choose arbitrary recipients. `dependency.recipient` is routing only, not assignment or authority; free text is never parsed to infer work or a dependency.
- Each recipient acknowledges exact record IDs and digests explicitly. The host freezes an exact `{role, record_id, record_digest}` delivery manifest for the current unit and accepts only explicit selections from it. A successful unit with no explicit selection acknowledges nothing; a partial selection acknowledges only those records. Receipts are role/record/payload-bound, create-only and replay-safe. No timestamp watermark or completion-as-ack.
- Delivery order is oldest-first by the source event's immutable timestamp normalized to UTC, then stable record ID. This is deterministic event-time order, not arrival FIFO: late backfill or clock rollback can insert an older event ahead of retained pending events. Acknowledged rows are filtered before page selection, so a hole or partial acknowledgement cannot hide older pending records. Count and aggregate serialized-byte limits (including envelope, preview/reference, pagination and overflow metadata) apply across all configured kinds, with explicit truncation/overflow; independent per-kind page sizes must not add up to an unbounded prompt.
- Wake is a separate, declarative policy field. Record existence, reading, diagnostic content, and task readiness do not imply admission. Only explicitly configured, typed event kinds may signal a wake; that signal does not itself create work or mutate source state. No records means no new route or wake authority.
- One source has one active delivery/receipt authority at a time. During migration a shadow projection may compare sets, but it must not be prompted, acknowledged, or used for admission.

A minimal proposed record is:

```json
{
  "schema": 1,
  "id": "project/source/event namespace",
  "kind": "namespaced.kind",
  "created_at": "aware source-event time",
  "writer": "authenticated product principal",
  "subject": {"type": "kind-specific exact source binding", "id": "source id", "rev": 1},
  "payload": {"...": "kind-specific bounded immutable data"}
}
```

The record digest is `sha256(canonical(record))`; it is recomputed, not self-asserted. A separate receipt is `{role, record_id, record_digest, acknowledged_at}`. The authenticated `writer` is the principal authorized to project the source event, not the insight's origin/submitter or the payload's claimed author. Source origin is separately validated for `own`/`any` routing; it never grants writer authority. The subject above is illustrative, not a universal three-field schema: each kind must bind its exact insight/decision revision, task transition, wait/revision/holder and wait digest, snapshot/whole-capture digest, or publication receipt/branch/SHA/check set as applicable. Project/source namespaces prevent cross-project or cross-source identity collisions.

Kind policy declares a closed payload validator/size bound, authorized writer, structured route, role subscription/filter, whether a wake is permitted, the source-current eligibility predicate, and retention. These are declarations, not executable expressions. Resolved recipient sets are not immutable record fields (typed addressee/source fields may be): current trusted policy resolves delivery and wake independently. New subscribers see retained events that are still source-eligible; removed recipients receive no new deliveries or wakes. Remove/re-add does not erase a role's exact prior receipts. Policy changes cannot rewrite an already-frozen delivery manifest or let one role acknowledge another's delivery. No model-facing generic record-write capability is added.

Source-current eligibility is distinct from retention and acknowledgement. A retained obsolete insight decision or CI result whose wait revision/holder moved must not be newly delivered or woken, even without a receipt. Revalidate eligibility and current routing at delivery/wake; neither retention nor absence of a receipt proves eligibility. Conversely, a source policy may permit an immutable event already delivered in the current unit to be explicitly acknowledged by its manifest identity after source movement (insight decisions do; CI waits and terminal work items retain their current-revision refusal); this records handling of that delivery only and never mutates or revives the source. Retention keeps evidence, not delivery authority.

Keep each kind's existing event-time meaning, including the blocked revision's `updated_at` for a CI waiter, rather than replacing it with projection/arrival time. Filter source-ineligible and exactly acknowledged rows before bounded oldest-first selection. If the oldest eligible payload exceeds a page, deliver a bounded digest-pinned preview/reference with paged retrieval of the exact immutable content; the delivery manifest still binds the full record digest. Retrieval must validate that identity across pages. Do not silently skip the row or stall forever behind it. The byte budget must accommodate the bounded reference and metadata; otherwise report an explicit configuration/budget failure, not an empty successful page.

For the active `insight.decision` pilot, `decision_page_bytes` is an explicit per-role budget required for every enabled subscriber at activation (1..1 MiB; no default). It bounds the actual UTF-8 JSON contribution from `decision_events` plus `decision_page`, including keys, formatting, tokens, references and truncation metadata; it does not bound the entire prompt. The full prompt also contains other sections and the manifest-only read-tool surface, so reserve space for those. No other kind consumes this setting in the pilot, and no cross-kind aggregate default is implemented. The 1 MiB limit is an absolute ceiling, not a target.

## Current source inventory

| Family | Current authority and authenticated writer | Subject, route, and current delivery/closure | Current bounds and retention |
|---|---|---|---|
| Insight proposals and decision events | `Insights` owns the live inbox/revisions, decision pointer/history, and permanent decided-insight archive. Legacy receipts remain the runtime delivery authority by default; only the explicit host activation marker selects shared per-role receipts. Writers use the existing insight/decision capabilities; editor imports and operator-originated entries use their existing host paths. | Open proposals also appear in the capability-gated `pending_insights`/`open_insights` backlog; that is not a decision receipt. A decision event binds insight ID, current revision, action, source, title/body, reason/revisit and evidence. `decision_events` filters configured actions and `own`/`any` source routing. `finish.ack_events` explicitly selects delivered `ack_token`s; only selected exact events are receipted after the sealed result is durable. The source decision/revision, not the receipt, closes or supersedes the proposal. Legacy timestamp cursors are not acknowledgement evidence. | Per-role event count is configured (API limit 1–100), and each decision record read is capped at 1 MiB. Legacy delivery has no page-byte ceiling; after explicit shared activation, every decision subscriber must set `decision_page_bytes`, which bounds the serialized `decision_events`/`decision_page` contribution. The live inbox follows `retention_days`, but archived bodies and decision/history evidence are permanent. |
| Review result and findings | Reviewer findings submitted through the product are ordinary `Insights`; there is no distinct durable aggregate review-result store in the inspected product path. External VCS proposal rows may carry up to 64 reviewer/verdict/SHA facts and are normalized into proposal insight revisions by the trusted adapter; those facts remain external-untrusted. | A finding is bound to its insight revision and any reviewed snapshot/SHA evidence. Reviewers route through configured insight subscriptions. An aggregate verdict, if delivered as a separate source, needs its own explicit `review.result` record bound to the exact whole-capture `code_digest`; findings are separate `review.finding` records bound to that same snapshot and stable finding IDs. Neither record changes the host publication approval requirement. | Findings inherit insight bounds and permanent archive behavior. VCS proposal pages are bounded to 256 proposals and 64 review rows each; no independent review-result acknowledgement store was found. |
| Terminal work-item events | `WorkItems` owns seeds, history, CAS lifecycle and terminal state. `work_item_events` derives a read-only event from a validated terminal revision; it does not own task state. The transition actor and origin submitter remain distinct. | Event identity is `work_item:<id>:<rev>`; `done`/`dropped` payloads bind the exact terminal transition, origin-source route, and (for `done`) verification digest. Configured `work_item_events` actions and `own`/`any` source routes determine addressees; unknown origin stays visible as unknown. Exact per-role receipts are written only for delivered role/payload-bound `ack_token`s explicitly selected in `finish.ack_events`, after durable successful result/publication. Empty/omitted selection acknowledges none; unselected events replay oldest-first. The existing terminal receipt format is unchanged. Terminal status is source closure, not delivery acknowledgement. | Full ledger/history validation, oldest-first bounded heap; up to 100 rows, at most 8 KiB per row and 1 MiB per page. Role page size is configurable. No separate event-history pruning policy; task history and exact receipts remain with the ledger. |
| Structured waits and obligations | Decision waits are bound to an insight ID/revision and the exact defer decision; WorkItem waits remain in the WorkItems history. Dependency satisfaction is checked against a specific insight revision/action. | Due insight waits route to the insight's source role; `pending_obligations` routes structured dependency visibility to `recipient` but does not admit a run by itself. Both projections carry role/source/canonical-payload-bound `ack_token`s and an exact `registration_id`. Only explicit `finish.ack_events` selections apply after durable successful result/publication: `consume_waits` removes only a still-matching registration, and atomic per-role `obligation-receipts` suppress only the acknowledged exact registration. Empty/omitted selections consume none; re-registrations survive. CI wait rows sharing `wait_events` use their separate default-off SharedRecords gate described below. Neither route establishes task assignment or external provenance. | Due waits and obligations each return globally earliest registrations, at most 10 by default (API bound 100), after eligibility/receipt filtering. Stored registration reads and token-bearing rows are capped at 1 MiB each; receipts at 4 KiB. Full insight text remains, without one shared prompt byte budget. Corrupt/unsafe receipts fail closed without repair. Legacy absent IDs use a bounded stable digest fallback; malformed explicit IDs never list. Stale/unbound waits are pruned; explicitly consumed waits are removed. WorkItem history remains authoritative. |
| Exact-proposal merge waits | `WorkItems` owns the receipt/head/base-pinned blocked revision; proposal polling owns complete normalized observations, retained through the existing `vcs` proposal insights. Only `host.vcs` writes immutable `proposal.wait` records. | Exact proposal ID plus both repo/ref/SHA endpoints must be observed merged in a complete enumeration. Current blocked revision/holder eligibility is rechecked; explicit `finish.ack_events` applies generic SharedRecords receipts only after successful completion. No work-item transition. | Existing 256-row single-enumeration bound; 8192-byte envelopes, oldest-first default 10 events (maximum 100). Immutable per-blocked-revision evidence and receipts; incomplete pages, missing rows and endpoint changes cannot create evidence. |
| CI status and receipt-pinned CI waits | Adapter status is external-untrusted; ordinary status/failure projections are cached host observations. A WorkItem's structured CI wait and its publication receipt bind the expected branch, exact published SHA, declared check set, item/revision and holder. CI requests have a separate create-only dispatch/outcome record keyed by publication receipt, SHA and workflow set. | General CI failure rows are diagnostics, not task state. Current runtime/docs also allow a due aggregate CI-red projection to signal admission; that is intentional writer-wake policy (see resolved CI policy below). The cache is the observation producer, not a special authority. A terminal waiter result routes only to the matching current blocked holder, is digest-bound to its exact wait, and only wakes: it never transitions the item. By default the host auto-acknowledges delivered CI waiter evidence after a successful unit. The explicit host gate `vcs.ci_wait_shared_records` enables `ci.wait` shared delivery and only selected per-record receipts, retaining current source checks at acknowledgement; legacy auto-acks are not mapped. CI request/read/approval grants remain separate. | CI failure cache is bounded (128 branches × 16 checks/branch, 32-row prompt projection). Wait evidence is create-only, at most 128 KiB; polling is opt-in and bounded by the configured per-tick cap. `ci_waits.pending` returns at most 10 by default (API bound 100); CI requests scan at most 10,000 directory entries/1,000 candidate requests and list at most 100. Existing source-specific evidence remains; no shared retention policy exists. |
| Check-job results | `check_jobs` owns queued/running/terminal job records, command evidence, input digests and result files; the result is not a task transition. The job owner and optional producer are host-bound. | Prompt delivery selects `job.role` only. `action=result` and `logs` authorize either that role or `producer_role`; read authorization is not acknowledgement authority. Only the owner role can acknowledge, and this may mark same-role shared-result subscriptions together after a durable successful unit. Superseded demand can already be marked `acknowledged` by withdrawal without result handling; that flag alone is not proof of handling. A verification pass is imported only when source, commands and environment still match. The bounded check wait is a separate role deferral, not a result acknowledgement. | Up to 32 terminal results are projected per role; logs are paged at 8,192 characters. At 1,024 retained job records the current room-making path can evict acknowledged nonexecuting jobs subject to its shared-result protections. Migration must keep required deliveries and referenced result evidence available; a generic notification receipt alone must not authorize their eviction. Existing job/result authority remains. |
| Operator answers and notices | Trusted host CLI writes `answer` and `operator.notice` directly to SharedRecords as `operator.cli`; no model writer. | Answers bind an exact Insight ID/revision and route to the authenticated source role. Notices route only to their explicit validated addressee. Both use manifest-scoped `record_read` and explicit `finish.ack_events`. Notice writer closure/supersession appends an immutable closure event; neither kind uses Insight decisions. Pending eligible records can be a configured wake source through existing scheduler gates; no new scheduler service. | 32768-character content, 140000-byte envelopes, 20 aggregate references plus pending/truncation metadata; permanent immutable record/closure/receipt audit. See operator CLI and configuration above. |
| Publication blockers | Publication intent/receipt records are host VCS evidence, bound to approval, run, whole-capture `code_digest`, public-source projection and exact published SHA. A failed operation is also a run/tool result; the inspected product path has no distinct durable `publication.blocker` inbox. | A future blocker record must be emitted only from a validated host publication outcome, bind the exact publication attempt/receipt or source snapshot, and route under explicit policy. It cannot grant approval, reinterpret `code_digest`, or infer a task/dependency from an error message. Publication approval and adapter reconciliation remain unchanged. | Publication receipts are immutable project evidence; failures remain run evidence. No common blocker retention or acknowledgement policy exists. |

Across the families, source history is authoritative and retention differs: insight archives are permanent, WorkItems retain validated revision chains, check jobs use an acknowledged-eviction cap, and other evidence stores keep their own bounds. The shared delivery store must not add a destructive global TTL. Unacknowledged records and unresolved source events are never silently expired; any later pruning is a documented per-kind policy and cannot remove source history or verified completion evidence.

## CI-wait explicit-receipt preflight (shadow only)

The bounded isolated experiment `f249c22172974261904ef623ca3ad616` projected
four terminal waiter results (three routed to `worker`, one to `reviewer`) from
the existing `CiWaitTests` fixture into disposable `ci.wait.result`
`SharedRecords` envelopes. Reading them through frozen role manifests exactly
reconstructed the current `ci_waits.pending` event fields and evidence digests;
manifest order followed the current `updated_at`/item-ID order, and envelope
`created_at` preserved the blocked WorkItem revision's `updated_at`. Omitted
and empty selections wrote no receipt; one exact selection receipted only that
record; a wrong-role selection was refused. This is contract evidence for the
existing transport only, not integrated runtime or performance evidence.

The experiment also moved one waiter to a new revision/holder: current
`ci_waits.pending` no longer routed its old event, and the fixture's
source-current acknowledgement check rejected the old selection.
`tests/test_ci_waits.py::CiWaitTests.test_stale_observation_and_old_delivery_cannot_reach_reopened_waiter`
exercises the existing refusal. `SharedRecords` itself intentionally validates
only the frozen role/record/digest manifest and allows an exact receipt after
source movement; therefore a CI adapter must keep the current
blocked-revision, holder, wait-digest and evidence checks before both new
delivery and acknowledgement. `WorkItems` history/CAS, the exact
`Publication` receipt, and `ci_waits.terminal`'s unique declared check set,
exact SHA, and terminal-state validation remain authoritative; the shared
record is notification evidence, never a task transition or publication
approval. Preserve `external-untrusted` status evidence and the existing
oldest-first filtering before page limits.

A separate bound-oriented fixture `b558226bbbf342bd940dadba3bebb7b1` carried
64 declared checks and maximum-length run IDs. Its source evidence was 23,589
bytes and the compact shared envelope 25,716 bytes, both below the unchanged
131,072-byte per-waiter evidence bound; no size cap was raised. This is not a
proof for every accepted payload shape, so an implementation must retain the
same cap and test its full boundary before activation.

The legacy CI acknowledgement is not mappable to a SharedRecords receipt: the
current file contains only role, source evidence digest and acknowledgement
time, and is written after unit success without an explicit per-record token
selection or run binding. `SharedRecords.received` therefore remains false for
that old acknowledgement in the experiment. Do not import it as handling;
under a future cutover an otherwise-current result without a new explicit
SharedRecords receipt may be replayed. Stale source events remain ineligible.

No production CI kind policy, source store, wake, or runtime activation changed
here. A later cutover must use the existing `SharedRecords` store and explicit host-only
kind/route activation, must not write a parallel CI acknowledgement file, and
must prove durable replay, partial selection, wrong-role and stale-revision
refusal, backup/restore, exact publication/check validation and bounded reads.
Code presence alone never activates it. Stop if the source checks cannot be
revalidated before delivery and selected acknowledgement, if ordering or
payload identity changes, if legacy auto-acks would need to be inferred, or if
the existing 128 KiB cap cannot be preserved.

## Terminal work-item event shadow preflight (fixture only)

Experiment `d05a868b0a6f4a9fac50e451cbcdc975` projected 24 deterministic
terminal events (23 `dropped`, one `done`) into a disposable `work-item.terminal`
SharedRecords envelope, then compared the bounded pending view with
`work_item_events.pending`. The candidate repeated WorkItems seed/full-chain
and current-terminal validation before exact shared-record and receipt checks;
WorkItems remained authoritative. With tied event times and mixed reviewer /
searcher origins, the initial and post-partial-ack views matched exactly; exact
record replay was idempotent, conflicting identity and cross-role selection
were refused, and the fixture had no unseeded decisions. This did not exercise
terminal revision correction or maximum-size rows; the existing
`test_corrected_terminal_revision_invalidates_old_delivered_token` regression
remains relevant.

The largest fixture row was 1,926 bytes and envelope 2,195 bytes, below the
unchanged 8 KiB row cap; page JSON was 16,726 bytes before and 16,827 after
receipts, below the 1 MiB page cap. Five page-read samples had median 35.745 ms
for the legacy path and 54.859 ms for the authority scan plus shadow lookup.
This small fixture is not a production performance estimate: it shows no
speedup, and retaining source-current validation while adding immutable record
and receipt checks adds work. It justifies neither a cutover nor an index; any
future migration needs a separate design and evidence for source-current
acknowledgement, correction/replay, backup/restore and full-size bounds.

## Structured wait and obligation shadow preflight (fixture only)

Experiment `7e65ad00a6ac4c27af77a1435f592728` compared the current
`Insights.due_waits` and `pending_obligations` paths with temporary
`insight.wait` and `insight.obligation` SharedRecords envelopes. The deterministic
fixture held four due deadlines, one satisfied dependency wait, and four
unresolved obligations. Per-source business payloads and event-time order
matched after excluding each transport's distinct acknowledgement token; a
cross-role SharedRecords acknowledgement was refused. After one explicit wait
consumption and one obligation receipt, then revising an unacknowledged waiter,
the current projections still matched. The shadow retained all nine immutable
records; three no longer appeared in current source projections, including the
revised waiter. Therefore SharedRecords records alone cannot replace the
source-current revision, dependency and registration checks.

The nine legacy event rows totaled 3,735 canonical bytes (largest 599); the
SharedRecords envelopes totaled 6,303 bytes (largest 875). In the measured
post-partial-ack fixture state, nine local samples had median 8.143 ms for the
current source projections and 16.732 ms for those projections plus shared
record/receipt validation. These are disposable local fixture measurements,
not production or Windows estimates. They show no performance case for a
cutover or index and preserve the need for current-source filtering; no runtime,
wake or admission path was changed.

## Bounded disposable pilot

The high-volume representative was `insight.decision`, because the current decision-event reader already handles exact per-role receipts and has a 205-event oldest-first regression. The experiment created 205 rejected insights in an isolated test `Fixture`, repeated each read five times, built the normal prompt locally, and projected the same events into a scratch common envelope (`kind`, stable decision event ID, author, insight/revision subject, bounded payload) with scratch per-role receipts. It changed no source files and used no model/provider, network, CI runner or deployment. Evidence: experiment `0adfa0cbd6e46e847d04f211c87795caf7a1398b2a6d83758097ece8382de1ea`.

| Page | Current `decision_events` scan median/sample max | Current `prompt_for` build median/sample max | Current event JSON / full prompt bytes | Scratch shared-file scan median/sample max | Scratch shared-event JSON bytes |
|---:|---:|---:|---:|---:|---:|
| 10 | 138.410 / 149.462 ms | 87.944 / 91.565 ms | 3,471 / 5,701 | 26.847 / 28.682 ms | 3,431 |
| 100 | 115.133 / 137.395 ms | 76.255 / 86.956 ms | 34,701 / 39,092 | 25.965 / 26.856 ms | 34,301 |

| Retained bytes for 205 events | Current source-shaped records/receipts | Scratch normalized projection/receipts |
|---|---:|---:|
| Decision history records / common records | 42,845 B | 70,110 B |
| Exact per-role receipts | 38,540 B (188 B/receipt mean) | 41,820 B (204 B/receipt mean) |

The normalized envelope's delivered JSON was 40 B smaller at 10 rows and 400 B smaller at 100 rows in this sample because it replaced the source-specific acknowledgement token with a stable record ID and shared fields. Its stored record files were larger than decision-history files because the projection carried the full title/body/evidence payload; those source decision files do not. Receipt size grew by 16 B per row in this particular serialization. These are shape measurements, not a product capacity estimate.

The scratch scanner was plain `os.scandir` + `json.loads` and intentionally omitted the production reader's safe-path checks, exact source-chain validation, locks, receipt validation, and durable writes. Its lower scan time is **not** evidence that a production shared store will be faster; correctness and no-follow/fail-closed behavior must be retained. With five samples, the reported upper statistic is a sample maximum, not a meaningful p95. The initial pilot remains useful for shape and receipt-storage comparisons: 100 small decision records used about 34.7 KiB of event JSON in this fixture. It does not establish which cost dominates an integrated build.

### Corrected full-prompt comparison

Experiment `3a2e941a2322b6589655ab68b194cc07b4ee2c365d760fd89beb4bc04fcfc742` used the same `prompt_for` and common fields on both paths, injecting the normalized field at final serialization. Source scanning was outside the measurement; each path had five samples. This corrects earlier experiment `b48c`, whose prebuilt base was not comparable for full-build timing and is not used here for that claim.

| Rows | Legacy full-prompt median / sample max | Legacy bytes | Shared full-prompt median / sample max | Shared bytes |
|---:|---:|---:|---:|---:|
| 10 | 201.175 / 259.547 ms | 5,701 | 166.925 / 288.886 ms | 5,466 |
| 100 | 92.128 / 118.436 ms | 39,092 | 126.757 / 143.673 ms | 38,856 |

These maxima are not meaningful p95 estimates with `n=5`. The mixed timings do not demonstrate a speedup: order effects/noise remain, and integrated shared-store validation is omitted. These are local full-prompt construction measurements, not model latency or a causal scan-dominance result. A production slice must measure matched workloads with source validation, current eligibility and aggregate prompt limits in place.

### Production-safe projection/read baseline

Experiment `83aee77506a2ccd56eb73538673e15ba7873236239cfee7f6f88b37c99acd13c`,
command evidence `104c57b71b924d9b8cc4ba7bbf91586c`, used code digest
`089ee4252ec14dc7e3146ed3032e39ab4efe116c2209593de0135ac47beec6db`. It made
205 short rejected insights in a disposable `Fixture`, then compared the legacy
reader with a prototype using `project_decision_records()` plus the production
`SharedRecords.get/received/freeze` paths. Five samples alternated order. No
legacy or shared receipts existed; both event lists matched after removing the
role-specific token, and both full prompts used the same `prompt_for()`. The
shared timing includes full source reconciliation, exact idempotent record
validation, safe record reads, receipt checks and manifest creation. This is a
historical shadow-only prototype measurement; it does not measure the later
active page path or the full shared prompt with its read-tool surface.

| Rows | Legacy scan median / sample max | Projection + shared read/freeze median / sample max | Legacy / shared event bytes | Legacy / shared full-prompt bytes |
|---:|---:|---:|---:|---:|
| 10 | 212.563 / 270.555 ms | 709.656 / 783.823 ms | 3,632 / 3,632 | 6,453 / 6,453 |
| 100 | 223.728 / 289.990 ms | 657.122 / 787.655 ms | 36,302 / 36,302 | 41,284 / 41,284 |

Prompt construction medians were 102.730 / 108.608 ms for 10 rows and 129.936 /
124.301 ms for 100 rows (legacy / shared); with only five samples, sample maxima
are not p95 estimates and the mixed timings show no prompt speedup. The one cold
205-record projection took 466.016 ms. Retained `decision-history` bytes were
43,050; shared immutable record bytes were 115,620; receipt bytes were zero in
both stores, so receipt storage was not measured. This small fixture measures
serialized shape, not large-body/evidence tails or an integrated run. In this
prototype, the extra safe projection/read work is slower than the legacy scan;
it does not justify adding an index. The actual page/receipt prototype is
measured below; neither experiment establishes an integrated prompt budget.

### Bounded shadow-page and matched-receipt prototype

Experiment `48eea5880d056641e98b5631b510fb593fdbf77c2742d861d9b9778b3eed4683`,
command evidence `be2d71b49cf54c9db1e17c7b17332dd7`, used code digest
`b9e99402ff93bfdc12c3a1e363f1b8dd9528d74ef5dec205dc3c5c89dd3fd2cb`. It used
205 short current decisions, a 128 KiB explicit page bound, and five alternating
samples. Legacy and shadow event payload/order matched after removing their
role-bound token. The shared-reader timing covers source reconciliation,
projection, exact shared-receipt checks, page sizing and selected-manifest
freezing under the source lock. Prompt measurements reused the real
`prompt_for(Context)` with each row list substituted; the shadow page remains
unintegrated, so page metadata was not inserted into a runtime prompt.

| Receipt state | Rows | Legacy / shadow page read median (sample max), ms | Legacy / shadow prompt build median, ms | Full-prompt bytes (legacy / shadow) | Shared page bytes |
|---|---:|---:|---:|---:|---:|
| Empty | 10 | 235.236 / 765.139 (264.145 / 1,255.192) | 160.150 / 187.496 | 6,542 / 6,542 | 3,815 |
| Empty | 100 | 208.802 / 1,018.942 (687.498 / 1,325.305) | 131.930 / 146.073 | 42,160 / 42,160 | 37,272 |
| Matched partial (oldest 100 acknowledged in each store) | 10 | 298.058 / 927.528 (332.510 / 1,476.439) | 146.158 / 147.233 | 6,539 / 6,539 | 3,812 |
| Matched partial (oldest 100 acknowledged in each store) | 100 | 271.400 / 982.764 (606.945 / 1,657.235) | 122.360 / 121.360 | 42,159 / 42,159 | 37,271 |

The partial-receipt sets matched by exact oldest event identity; legacy and shared
pending pages remained payload/order-equal. Full prompt bytes stayed equal, while
shared `canonical(page)` bytes include the bounded page envelope metadata. The
cold 205-record projection took 454.196 ms. Stored bytes were 47,932 in
`decision-history`, 117,414 in shared immutable records, 18,800 in 100 legacy
receipts and 21,300 in 100 shared receipts. The five-sample maxima are not p95
estimates. This actual shadow path is roughly 3–5 times slower than the legacy
scan on this small fixture because it performs full reconciliation and safe
record/receipt validation under lock; prompt timings are mixed, with no clear
speedup. These measurements justify no index or speed claim. The tested 128 KiB
page budget is about 3.1 times the largest measured full prompt (42,160 bytes),
but is not a configured default or a worst-case bound: activation must budget
other prompt sections and validate large-body/evidence tails separately.

## Persistence-only decision-source adapter

`Insights.project_decision_records(limit=512)` is an explicit host-only migration/
shadow call, not a runtime hook or model tool. It returns a deterministic mapping
of exact record IDs to recomputed canonical envelope SHA-256 digests, without
payloads. The project-local store scopes `insight.decision/<event UUID>` IDs;
there is no global namespace or caller-selected writer. The trusted closed
`INSIGHT_DECISION_POLICY` authorizes only `host.insights`, keeps the legacy
source-prompt payload cap at 1 MiB, and permits the canonical envelope up to
the generic 1 MiB + 4 KiB framing cap. The adapter constructs
`kind: insight.decision`,
`created_at: <source decided_at>`, and exact `{event, insight, rev}` subjects.
The closed payload is the current legacy prompt payload's `event`, `insight`,
`rev`, `title`, `body`, `action`, `reason`, `revisit`, `decided_at`, `source` and
`evidence`, **without** its role-specific `ack_token`. Evidence JSON is preserved,
not interpreted as delivery authority. Payload `source` is the insight's original
submitting channel, not the authenticated host writer or opaque `origin` label.

Projection and legacy delivery share source-current selection: immutable history
must match the current insight revision and exact effective decision. Revised,
withdrawn and superseded events are not projected. The adapter holds the existing
insight record lock across selection and immutable writes so revise/decide cannot
interleave. It projects all current actions and sources independently of any
legacy receipt, corrupt receipt diagnostic, cursor, role or subscription. Existing
projected history remains immutable when its source later becomes ineligible;
the returned mapping describes only the **current** set, not all retained records.

This is a bounded full reconciliation, not a delivery page: `limit` is an integer
in 1..512 (default 512); eligible-count overflow fails before writing rather than
returning an incomplete reconciliation. It scans history with per-source reads
bounded at 1 MiB. Malformed/unsafe source or store data and conflicting identities
fail closed, with no repair/deletion. A failure during writes may leave a committed
prefix: retry the same call after addressing the failure; exact replay does not
rewrite records. A changed payload under an already projected event ID (including
later evidence on a deferral) is a conflict, never an implicit record update.

The low-level projection adapter alone creates no receipts or delivery authority;
its output proves neither pending-set parity nor a cutover. The implemented host-only
`activate_decision_delivery(project)` operation supplies that explicit switch.
Legacy decision reads and receipts remain authoritative until exact receipt
translation and full pending-set parity complete under source/receipt/config
quiescence and the single-authority marker is published last.

## Implemented pilot and remaining migration plan

The bounded immutable record/receipt mechanism, the `insight.decision` shadow
projection and exact legacy-receipt migration, and the optional host-only
runtime cutover are implemented. The first three stages below apply to
`insight.decision`; they neither activate that kind without its explicit marker
nor activate other source families. Operator answer/notice is a separate,
configuration-gated active source slice (above). Remaining stages describe
future source integrations except for that slice.

1. **Bounded record/receipt mechanism (implemented).** Immutable idempotent records, bounded kind validators, one host-side per-record receipt store, exact current-unit acknowledgement, and failure/unsafe-path validation. A missing kind policy authorizes neither writer nor route nor wake.
2. **`insight.decision` projection and exact receipt migration (implemented).** Stable source event identities are projected without changing insight lifecycle, decisions or archives. Only validated exact per-role receipts are translated; legacy completion cursors never proved handling. Complete subjects/digests, current eligibility and per-role pending sets are checked under the same configuration boundary. Shadow records alone cannot prompt, acknowledge or wake.
3. **`insight.decision` runtime cutover (implemented, explicit opt-in).** A host-only operation quiesces all enabled roles under existing source/receipt locks, applies exact receipt migration, checks complete pending-set parity, and publishes the durable marker last. Before that marker, legacy remains the sole authority; afterwards all active decision consumers select shared delivery. Failures never enable dual delivery or receipt writes; source state remains the sole lifecycle authority.
4. **Future source-specific commit/restart reconciliation.** For each additional kind, define how source commits are projected idempotently across interruption, validate current eligibility before replay, and prove no eligible event is lost. A failed or uncertain projection is not an acknowledgement.
5. **Future work-item delivery, without task migration.** Project exact terminal and structured-wait identities only after a separate explicit cutover design. Revalidate terminal old receipts against authoritative source revision/payload before mapping them to a new digest; consumed insight waits have no exact old receipt to translate and cannot reconstruct historical handling. Keep task CAS/history, holder authority, dependencies and digest-bound `done` unchanged.
6. **Future check-job notifications, not verification authority.** Keep prompt routing to `job.role`, role-or-producer result/log authorization, owner-only acknowledgement and same-role shared-subscription semantics. Do not import every `acknowledged` flag as handling evidence: replaced demand may be withdrawn without a result read. Notification acknowledgement never imports a verification pass; only explicit `action=result` with matching source/commands/environment can do so. Required deliveries and referenced result evidence must survive eviction; cancellation and job retention remain job-owned.
7. **Operator answers/notices implemented; future review/publication kinds only when a trusted source exists.** The configured `answer` and `operator.notice` kinds use the authenticated host CLI; answers bind exact questions and notices may carry an optional exact subject. Both use structured source/addressee routing, explicit wake policy and permanent retention. Do not synthesize answers, verdicts or blockers from free text. Any future kind must declare its authenticated writer, exact subject, current routing/eligibility, wake and retention before enablement. Keep authoritative histories, publication receipts, run evidence and task verification; do not rewrite them as delivery state.

At each cutover, test that no duplicate delivery occurs, omitted records remain pending oldest-first, explicit partial acknowledgements affect only selected IDs, failed/uncertain units do not acknowledge, records cannot acknowledge across roles or revisions, and a record read/wake cannot mutate source state. After every code slice, verify and publish the exact snapshot; `WorkItems` completion remains bound to its matching published tree and passing digest-bound verification.

## Resolved CI policy

Operator answer `609e9479aaca426ca196f5dd81360e16` resolves the earlier question: current red `ci_branch` writer wake is intentional. The default CI-status kind therefore has an explicit waking route to the writer, preserving current branch-selection and due/deduplication policy. The cache is a producer of host observations, not a special source of authority outside kind policy. A structured terminal CI-wait result separately routes only to the matching current blocked holder with its exact receipt/wait binding. Neither payload content nor cache existence implicitly wakes, assigns work or mutates task state; waking is the configured typed route. This design records the resolved policy without changing runtime behavior.

## Source anchors

Current behavior can be checked in these functions (`insight.decision` uses its explicit host marker; answers/notices use their separate configured slice; all remaining source families retain existing routes):

- [`SharedRecords`](../src/mizu/shared_records.py): bounded canonical record envelopes, exact role/record/digest receipts and manifest-scoped paged reads; it does not own source eligibility, routing, wake, backfill or cutover. [`Insights.shared_decision_page`](../src/mizu/insights.py) builds shadow or active pages, selected by the host's durable authority route.
- [`operator_records`](../src/mizu/operator_records.py): host-only answer/notice writes, exact-question routing, notice closure/supersession, pending projections, wake hints and manifest-scoped acknowledgements.
- [`Insights.decision_events`, `acknowledge_decisions`, `due_waits`, `consume_waits`, `pending_obligations`](../src/mizu/insights.py): current decision/revision eligibility, exact receipts and consumable waits.
- [`decision_delivery.pending`, `bind`, `read`, `acknowledge`](../src/mizu/decision_delivery.py) branch by the durable marker, carry host-only unit manifests and never fall back from shared failures. [`selection.work_inputs`](../src/mizu/selection.py) uses the same authority as runtime prompt/wake/ack, so legacy receipts cannot create a second active selection view.
- [`work_item_events.pending`, `acknowledge`](../src/mizu/work_item_events.py): terminal projections and source-validated old receipts; [`ci_waits.pending`, `_pending`, `acknowledge`](../src/mizu/ci_waits.py): current holder/revision checks and blocked `updated_at` order.
- [`check_jobs.pending`, `result`, `logs`, `acknowledge`, `_withdraw_locked`, `_make_room`](../src/mizu/check_jobs.py): prompt role, read versus acknowledgement authority, withdrawn demand and eviction.
- [`runtime.prompt_for`, `ci_failures_projection`, `wake_ci_failures`, `_ci_due_for`](../src/mizu/runtime.py): prompt construction and explicit CI wake selection/due policy.


## Decision page and manifest-scoped retrieval

These are trusted host Python APIs; calling them alone does not activate runtime
delivery. They write immutable projections, never receipts. The explicit host
activation below is the only runtime switch. Before activation, legacy remains
the sole active delivery/wake/ack authority; afterwards the same page builder
serves only shared receipts through `decision_delivery`, with a narrow
manifest-scoped runtime retrieval tool and explicit finish acknowledgement.
Operator answer/notice use the separate configured projection described above; all other source lifecycles remain unchanged.

```python
page, manifest = insights.shared_decision_page(
    role_name, actions, max_bytes=explicit_budget, limit=100, sources="own")
part = records.read_page(role_name, page["events"][0]["ack_token"],
                         manifest=manifest, offset=0, limit=8192)
```

`actions` is an explicit collection (`list`, `tuple`, `set` or `frozenset`) of
`accept`, `modify`, `reject`, `defer`; empty means no selected actions. `sources`
is `own` or `any`; role and routing arguments come from trusted host policy,
not payload text. `limit` is an integer in 1..100 (default 10).
`max_bytes` is **required**, an integer in 1..`fs.MAX_FRAME` (1 MiB), with no
policy default. An empty `actions` collection returns the bounded empty page without
projecting or writing records. It bounds `len(fs.canonical(page))`, including UTF-8 encoding,
JSON escapes, the final newline, event framing, tokens, references and metadata.
The manifest tuple is separate host-only state, not serialized into the page.
Active runtime passes `prompt=True` to bound the actual UTF-8 serialized
`decision_events`/`decision_page` prompt fields instead, including JSON formatting
and both field names. The operator must reserve space for all other sections;
this API alone does not promise a full prompt fits a protocol frame.

The page is exactly:

```text
{events: [row, ...], pending_count: integer, truncated: boolean,
 count_truncated: boolean, byte_truncated: boolean}
```

`pending_count` counts all source-current, routed, not-exactly-shared-received
records before page selection. `truncated` means fewer rows are represented
than pending; `count_truncated` means pending exceeds `limit`;
`byte_truncated` means fewer than `min(pending_count, limit)` rows fit the byte
budget. A reference represents one row; replacing an oversized full row with a
reference does not itself count as an omitted row. There is no timestamp cursor.

Selection, projection, shared receipt checks and page construction hold the
existing insight record lock together. The projector's existing ceiling of 512
current source records applies before routing/receipts; overflow refuses before
any projection writes. The public projector still returns only ID-to-digest
bindings. Exact current decision and source revision/action must match; retained
stale or withdrawn projections cannot become new page rows. Shared exact receipts
are filtered before oldest-first selection, independently of legacy receipts.
Timestamps are normalized to UTC and ties use the stable record ID. There is no
index: this remains a bounded, fully validated scan.

A full `row` preserves every legacy decision-event payload field, replacing only
`ack_token` with the shared manifest token. If that row cannot fit in the
remaining page, it is replaced, if possible, by:

```text
{event, insight, rev, action, preview, ack_token,
 ref: {record_id, record_digest, bytes}}
```

`preview` is at most 80 title characters, never authority or the full content.
`ref.bytes` is the exact canonical envelope's UTF-8 length including its final
newline; the digest pins that full envelope, not the preview. Only represented
rows are frozen into the returned `tuple[Delivery, ...]`, in page order. Each
entry binds role, record ID, full-record digest and its token. Token bytes are
reserved at their exact fixed width while sizing the page. No discarded row is
added to the manifest. If the next row/reference cannot fit, a nonempty page
ends with explicit truncation, without advancing past that row. If even metadata
or the first row's reference cannot fit an empty page, `LimitExceeded` is raised,
not an empty successful page. Retry with a larger explicit budget (within the
ceiling); never silently skip the oldest row.

`SharedRecords.read_page(role, ack_token, manifest=..., offset=0,
limit=READ_PAGE_CHARS)` accepts no arbitrary record ID. The host must supply the
current role's retained current-unit manifest, never one supplied by the model.
Tokens are identity checks, not secrets or authorization outside that manifest.
Every call validates manifest role/token/digest bindings and safely rereads and
validates the entire canonical envelope against its exact digest. Source or
routing movement after the freeze does not prevent retrieval of that immutable
record during the same unit. The host discards the manifest at unit end.

Retrieval returns exactly `{record_id, record_digest, offset, total_chars,
next_offset, text}`. `text` is a slice of canonical envelope text, including the
final newline on its last page. Offset/limit are integer Unicode character
counts, not UTF-8 byte offsets; booleans, negative/past-end offsets and limits
outside 1..`fs.READ_PAGE_CHARS` (8192) are refused. At exactly the total length an
empty terminal page is allowed; `next_offset` is `null` at the end. This avoids
splitting UTF-8 characters. Concatenate text pages and UTF-8 encode to reproduce
the digest and `ref.bytes`. Each returned slice has at most 8192 characters,
even when JSON-escaped serialization takes more bytes.

Malformed arguments, cross-role/unmanifested selections, duplicate or invalid
manifest entries, missing/stale-digest records, corrupt/noncanonical records or
unsafe store paths fail closed on retrieval. Corrupt receipts fail shared page
selection closed; retrieval does not consult receipts. No automatic repair occurs.
Page generation may idempotently project current source records and can leave a
committed prefix on failure; it never writes receipts. A failed page returns no
usable page/manifest and exact replay is safe after the failure is addressed.
`read_page` itself writes nothing; repeated pages without receipts replay the
same selection while sources/policy remain unchanged. No cancellation or
background job is introduced; I/O failures propagate to the host caller.

The measured baseline remains experiment
`83aee77506a2ccd56eb73538673e15ba7873236239cfee7f6f88b37c99acd13c`,
command evidence `104c57b71b924d9b8cc4ba7bbf91586c`: 100 short rows produced a
41,284-byte full prompt, and full projector plus safe shared reads were slower
than legacy. This shadow adapter claims boundedness and parity, **not** speed or
storage savings. Further storage/index complexity and a policy budget default
still require the integrated pilot evidence.

## Shadow-only exact legacy receipt reconciliation

`mizu.decision_reconciliation` provides trusted host Python functions, **not
model tools**, with no runtime caller or default route:

```python
plan = preflight_decision_receipts(insights, routes, enabled_roles=enabled_roles)
report = apply_decision_receipts(insights, plan, routes, enabled_roles=enabled_roles)
```

Both calls require a fresh trusted configuration snapshot. `enabled_roles` is a
unique list/tuple of valid role identifiers; `routes` is exactly a dictionary
covering those roles, including roles with no decision subscription. Each value
is exactly `{"actions": [...], "sources": "own" | "any"}`. Actions are a unique
list/tuple subset of `accept`, `modify`, `reject`, `defer`; `[]` selects nothing.
There is no inferred role, route, subscription, config path or default. `{}` is
valid only with an explicitly empty enabled-role set. The caller is responsible
for supplying the complete enabled set from trusted configuration, not model
arguments. Routes are detached and normalized by sorting roles and actions;
order-only differences do not invalidate the plan. Changed actions, sources or
role membership do. This snapshot is not a lock on an external config file:
apply must be called with the trusted configuration again, never routes copied
out of the plan. A trusted host that edits receipt stores directly must also
respect the boundary; these APIs cannot synchronize an out-of-band operator
file edit or a caller that supplies an obsolete config snapshot.

### Boundary and lock order

Each call holds, together and in this order:

1. Every enabled role's existing `admission.role_exclusion(project.root, role)`,
   sorted by role, **nonblocking**. Any live run (even an empty-subscription role)
   causes `Busy`, with no new projections or shared receipts.
2. The existing insight `record_lock` (including its managed shared-retention
   lock), excluding current source mutation.
3. Each enabled role's receipt-writer lock, named
   `locks/decision-receipts-<sha256(role)>.lock`, sorted by role. The exact
   legacy acknowledgement algorithm holds only its own role's lock, so
   independent role receipts do not serialize one another.

All remain held through scan, comparison, projection writes, receipt writes and
post-apply verification. Runtime already holds its own role exclusion through
finish/ack. Direct legacy ack acquires only that role's receipt lock: it never
acquires a source or role lock while holding it. Source mutations take
`record_lock` and never acquire role exclusions or receipt locks in reverse
order. Concurrent
reconciliation attempts serialize or fail `Busy`; no caller waits for an active
run while holding the source lock. Locks are released on exception/death and
are never unlinked. Preflight can create lock infrastructure but creates **no
immutable projections or receipts**. Legacy delivery and exact acknowledgement
remain the only active runtime authority for decisions.

### Exact schema and identity

The returned JSON object has exactly the following schema (decision maps below
use full shared record IDs `insight.decision/<event>`; pending maps use
`insight.pending/<event>`; lists are sorted by ID, not event time):

```text
{
  schema: 1,
  plan_digest: sha256(canonical({schema: 1, boundary: boundary})),
  boundary: {
    routes: {role: {actions: [action, ...], sources: "own" | "any"}},
    route_digest: sha256(canonical(routes)),
    sources: {record_id: {
      record_digest, history_digest, insight_digest, decision_digest
    }},
    legacy_receipts: {role: {record_id: {ack_token, receipt_digest}}},
    pending_sources: {record_id: {
      record_digest, inbox_digest, decision_digest, target
    }},
    pending_legacy_receipts: {role: {record_id: {ack_token, receipt_digest}}}
  },
  roles: {role: {
    current_ids: [record_id, ...],
    legacy_received_ids: [record_id, ...],
    legacy_pending_ids: [record_id, ...],
    shared_received_ids: [record_id, ...],
    shared_pending_ids: [record_id, ...],
    shared_receipt_digests: {record_id: digest},
    post_map_pending_ids: [record_id, ...],
    post_map_parity: true,
    pending_insights: {
      current_ids: [record_id, ...],
      legacy_received_ids: [record_id, ...],
      legacy_pending_ids: [record_id, ...],
      shared_received_ids: [record_id, ...],
      shared_pending_ids: [record_id, ...],
      shared_receipt_digests: {record_id: digest},
      post_map_pending_ids: [record_id, ...],
      post_map_parity: true
    }
  }}
}
```

Digests are SHA-256 hex strings using `fs.canonical` (including its newline) for
structured values. `record_digest` pins the entire projected envelope; history
and effective decision digests pin exact safely read file bytes. The insight
digest pins exact live inbox bytes, or canonical retained archive content when
there is no live inbox record. Thus non-payload source movement also invalidates
the boundary. `sources` contains the complete strict source-current decision
set, before routes, bounded at **512**. `legacy_receipts` contains every routed
role/record pair, recomputed with `Insights._decision_token`; `receipt_digest`
is the exact receipt-file byte digest, or **null for absence**. Only receipts
validated by `_decision_received` count: exact role, event, full payload token
and timezone-aware acknowledgement timestamp. Cursors are never consulted.
Stale, withdrawn, superseded, old-payload-token and route-excluded receipt files
are never translated or scanned for acknowledgement. Unrelated stale corrupt
receipt files remain irrelevant. Relevant corrupt or unsafe files fail closed,
without repair or deletion; receipt-presence and timestamp changes invalidate
apply, even when the received set would be unchanged.
Addressed pending attention uses the same token/receipt mechanism with a
distinct pending-insight token domain (`Insights._pending_insight_envelopes_locked`
for each explicit enabled role, `INSIGHT_PENDING_POLICY` alongside
`INSIGHT_DECISION_POLICY`). `pending_sources` binds each current
`insight.pending/<event>` record digest, route target, exact inbox byte digest
and decision-pointer byte digest (or explicit null absence), bounded at
**512** current pending projections; `pending_legacy_receipts` binds every
addressed role/record token and receipt digest (null for absence). Only exact
current events already acknowledged in legacy map; unacknowledged attention
never gains a shared receipt, and legacy receipts are never modified.

`roles` is the deterministic observation/progress report. Existing shared
receipts must be a valid exact subset of legacy-received identities, separately
for decisions and for the nested `pending_insights` report. Anything
else refuses preflight/apply, because it would hide a legacy-pending event.
Every predicted post-map pending set equals its legacy pending set, for both
decisions and pending attention. The plan also refuses a per-role combined
shared-page projection where decision records plus still-unacknowledged
addressed attention exceed the existing 512-row ceiling. The
`plan_digest` deliberately excludes output progress: a new preflight after an
interrupted apply has the **same identity** while its shared-received report
can advance. The plan is host-retained evidence, not a model authorization
credential. Apply validates report consistency and recomputes the entire input
boundary from trusted routes and stores; it never uses a plan payload to create
a record or choose recipients. Decision report fields are unchanged; pending
parity lives only in the nested `pending_insights` object.

### Apply, failure and restart

Before any writes, apply checks the identical source/legacy-receipt/route
boundary and all bounds. The full serialized plan/report, including its
post-map form, must fit `fs.MAX_FRAME` (1 MiB); this is a refusal ceiling, **not a
page budget default**. Count or byte overflow refuses without shared receipt
writes. This API adds no index, scan cache, page default or storage optimization.
The prior 205-event shadow scan evidence (3–5x slower reads) does not justify
additional storage complexity here.

Apply creates/validates projections only for currently routed (or addressed),
legacy-acknowledged records, then uses `SharedRecords.freeze` and explicit exact
`acknowledge` selections for missing role/ID/full-envelope-digest receipts, first
for decisions then for addressed pending attention. It
never rewrites/deletes legacy receipts, maps a completion cursor, or invents a
receipt for a legacy-pending event. Existing projections are validated in
preflight without writes; an orphan shared receipt with no immutable record
refuses. Valid existing shared receipts are reused unchanged. All report size
bounds, including the post-map form, are checked before any writes.

I/O interruption can leave an idempotent projection/receipt prefix. Retain the
original plan across restart and call apply again with current trusted routes.
Any newly present exact shared receipts must be a subset of that plan's
legacy-received identities. Original shared receipt byte digests must still be
present and unchanged; corrupt, extra, missing-original or changed receipts
refuse. This permits exact interrupted retries without putting output progress
in input identity. Successful apply re-scans under the same locks, verifies each
role's shared and legacy pending sets are equal for decisions and for nested
pending attention, and returns the same schema
with the completed shared progress report. A failed call is not proof of a
completed batch, and failure never rolls back an immutable prefix.

The two receipt-reconciliation calls alone never activate runtime delivery.
They now refuse after activation; they cannot re-import legacy receipts into an
active shared store. Task lifecycle, WorkItems, waits and CI remain unchanged.


## Host-only one-way runtime activation

Call `mizu.decision_reconciliation.activate_decision_delivery(project)` from a
trusted host with a freshly loaded `Project`/configuration snapshot. This API
is not installed deployment modification, a CLI/model tool or an automatic
config-driven switch. It derives **all enabled roles** and existing
`decision_events`/`decision_sources` routes directly from that trusted project,
including empty subscriptions, and requires an explicit `decision_page_bytes`
for every subscriber (see [configuration](configuration.md#shared-decision-delivery-explicit-host-cutover)).
As with reconciliation, the host must serialize external config replacement;
these locks cannot synchronize a host editing files behind the API.

Activation holds the exact boundary above (all role exclusions, source record
lock, all role receipt-writer locks) continuously through preflight, exact
apply, complete per-role pending-set parity for decisions and pending attention,
and final marker publication.
It does not compare only the first dispatch page. Every existing shared decision
and pending-attention receipt must be explained by an exact routed/addressed
legacy receipt; extra receipts for
stale, route-excluded, removed or disabled recipients also refuse activation. Inventory
reads use the shared store's safe directory and bounded receipt readers and
reject unexplained `insight.pending/` receipts just as for `insight.decision/`.
Recognized `.new-<8 lowercase/digit/underscore characters>` atomic-write residue
is safely read but not interpreted as a committed receipt. Other kinds are not
imported or made authoritative. Legacy source records and receipts are never
rewritten, deleted or used as timestamp watermarks.

Durable project-root evidence, included in backup/restore:

- `decision-activation-plan.json`: create-only schema-1 exact reconciliation
  plan, at most `MAX_FRAME`. Written before mapping. Retained original input
  identity survives restart; retries use this plan, not a silently fresh one.
- `decision-activation-report.json`: completed schema-1 reconciliation report,
  at most `MAX_FRAME`, with actual whole-set parity. Written after successful
  apply; a pre-marker exact retry validates/reuses the same bytes without
  rewriting the report. Corrupt or mismatched prior report bytes refuse.
- `decision-authority.json`: canonical create-only marker, at most 4096 bytes,
  `{schema: 1, kind: "insight.decision", authority: "shared", plan_digest,
  report_digest}`. `report_digest` pins the entire canonical completed report;
  its `plan_digest` must match the marker. Published atomically and durably
  **last**, while all the same exclusions/locks remain held.

Before the marker exists, decision delivery remains legacy-only, even with an interrupted
projection/receipt prefix. Exact retry validates the original source/receipt/
route boundary and shared subset and completes missing writes. A source,
legacy receipt, route or enabled-role change during that interruption refuses
retry; an operator must explicitly investigate/reconcile it, not discard the
plan to force a switch. Crash after marker publication is a completed switch:
retry returns the validated marker without reimporting any legacy receipt.
Corrupt/unsafe marker or report, mismatched report digest, unexplained shared
receipt, or uncertain parity fails closed. No implicit reverse migration exists;
never delete the marker or restore only legacy files to switch back. Reverse
migration requires its own explicit reconciliation protocol. Out-of-band
host-owned file edits are outside the synchronization contract.

`decision_delivery.pending` is the single branch used by runtime dispatch,
substantive-event wake (including the deferral-window probe) and selection.
Shared pages reconcile source-current eligibility and trusted routing before
reading only shared receipts; they do not consult legacy receipts. No shared
failure can fall back. In active mode `shared_decision_page(prompt=True)` counts
the actual full/delta prompt decision fields and JSON framing against the
explicit byte budget; the host-only shadow call's default still measures
`canonical(page)`. Count/byte overflow is explicit metadata, and an unfit oldest
reference is an explicit error, not an empty successful page.

A bounded full-prompt measurement used the real `prompt_for()` builder with a
synthetic reviewer, short ASCII decisions, a 100-row count limit and a 131072-byte
page budget (experiment `722f0b222ed2c9039ff96fd4fbcc2375e08420dc07330491a910cdfc5f6df92d`,
command evidence `3c839faf85f645bdb2c36e6031f1490d`, code digest
`6412e8c0eb4f68df34768d8a6aceac9fd0bc62154f6ab9aa99a5602e2bfcee45`):

| Current decisions | Legacy/shared rows | Shared page contribution | Legacy full prompt | Shared full prompt |
|---:|---:|---:|---:|---:|
| 10 | 10 / 10 | 3,812 B | 6,263 B | 6,602 B |
| 100 | 100 / 100 | 37,113 B | 39,564 B | 39,904 B |
| 205 | 100 / 100 | 37,111 B | 39,564 B | 39,902 B |

The 205-record case reports all 205 pending while the configured count cap keeps
100 rows. The full-prompt figures include the prompt's other sections and the
shared read-tool surface in this fixture, but the non-decision sources are
minimal. These are observed construction sizes, not a worst-case prompt bound,
token estimate, latency result or recommended budget; operators must reserve
headroom for their actual prompt inputs.

The host retains the immutable `{role, record_id, record_digest, ack_token}`
manifest only for this unit, recording it with the page in the run's
`decision-delivery.json`. Prompts carry rows and overflow metadata, not a
model-supplied authorization manifest. The narrow `decision_read` tool retrieves
only identities in this host manifest. Finish validates explicit token selections
against it **before** seal intent; after successful result/publication, only the
selected shared receipts are written under the role receipt-writer lock.
Omitted/partial selections leave all other deliveries pending. Cross-role or
undelivered tokens are refused. Source revisions, withdrawal and route removal
do not invalidate handling of an already frozen delivery; future reads/wakes
still revalidate current eligibility. Shared errors remain loud, and replay after
an interrupted receipt write is idempotent. Legacy acknowledgement writers
refuse once the marker is present.


### Gated CI-wait source cutover

The runtime now includes the separate CI-wait cutover behind host configuration
`[vcs] ci_wait_shared_records = false`. Code presence does not activate it; no
example enables it. This gate supplies only the `ci.wait` kind/holder route,
not a general model-writable SharedRecords interface. The preceding shadow
preflight remains evidence of the design, not an activation receipt.

The enabled route uses immutable `ci-wait:<item>:<blocked-rev>` identities in the
existing shared store, writer `host.ci`, subject `{work_item, rev, holder}` and
blocked-revision `updated_at` event time. The payload is exactly the validated
external-untrusted terminal source event including its evidence digest. Only
current eligible sources without an exact explicit generic receipt enter the
oldest-first bounded page (ten rows or the existing generic 1 MiB frame measured
against the actual serialized `wait_events` prompt contribution). Full payloads
are delivered; the per-source 128 KiB evidence limit is preserved and each shared
envelope uses the existing generic record bound, so a single valid row fits an
empty page. If the next oldest row exceeds remaining page space, the page ends
there; after the delivered prefix is receipted, replay starts at that row rather
than skipping ahead.

A host-frozen manifest grants only exact current-holder token selections from
`finish.ack_events`. Selection is checked before intent and again after
successful completion under the WorkItems lifecycle lock. Current full history,
blocked revision/holder, exact Publication receipt/wait digest, declared unique
check set and terminal evidence remain domain authority, unlike the shared
store's source-independent generic receipt operation. Stale selected sources
write no CI receipt. Omitted/empty/partial selections never handle other rows;
bounded/bound-hit units and finishes waiting for another check write no CI receipt.
Legacy auto-ack files are not mapped. The shared store owns the only new
receipts, and existing backup/restore includes both generic directories. See
[the full source contract](scheduling.md#exact-sha-work-item-ci-waits) for
failure/retry, diagnostics and unchanged polling/admission semantics.


### Configured insight attention

Runtime insight attention uses the existing decision delivery boundary, not a
new activation tool or source authority. An undecided current revision with an
explicit stored addressee projects `insight.pending` to the configured project
role (`writer` resolves to its source). Human `operator` attention stays in the
host open-insight inbox. Legacy omissions do not imply a target.

In an already-active shared project, `decision_events` may contain
`kind=insight.pending` rows alongside decision events; `insight_kind` carries the
stored proposal/question metadata. The same bounded page, exact role/digest
manifest, `decision_read` reference reader and explicit `finish.ack_events`
application boundary are used. A frozen old manifest can acknowledge only its
old payload, never a replacement revision/target. When the decision authority
marker is absent, the existing legacy exact per-token receipt path is used
instead (combined legacy prompt contribution bounded to 1 MiB by omitting only
newest attention rows with visible truncation; decisions never omitted for
bytes); no shared shadow delivery or automatic activation is added. Removed or
unconfigured destinations are isolated without blocking valid routes; malformed
sources still fail closed.
The cutover reconciliation now includes pending-attention receipts: exact
addressed `insight.pending` envelopes, route targets, inbox and decision-pointer
digests and legacy tokens are pinned in the plan boundary, mapped only from
legacy-acknowledged attention, and verified for post-apply parity alongside
decisions, without dual-read, auto-ack or activation-policy change. See the insight routing contract in `architecture.md` for bounds,
coarse inbox wake hints, unresolved destinations and platform limits.
