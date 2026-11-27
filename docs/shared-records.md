# Shared records: CI waits and host publisher outcomes

`SharedRecords` is a bounded immutable record store with exact per-role receipts.
Its live uses are receipt-pinned CI-wait delivery and declared host publisher
outcomes. Role questions (with answers in the same threads), notices,
proposals and work are declared queues
([kernel.md](kernel.md)); decisions, work items and waits are role policy and
work-item records. There is no delivery-activation marker, fallback delivery route,
dual-write path or runtime cutover for those families.

## Declared host publisher outcomes

`record_kinds` defaults to empty (no authority). A declaration names an
authenticated host writer and this bounded, data-only schema, with no executable
configuration or callbacks:

```toml
[record_kinds."publisher.outcome"]
writer = "publisher.cli"
route = "publication_wait"
wake = true
retention = "permanent"
acknowledgement = "explicit"
subject = "snapshot"
maximum = 8192
[record_kinds."publisher.outcome".variants.refused.reason]
type = "text"
maximum = 1024
[record_kinds."publisher.outcome".variants.superseded.replacement]
type = "newer_snapshot"
```

The declaration names the kind, authenticated writer, supported route, wake
policy, retention, acknowledgement and exact subject format. Schema-v1 allows at
most 16 kind/variant/field entries per mapping, exact keys and no optional payload
fields. Kind/field/tag names follow SharedRecords name grammar; the resulting
record ID also fits its 256-character bound. For publisher outcomes only the implemented publisher
principal and publication-wait route are supported, with one declared kind for
that route (never a search among competing authorities). Maximum envelope bytes
and nonblank text character bounds are explicit, positive integers at most
`PREVIEW_BYTES` (128 KiB). This source-record ceiling keeps the projected outcome
comfortably within the existing 1 MiB prompt frame. `newer_snapshot` is an exact
`{snapshot_id, code_digest}` reference, like the subject, not a head or prefix.
It proves strict descent from the subject using immutable parent links, bounded
by the configured `limits.history_index`. An absent or incomplete reference
refuses the record write; it is never proof of supersession. Only the exact
`refused`/`reason` and `superseded`/`replacement` variants shown above are
accepted for this route; unsupported or incomplete variants fail configuration
validation rather than recording an outcome the waiter cannot consume.

The trusted publisher adapter calls the host CLI, for example:

```sh
mizu publisher-record PROJECT --kind publisher.outcome \
  --subject '{"snapshot_id":"<candidate ID>","code_digest":"<candidate digest>"}' \
  --payload '{"outcome":"refused","reason":"Approval refused"}'
```

For supersession the payload is
`{"outcome":"superseded","replacement":{"snapshot_id":"<newer ID>","code_digest":"<newer digest>"}}`.
Host configuration/data access is the authentication boundary, as for the existing
operator CLI. The command fixes `publisher.cli`: no writer/timestamp parameter,
and no model role can use this publisher route. This is an endpoint for the
external publisher's authoritative observation, not a new observer; the adapter
must adopt it. The harness neither infers outcomes nor changes publication,
promotion or CI dispatch.

Records use the ordinary SharedRecords envelope and canonical create-only ID
`host/<declared kind>/<candidate snapshot ID>`. New writes hold publication.lock
and validate each exact retained snapshot and whole-capture digest. The newer
replacement may be a retained unpublished candidate; it must descend from the
subject through exact immutable parent links. Current rolling-history membership
is not rechecked, so an existing blocked wait can still receive an outcome after
its candidate leaves the rolling index. Same-content retries reuse the original
timestamp; a conflicting terminal report is refused, not overwritten. Exact
retries and reads revalidate retained immutable identities.
Directory-sync failures can be repaired by exact replay. Source records and their
candidate/replacement manifests are permanently retained by the existing storage
audit; unknown/corrupt source records prevent reclamation rather than hide roots.
There is no new index, cursor, source store or cancellation/deletion operation.

A `publication_ci` waiter looks up only its exact candidate ID and digest. The
existing wait delivery binds the source record ID/digest to the current item,
revision, holder and wait digest in its existing `ci.wait` envelope. Delivery is
`kind: publication_outcome`, `trust: host-record`, with the bounded `outcome`
payload and source identity; positive receipt-bound CI delivery is unchanged.
The source is revalidated before delivery and explicit acknowledgement. Missing,
unknown, malformed, mismatched, oversized or conflicting evidence remains
pending, including a source outcome conflicting with retained terminal CI
evidence. There is no inference from silence, elapsed time, another snapshot or
branch head. When the declaration enables wake, writes touch the existing
`waits` platform-watch hint; admission rechecks exact eligibility. Only the
current holder wakes. Unselected receipts replay; `finish.ack_events` acknowledges
only the exact delivered token, never a work-item transition or completion.
`wake = false` suppresses outcome delivery and the platform hint without deleting
the permanent source record.

## Scope and invariants

Use one small, append-only record mechanism for delivery, wake hints, and per-recipient acknowledgements. It does **not** replace any source's lifecycle or authority:

- `WorkItems` owns task seeds, CAS transitions, holder, waits, history and completion. A task marked `done` still requires passing verification bound to the exact published snapshot; `code_digest` remains the whole-capture digest.
- CI adapters, check jobs, review inputs, and publication receipts keep their existing evidence rules. Generic delivery cannot approve a publication, prove external-actor provenance, assign work, transition a task, or turn a diagnostic into a task/dependency.
- A delivered record is immutable and has a stable identity namespaced by project and source event identity, not its prose or a timestamp. A source retry with the same identity and same content is idempotent; a conflicting reuse fails closed. Record payloads are bounded and kind-validated.
- A record is routed only by current trusted kind policy over authenticated writer and structured subject/source fields, evaluated separately at delivery and wake time. The writer cannot choose arbitrary recipients; free text is never parsed to infer work or an assignment.
- Each recipient acknowledges exact record IDs and digests explicitly. The host freezes an exact `{role, record_id, record_digest}` delivery manifest for the current unit and accepts only explicit selections from it. A successful unit with no explicit selection acknowledges nothing; a partial selection acknowledges only those records. Receipts are role/record/payload-bound, create-only and replay-safe. No timestamp watermark or completion-as-ack.
- Delivery order is oldest-first by the source event's immutable timestamp normalized to UTC, then stable record ID. This is deterministic event-time order, not arrival FIFO: late backfill or clock rollback can insert an older event ahead of retained pending events. Acknowledged rows are filtered before page selection, so a hole or partial acknowledgement cannot hide older pending records. Count and aggregate serialized-byte limits (including envelope, preview/reference, pagination and overflow metadata) apply across all configured kinds, with explicit truncation/overflow; independent per-kind page sizes must not add up to an unbounded prompt.
- Wake is a separate, declarative policy field. Record existence, reading, diagnostic content, and task readiness do not imply admission. Only explicitly configured, typed event kinds may signal a wake; that signal does not itself create work or mutate source state. No records means no new route or wake authority.

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

The record digest is `sha256(canonical(record))`; it is recomputed, not self-asserted. A separate receipt is `{role, record_id, record_digest, acknowledged_at}`. The authenticated `writer` is the host principal authorized to project the source event, not a payload-claimed author. The subject above is illustrative, not a universal three-field schema: each kind binds its exact work-item wait/revision/holder and wait digest, snapshot/whole-capture digest, or publication receipt/branch/SHA/check set as applicable. Project/source namespaces prevent cross-project or cross-source identity collisions.

Kind policy declares a closed payload validator/size bound, authorized writer, structured route, role subscription/filter, whether a wake is permitted, the source-current eligibility predicate, and retention. These are declarations, not executable expressions. Resolved recipient sets are not immutable record fields (typed addressee/source fields may be): current trusted policy resolves delivery and wake independently. New subscribers see retained events that are still source-eligible; removed recipients receive no new deliveries or wakes. Remove/re-add does not erase a role's exact prior receipts. Policy changes cannot rewrite an already-frozen delivery manifest or let one role acknowledge another's delivery.

Source-current eligibility is distinct from retention and acknowledgement. A retained obsolete CI result whose wait revision/holder moved must not be newly delivered or woken, even without a receipt. Revalidate eligibility and current routing at delivery/wake; neither retention nor absence of a receipt proves eligibility. CI waits retain their current-revision refusal: an ack records handling of that delivery only and never mutates or revives the source. Retention keeps evidence, not delivery authority.

Keep each kind's existing event-time meaning, including the blocked revision's `updated_at` for a CI waiter, rather than replacing it with projection/arrival time. Filter source-ineligible and exactly acknowledged rows before bounded oldest-first selection. If the oldest eligible payload exceeds a page, deliver a bounded digest-pinned preview/reference with paged retrieval of the exact immutable content; the delivery manifest still binds the full record digest. Retrieval must validate that identity across pages. Do not silently skip the row or stall forever behind it. The byte budget must accommodate the bounded reference and metadata; otherwise report an explicit configuration/budget failure, not an empty successful page.

## Current source inventory

| Family | Authority and authenticated writer | Delivery and acknowledgement | Bounds and retention |
|---|---|---|---|
| Receipt-pinned CI waits | `ci_waits` owns the current blocked work item, immutable wait evidence and receipt validation; `host.ci` writes `ci.wait` records. | Only the matching current holder receives a due terminal result. The host freezes a role/record/digest manifest; `finish.ack_events` selects exact tokens after a durable successful result. Waking never transitions the item. | Wait evidence is create-only and capped at 128 KiB; pages are bounded to ten rows (API maximum 100) or the 1 MiB frame. Work-item history and shared records are retained permanently. |
| Declared host publisher outcomes | `host_records` validates declared `record_kinds` variants; `publisher.cli` is the only authorized writer. | Envelope identity binds the exact snapshot and outcome; no model grant or role route is involved. | Declarations require permanent retention; records are create-only and bounded by the kind declaration. |
| Exact acknowledgement receipts | The host store holds `{role, record_id, record_digest, acknowledged_at}` receipts. | Acknowledgement is explicit, role-bound and replay-safe; reading a record never acknowledges it. | Receipt files are bounded to 2 KiB and retained with the records they acknowledge. |

### Runtime delivery settlement

Actual scheduled executions receiving SharedRecords-backed deliveries register a
host-only `delivery-settlements/RUN_ID.json` before model dispatch. The record
binds the actual 32-hex run ID, role, and CI-wait manifests. Each binding retains the role,
record ID, whole-record digest and acknowledgement token. Empty deliveries,
readiness checks, previews and non-dispatched selector waits create no pins.
The generic `freeze()` operation itself remains an in-memory operation, not a
registration API for arbitrary host consumers.

The canonical envelope is exactly `{record, digest}`, where `digest` is SHA-256
of the canonical `record`. Record schema 1 has exactly `schema`, `run`, `role`,
`manifests`, `selected`, `effects`, `closed` and `finalized`. The single family map
uses exactly `ci`. `selected` is initially
null (no receipt-finalization selection boundary), then lists the exact selected
tokens per family. Each delivered token initially has an `unresolved` effect.
Live finalization explicitly records `not-selected` for unselected tokens, and
`check-join` or `bounded` for selected CI tokens whose existing completion
semantics require no receipt. These are skipped effects, not acknowledgements.
A required effect becomes `succeeded` only after its existing source-specific
receipt operation returns normally. A partial batch or suppressed exception
leaves unresolved effects; recovery never bypasses current-source validation or
infers success by finding a receipt from another execution.

`finalized` records completion of the host finalization path. Separately,
`closed` records Context authority revocation under its mutex, idle joined/tool
operations and successful exec cleanup. Both must be durable and every effect
resolved before removing a pin. A result status, finish-intent, publication,
acknowledgement omission, timeout or absent active pointer/process proves neither
boundary. A fault before selection leaves all manifest effects unresolved; run
and finish evidence remain available without manufacturing a selection from them.
Interrupted/unrecoverable records intentionally remain pinned without expiry or
automatic repair, even when a later run handles the same delivery.

Each canonical envelope uses the existing 1 MiB frame bound; each family retains
the existing 512-entry manifest bound. Registration fails before dispatch rather
than truncating. Maintenance shares its existing aggregate run-reference scan
budgets with this inventory. Corrupt, unsafe, unknown or over-budget registry data
blocks pruning globally, including committed quarantine deletion. Unresolved
records protect the run component and its run-local input/log evidence from
storage pruning; normal read-only workspace materializations remain reproducible
from the run's retained snapshot. Backup and restore retain registry bytes,
including corrupt evidence, alongside runs and SharedRecords. Backup refuses
unsafe protected roots, unsafe/unknown or over-budget settlement registries, missing
settlement run targets, missing/digest-mismatched manifest sources or receipts for
`succeeded` effects, and unsafe members in included run, SharedRecords, receipt and
maintenance evidence, rather than silently omitting them through its general archive
filter. Generated `index.html` and
reproducible run `input`/`controller` trees remain excluded. No role-facing
read/write tool or grant is added.

Registration, updates and Context closure use existing execution exclusions;
maintenance uses its existing quiescent exclusions. No new cross-store lock or
source-lock/receipt-lock ordering is introduced. Atomic file publication uses
`fs.atomic_write`'s existing platform durability guarantees (no stronger Windows
power-loss claim). After a crash between terminal settlement and unlink, normal
storage-prune apply revalidates the exact record, flushes its directory and
replays cleanup idempotently before retiring run evidence. Recognized uncommitted
atomic-write temporaries never authorize dispatch and are removed under those
same maintenance exclusions after a complete bounded scan. There is no replay
of unresolved receipt effects and no SharedRecords collector or deletion path.

### Reference and release inventory

`SharedRecords` has no delete/prune operation, so no stored kind is currently
reclaimable. Operator and host record declarations require permanent retention.
Runtime delivery pins cover the CI-wait family above; they establish only
the execution lifetime boundary, not a complete source/receipt reference graph.
Reads and acknowledgements retain their existing per-family source-current
semantics. Other host consumers of `freeze()` must provide an exact lifetime
owner before any future source-specific release can reclaim their records.

| Kind or family | Authoritative references | Current release path |
|---|---|---|
| `ci.wait` | Work-item ID/revision/holder, retained wait evidence, exact receipt and live unit manifest. | Work-item and wait histories remain; no release hook. |
| Declared host snapshot/outcome records | Exact snapshot IDs in the subject and payload, which storage GC treats as roots; exact receipts and live manifests. | Host declarations require permanent retention; no source release hook exists. |
| Per-role acknowledgement receipts | Exact role, record ID and record digest; acknowledgement is audit evidence, not a release signal. | No receipt-deletion path exists; keep the record while any receipt remains. |

Backups include `shared-records`, `shared-record-receipts` and runtime delivery
settlements; restoring an archive may reintroduce its captured files. Mizu does
not enumerate or manage external archive paths, so archive retention/deletion is
operator-owned and is not part of the live managed-project reference graph.
Restore reconstructs managed state from the archive. **No current kind is proven eligible for
deletion, and no collector exists.**

A future release path must be source-owned and exact: each eligible kind needs a
documented release condition that accounts for source history, active manifests,
receipts and audit obligations, plus a lock boundary that prevents concurrent
writers from losing references. Add reference tracking only for a concrete kind
with proven releasable records, and keep work proportional to changed references;
do not add a generic deletion API or global reverse index speculatively. Source
owners must commit new references before a source transition can rely on them
and release them only after that transition commits. Incomplete, corrupt,
ambiguous, unsafe or over-budget evidence keeps the data. Unsupported kinds
remain permanent. External archives are outside this predicate; restoring
one can reintroduce its captured managed state. Acknowledgement time and a global
TTL never authorize deletion.

## Resolved CI policy

An operator decision resolved the earlier question: current red `ci_branch` writer wake is intentional. The default CI-status kind therefore has an explicit waking route to the writer, preserving current branch-selection and due/deduplication policy. The cache is a producer of host observations, not a special source of authority outside kind policy. A structured terminal CI-wait result separately routes only to the matching current blocked holder with its exact receipt/wait binding. Neither payload content nor cache existence implicitly wakes, assigns work or mutates task state; waking is the configured typed route. This design records the resolved policy without changing runtime behavior.

### CI-wait delivery route

Every CI-wait delivery uses the `ci.wait` kind in SharedRecords. This is a
host-owned source-specific route, not a general model-writable SharedRecords
interface. The obsolete `ci_wait_shared_records` configuration key is rejected
and must be removed from older host configurations.

The route uses immutable `ci-wait:<item>:<blocked-rev>` identities in the
existing shared store for receipt-pinned CI results. Snapshot-bound results with
a host-verified `snapshot_relation` use the versioned identity
`ci-wait:<item>:<blocked-rev>:snapshot-relation-v1`; an already receipted
legacy snapshot event is not delivered again under the new identity. Both use
writer `host.ci`, subject `{work_item, rev, holder}` and blocked-revision
`updated_at` event time. The payload is exactly the validated external-untrusted
terminal source event including its evidence digest and, for snapshot-bound
results, the exact/strict-descendant relation. Only
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
Legacy auto-ack files are not read or mapped; a still-current result without
a SharedRecords receipt is replayed. The shared store owns the only active CI-wait
receipts, and existing backup/restore includes both generic directories. See
[the full source contract](scheduling.md#exact-sha-work-item-ci-waits) for
failure/retry, diagnostics and unchanged polling/admission semantics.


