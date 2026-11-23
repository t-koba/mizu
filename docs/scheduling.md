# Scheduling compatibility map (current behavior)

This is a record of current role scheduling behavior, not a target design.
It separates the code mechanisms (admission, locks, service rendering) from
the schedule settings (`daemon` / `interval_seconds` / `calendar`, event
subscriptions, `on_change`, `ci_wake`) that operator policy selects and
`src/mizu/config.py` validates.

## Schedule modes and additive subscriptions

At most one of `daemon`, `interval_seconds`, `calendar` may be set per role
(`src/mizu/config.py` rejects combinations). Event subscriptions
(a `work_item_events` subscription together with its `work_item_sources`
route) is an independent trigger: it may accompany a schedule or run event-only
on platforms with file triggers; it does not count as a second schedule mode.
`on_change` skips same-code snapshots on scheduled/explicit runs, including for
every `review_verdict` capability holder: a typed approval on the reviewed code
digest transfers to a same-code successor, so a state-only republication needs no
new review while a changed digest re-arms it. For an unscheduled role, `on_change`
also selects the published-change event wake; for an unscheduled capability
holder, that wake likewise observes the code digest rather than the exact
snapshot ID. Pending role-readable queue messages are a separate role-addressed event
source, not the project operator wake-generation cursor.

Enabled `answers` and `notices` queues are additional role-addressed input. With
the default `wake = true`, due messages in role-readable queues admit
event/on-change dispatch and armed daemon readiness before the `done` gate.
Existing scheduling controls still win, and no queue-message lifecycle is
changed. Service-managed scheduled, event-subscribed and unscheduled on-change
roles watch their configured queues (triggers are not recursive);
explicit-run-only roles stay explicit-run-only. See [kernel.md](kernel.md) for
delivery, cursors and acknowledgements.

The shipped example (`config/config.example.toml`) exercises each pattern:

| Role | Workspace | Schedule | Additive gates |
|---|---|---|---|
| worker | write | `daemon = true` | writer-daemon `should_run` gate |
| searcher | read | `interval_seconds = 1800` | none |
| reviewer | read | `interval_seconds = 900` | `on_change = true`, optional `work_item_events` / `work_item_sources` (commented) |
| reporter | read | `calendar = ["07:00", "19:00"]` | none by default |
| consult | read | none (explicit-run only) | none |
| maintainer | write | `interval_seconds = 21600` | none |
| maintainer-searcher | read | `interval_seconds = 21600` | none |

A role with neither a schedule, subscriptions, nor unscheduled `on_change`
stays explicit-run-only for ordinary product work. Non-daemon roles still get
a coarse inbox trigger for explicitly addressed operator attention;
`event_watches` documents these separate paths. An event-only role uses file
triggers on Linux/macOS, including
a coarse `current.json` watch for unscheduled `on_change` roles, plus recovery
reevaluation for every unscheduled watched role at its effective
`event_poll_seconds` cadence (default 60 seconds, valid 60 through 31 days);
Windows has
no file trigger and instead renders a bounded poll task invoking
`mizu run --event` at that same effective cadence for event-only roles, as noted below.

A watched non-daemon role can instead opt into `event_batch_seconds` (integer
60–2,678,400 seconds, unset by default) for periodic event-only triggers on all
platforms, including for scheduled roles. It replaces file hints/recovery polls,
not ordinary schedules or admission; those can still deliver inputs earlier.
Daemon roles reject this setting. See [configuration](configuration.md) for the
separate `event_poll_seconds` policy and unchanged delivery/acknowledgement rules.

`roles.<name>.event_wakes` narrows which wake kinds (queue message,
work item, wait, CI, operator wake, ready work, code change) may admit a
standalone `--event` dispatch of a role; the default is all kinds. The runtime
masks unlisted kinds in the facts passed to `evaluate_dispatch`, so a masked
wake exits as `no-event` and consumes nothing, and a role with an empty list
gets no event units. Continuations, schedules and daemon loops are unaffected.

## Wake sources versus delivered inputs

`should_run` (`src/mizu/role_admission.py`, re-exported from `mizu.runtime`) is the writer-daemon readiness gate. It
first rejects unarmed/paused/draining control (and deferred sandbox-check
waits). For an enabled writer, an exact committed/reserved internal continuation
then supplies readiness, subject to the producer veto. Prepared intents never
supply readiness; see [the registry](#internal-writer-continuation-registry).
Otherwise it checks changed goal digest and operator wake-generation, then an
independent ready-set change, owned check-result wake / receipt-pinned CI wait, and pending role-readable queue
messages in evaluator order. Owned check results and queue messages are checked before the `done` gate;
the existing project wake-generation cursor remains a separate signal.
`mizu arm` and explicit `mizu wake` advance the operator wake generation.
`mizu resume` resets health and clears pause/drain; a true-to-false drain
transition advances the generation once, so work preserved by a drain-bounded
yield can resume without waiting for an unrelated event. Repeated clears do not
replay the wake, and a resume without an active drain preserves the generation.
Work that became due while paused and delivered inputs still admit through their
own readiness checks. Use `mizu wake` only for an additional operator-requested
reevaluation when ordinary work is not due.
Ready work for a named role requires the `work_items_transition` capability. The writer work order (`work_order`) never admits a run: `review_due` is display only.
It means an unheld `open` item the role can claim, or an `open`/`doing` item held by
that role, with no structured wait. Automatic wait reopening retains the holder;
see [project wait-resolution policy](configuration.md#work-item-wait-resolution).
The authoritative daemon `should_run` gate reconciles supported exact waits
before collecting ready-work change for a named writer with both `work_order`
and `work_items_transition`. This lets a proven superseded publication wait
restore readiness even though the blocked item was excluded from `page_ready`.
Reconciliation failure supplies no proof and does not suppress unrelated wakes.
Public status, ready/open pages and read tools remain side-effect free.
Satisfied or dead blocked waits below are ready
for their holder, or claimable by any transition-capable role when unheld.
Another role's held work never admits it. A changed eligible set (including terminal
evidence bindings below) admits the writer
even after `done`, an unexpired run-level `wait`, or a no-progress park. This
is independent of terminal-event subscriptions, but filtered by role/holder
before page selection and counting. Unnamed diagnostic callers retain the
aggregate project view. Workspace mode is not a ready-work capability.

Ready work is a change signal, not standing admission. The existing locked
full-ledger scan computes a fixed 64-hex fingerprint of **all** eligible IDs
and receipt-route terminal evidence and immutable `wait_resolution` proof
bindings, including rows beyond the prompt
limit. Unique domain-separated SHA-256 leaves (ID plus the terminal record
reference or resolution proof when present) are summed modulo 2**256, then hashed with cardinality; the validated progress role binds the comparison,
scan order, other metadata changes, prompt size and equal totals cannot substitute
for membership or exact terminal evidence. This probabilistic fingerprint is change evidence, not an
authorization or collision-free set proof, and retains constant extra memory.
Full and resumed/delta inputs capture that scan's signature when the ready
projection is built. Writer snapshots normally store it in validated progress
schema 3, or schema 4/5 with resolution-delivery receipts; observer completion
cursors store it in schema 3 as `ready_work_signature` (64 lowercase hex, or
null for no delivered input). A writer `continue`/`bounded` finish that parks
reanchors its signature to the validated complete empty finish-time inventory,
so dispositions made in that unit do not cause a redundant self-wake. Any ready
work remaining at that check refuses those finishes; work becoming ready after
it remains new input. A writer-selected `wait` or `blocked` finish keeps the
delivered ready-input signature: the row stays visible, but an unchanged set is
not a fresh wake,
even when every ready row is this holder's own future deadline. Such retries
advance no-progress handling to the configured fresh-session recovery threshold
and park until the set changes or an independent wake arrives. No new set is
anchored by either finish. Finish-time `ready_work` remains the
separate True/False/null idle diagnostic, not an admission cursor.

A writable role with both `work_order` and `work_items_transition` also records
progress schema 5's resolution-delivery receipts. `wait_resolution_tokens` holds
at most 200 exact `item_id:wait_rev` keys actually shown in its opening pages,
carried forward while still eligible. `wait_resolution_high_water` is an exact
`{signature, token}` object: a 64-lowercase-hex fingerprint of only the current
role-eligible holder-owned open resolution-token set, and a nullable exact token
boundary. It covers the contiguous shown prefix in lexicographic token order.
The fingerprint uses domain-separated SHA-256 leaves summed modulo 2**256 and
hashed with role and cardinality, like the ready signature; it is change-detection
evidence, not authorization or a collision-free set proof.

The ready page prioritizes the earliest proofs absent from both receipts and
not already shown by the open page. Only actually shown rows extend the prefix;
out-of-order shown rows remain bounded exact receipts. More than 200 current
proofs therefore advance across bounded pages without growing progress. A proof
set change or absent boundary invalidates the prefix and retries selection from
the beginning under the same ledger lock. Stale exact receipts are pruned.
This conservative reset can replay previously shown overflow after a proof-set
change, but cannot skip an unseen proof. Unrelated ready work does not change
the resolution fingerprint and cannot repeat an explicit-only resolution wake.
The extra scan is needed only on invalidation; retained scan memory stays bounded
by the prompt pages and exact receipts, not the full proof set.

Schemas 1–4 remain readable: schema 4 uses its exact receipts until the next
successful delivery upgrades it; older progress keeps its conservative existing
wake behavior. Malformed receipts fail closed. All receipts live in the existing
immutable snapshot progress record, committed only after successful delivery;
busy admission, failed delivery and restart leave unseen causes discoverable.
There is no separate store, generic ready-set cursor, acknowledgement, or
additional event type; existing `ready_work` subscriptions use this signature.

An unchanged set anchored after any finish parks until independent input, wait
expiry or another genuine wake: a `wait` or `blocked` finish with ready work
keeps its delivered signature, so the visible row advances no-progress handling
through fresh-session recovery and parks on the same set. `continue`/`bounded`
still require new actionable input or a writer handoff. Additions, removals
(including the last item), replacements with equal totals, and blocked-wait
satisfaction change membership and wake once after delivery. Claiming the same
ID or rewriting its other metadata does not. A new terminal evidence binding
for the same ID is new input; acknowledging its notification is not. Legacy schema 1/2 or absent signatures
admit nonempty current sets for reassessment, but empty sets create no event.
Malformed progress or unreadable current ledgers fail closed for this signal;
control/goal/operator short-circuit behavior and other wake sources are unchanged.
No mutable index, ordering policy or new configuration is added.
Next, an eligible owned check-result wake or unacknowledged receipt-pinned CI
wait evidence admits the role (daemon reason `ci_wait`), including after `done`.
The CI wait routes only to its current blocked holder. Without changed ready
work, an internal continuation or either check/CI signal, `done` still ignores
inbox, ordinary routed events, general CI, `wake_at` and progress. Pending role-readable queue
messages are checked before this gate; a changed goal or operator wake also
admits it.
For other outcomes, it checks inbox generation; role-routed decision/terminal
events and due structured waits (`_role_events_due`); due CI reds for opted-in
roles (`_ci_due_for`); expired `wait` `wake_at`; and finally the
writer handoff for `continue`/`bounded` units. Writers no longer self-admit on a
no-progress timer: holder-only waits surface as open role-eligible ready work,
and a progress-backed continuation needs a new actionable `next_step` with a
selected work signal for immediate writer handoff. Mere progress alone does not
qualify.

### Writer next-step handoff

A writer declares its next actionable step in the optional typed finish field
`next_step`, independently of the short prose `state`. The step must be 1–4,096
printable characters with no surrounding whitespace; empty values and
`none`/`nothing`/`n/a` placeholders (case-insensitive, ignoring trailing periods)
are invalid. This is an explicit writer declaration, not natural-language
classification: use `wait` with the exact check or a deadline when the next
step is not actionable yet. A writer `finish` with `outcome=continue` and no
valid `next_step` is refused before sealing or acknowledgement side effects.
Correct `next_step` and retry `finish` in the same unit. Other outcomes and
read-only roles need not supply it; any supplied field must be valid. The field
alone does not grant admission. It is retained in immutable finish/run and
snapshot records; legacy snapshots without it remain readable, but their prose
is never parsed for a step.

Readiness requires `continue` or `bounded`, a validated role-matching progress
record, and at least one selected work signal (`tool`, `code`, `state`, `message`, or
`input`). State rewording and delivered input alone are not work. Pure reads
(including check-job `list`, `logs`, and queued/running `result`) do not
count as tool work. Terminal result retrieval still qualifies as work; exact
check-result wake/acknowledgement and verification import are unchanged. The
declaration must differ from the exact published parent's typed `next_step`. Missing
or corrupt progress evidence, or an unavailable parent, fails closed. This reads at most
one parent, adds no store or task lifecycle, and
never changes WorkItems eligibility or any wait/event routing.

Before a writable writer parks with a `continue`/`bounded` finish without a
validated immediate next-step handoff, the host requires a complete, empty
role-scoped ready inventory. It reads `page_ready` under its 100-row
API maximum before sealing or applying acknowledgements; inventories larger
than that bound fail closed.
Unreadable evidence, false `coverage_complete`, truncation/overflow, or any ready
item refuses those finishes with a bounded actionable diagnostic. Resolve evidence
gaps and retry in the same unit. Every ready item needs a durable disposition:
finish it with the existing completion evidence, block it on a supported named
observable wait, or drop it with a reason. Holding it as `doing` is not a
disposition. Unheld open work and this role's held work count; other holders'
work does not block the role. A progress-backed new `next_step` may instead hand off
immediately under the existing validation rules. A writer-selected `wait` or
`blocked` finish is instead accepted with role-eligible ready work: the host
keeps the delivered ready-set signature, so the row remains visible but the
unchanged set does not re-admit the role or wait for a holder deadline instant.
Repeated unchanged finishes count toward no-progress recovery and park until the
set changes or an independent wake arrives, without requiring any item
transition. A host-imposed bounded yield may likewise retain ready work.

Ready-work presence alone never authorizes a timed re-admission, for either
`wait` or no-progress `continue`/`bounded` records. Ready-set changes still wake
through `should_run` and the configured `ready_work` event subscription; a
drain-to-undrained control edge is also one operator wake, consumed by the next
snapshot's wake-generation cursor. Independent CI, decision, code, work-item and
wait events keep their existing routing and acknowledgement rules. Explicit
run-level wait expiry and scheduled dispatch remain independent. Historical `wait_conversion` markers
remain valid snapshot evidence and status fields, but are no longer emitted or
timer authority. No FIFO, affinity, CI-red or check-job policy changes here.

A held `code_change` or `deadline` wait is holder-only when its holder is the
only project-assigned `workspace = "write"` role with an independent dispatch
route (`event_wakes`, `daemon`, `interval_seconds` or `calendar`). A
delegated-only second writer does not make the wait external. Holder-only waits
are open role-eligible work in the ready set; this projection does not transition
the stored item or grant timer re-admission. A holder-only `deadline` stays
ordinary held-open work in the holder's ready set with no date label and no wake
when its bound passes: the role may act on its own open work at any time, and a
timer grants it no special standing. Held CI waits use a separate
[exact absence and requester test](#exact-sha-work-item-ci-waits).

A role asks the operator by appending to the declared `questions` queue; the
operator appends a referring message to `answers`. Queue messages carry no
lifecycle beyond what role policy assigns them, and acknowledging a message is
not an answer.

The host records `finish.wait_liveness` in the existing finish intent/result:
`true` for an owned queued/running check, a pending external ledger wait, or a
held CI wait with an exact publication/SHA dispatch request and no terminal
evidence; `false` for proven absence/terminal/self-resolvable sources; `null`
for unknown evidence. Pending dependencies, pending object or proposal waits,
future deadlines held by another party, and code-change waits with another
independently dispatched project writer remain external sources. A holder-only
future deadline is self-resolvable deferred work, not an external source: it
stays in the holder's ready set as ordinary open work, with no date label or
date-based wake, and reports `false` here.
Admission rechecks current liveness.
Check discovery and the configured work-item prompt page bound the local reads;
incomplete, ambiguous, corrupt or unreadable evidence cannot prove absence.
A CI wait alone, including an unpublished snapshot without request evidence,
stays unknown. No provider is called or wait store created. Existing wait expiry,
subscribed event wakes and default/maximum wait durations are unchanged; a timer
fallback is not an event subscription. The `idle_with_open_work` status remains
the recorded-at-finish diagnostic, not a live scheduler verdict.

The published parent pair retains readiness across daemon restarts and repeated
readiness probes. A successful successor publication consumes it; repeating the
same `next_step`, even after work, does not renew it. When this is the dispatch cause,
admission is `next_task`, and both full and resumed/delta unit openings carry
the exact step as the first `task` field. Persistent session identity, stable
system context and cache remain unchanged. Independent events keep precedence;
`--event` probes still require their existing subscribed event, not `next_step` alone.

Progress records and their allowance/backoff disposition remain bounded
evidence. Read-only roles use that policy for on-change dispatch as described
below. For writers, allowance and elapsed no-progress backoff do not authorize
ready-work-only admission; an immediate handoff or independent wake is required. No-progress recovery rotation
on an already admitted run and missing-finish nudges remain defect backstops,
not general scheduling sources.

Daemon readiness follows the explicit per-role `daemon_policy` in
`src/mizu/daemon_runtime.py`, never `workspace`: `signals` (the default for `write`
roles) consults the role-aware `should_run` gate, while `periodic` (the
default for `read`/`none` roles) runs at the ordinary cadence when the project
is armed and neither paused nor draining, without consulting `should_run`.

A readiness signal does not itself name a delivered event. The normal
prompt projects ready work and the work order; terminal work-item events,
due waits, and CI facts are fetched by separate readers with their own role,
route, and batch policies.

Event-triggered dispatch (`mizu run --event`, invoked by the platform file
triggers or the recovery/poll triggers below) admits on a pending role-readable queue message, a terminal `work_item_events`
row, a due structured wait, a due CI red for that role, a changed role-eligible
ready set, an unconsumed role-cursored operator wake for a read-only role, or
an unobserved code change for an `on_change` role, and otherwise exits with `skipped: no-event` before
allocating a run directory or any model use. The code-change event carries
admission `change` when no more-specific event wins. For `on_change` roles
whose code snapshot is already observed, `Engine.run` otherwise skips
unchanged-code dispatch unless there is a pending role-readable queue message,
a terminal event, a due wait, a changed ready set, or a due CI red.
The snapshot-publishing role additionally bypasses that skip for changed goal,
operator wake or prior-wait expiry. Operator wakes and changed role-eligible
ready work bypass it independently of workspace mode. A writer next-step handoff or a read-only role-specific
progress continuation can also bypass the unchanged-code gate. Writers have no
self-resolvable-wait timer fallback to bypass it. Read-only roles
never inherit goal/inbox/run-wait generations or the
publisher's progress. Event probes still exclude generic goal/inbox/run-wait
changes, next-step handoffs and progress-only continuation; writer probes retain their existing
subscribed-event gate. A changed code snapshot bypasses the skip for either
workspace mode. The recorded
`admission` label is `queue` / `work_item` / `wait` / `ci` / `operator` /
`ready_work` / `next_task` / `change` / `schedule`, or `continuation` for an exact internal
committed/reserved intent. Such an intent bypasses both dispatch skips, not
control/producer/check holds or fresh admission checks. Without that intent,
existing event labels take precedence over ready work;
ready work has its own label for event-triggered or unchanged-code admission.
It does not add or consume terminal work-item events. The same normalized
`admission` label is exposed in both full and delta unit prompts, allowing role
policy to distinguish a schedule, delivered event source, or continuation
without operator-passed attributes; the bounded event payloads remain separate
prompt fields. The daemon's configured ordinary spacing still applies
(`roles.<name>.daemon_spacing_seconds` when set,
else the policy default: `signals` uses the role-effective `cooldown_seconds`
(`roles.<name>.cooldown_seconds` when set, else the project `[limits]` value),
`periodic` uses `idle_seconds`); it affects ordinary daemon spacing only, never
deferred `next_evaluation_at` re-entry or the `cooldown_seconds` retry policy.
Ready work does not accelerate it.

### Outcome effects

Positive outcome names are deployment policy, not runtime vocabulary. One
resolution in `mizu.config.outcome_effects` combines the built-in fallbacks
with `[limits] outcome_effects` and a role's `outcome_effects`; the existing
`progress_outcomes` setting (and its role override) owns the daemon
self-admission effect. The resolver fails closed on malformed values and
never marks `done` positive, even when configured. `no_progress` selects the
outcomes subject to unchanged-input continuation and recovery, `continuation`
selects writer handoff and read-only allowance, `idle` selects the
idle-with-open-work diagnostic, and `waiting` selects wait-style finishes that
accept ready work. A `reevaluate` disposition is an evidence gap, never proof
of progress. `examples/development/outcomes.toml` records the development
deployment's choices.

### Readiness evaluation while idle

The resident `signals` daemon evaluates readiness on each poll, which
enumerates work-item history, check jobs and the inbox. Wake and delayed-message
delivery deadlines now drive dispatch and cap the sleep between polls, so
readiness is evaluated fresh every time: this is the unchanged default behavior
and needs no reuse cache. Event dispatch (`--event`) and the other daemon
policies are unaffected.

### Read-only completion cursors

After successful completion (excluding a deferred check wait), a read-only
role atomically replaces its existing `observed/<role>.json` cursor with the
snapshot/code cursor plus `wake_generation_digest`, `state_digest`, `outcome` and
`progress`. Reads are bounded to 4096 bytes; malformed/unreadable records
provide no trusted progress. Wake generations and state are stored as fixed-size
SHA-256 digests. The generation offered at run start is acknowledged,
not a later wake arriving during execution. Failed/skipped runs do not
acknowledge it. An operator wake therefore admits each observer once, even
via `--event`, without requiring publication or spinning on a stale global
snapshot generation.

The progress field uses the existing validated bounded progress schema and
`role_progress_signals` / `progress_disposition` policy. Supported tool,
state, message, input and delivered role-input signals are recorded; code
progress is always false. State comparison uses a digest of the role's last
completion state, not another role's state. The consecutive no-progress count
saturates at 101 and advances on every outcome whose input observation is
unchanged; progress or a changed observation starts a new streak.
A progressful `continue`/`bounded` completion can self-admit
an ordinary on-change dispatch; an empty continuation replaces that evidence
and parks at the configured allowance/backoff. A fresh routed event or
operator wake can still admit parked work. Existing configuration defaults,
workspace permissions and publication/verification authority are unchanged:
granting transition capability to a read-only role does not grant source edits.
Read-only daemon cadence remains the existing control-only policy above;
this slice changes on-change dispatch, not schedule modes or spacing.

### Bounded ready-work input

Both full and delta prompts always include `ready_work`, independently of the
ordinary `open_work_items` page, filtered by the receiving role/holder. It has `items`, `total`, `overflow_count`,
`truncated`, `coverage_complete`, and a bounded `unknown_blocked` count. Each item contains
`id`, `title`, `status`, `stale`, `redecision`, ledger `updated_at`, and
`age_seconds`: time since the latest work-item ledger update (normally creation
or a transition), not time since the daemon last ran. Age is
floored to integer seconds, clamps future timestamps to zero and saturates at
2,147,483,647 seconds; invalid/missing/naive timestamps or a nonfinite clock
produce JSON null, never a fabricated age. Age is display-only and does not
prioritize admission.

A blocked item with a satisfied `deadline`, `code_change`,
`object_resolved`, or `dependency` wait re-enters `ready_work` for reassessment.
Imported blocked seeds may start unheld with one of these waits, with the wait
recorded in the immutable seed. No per-task transition or wait registration
is needed. Once satisfied, an unheld blocked seed is claimable just like unheld
open work; a held blocked item is routed only to its holder. Dependency
`recipient` keeps its existing condition semantics, not task assignment.
Its prompt row includes the normalized `wait`, `wait_liveness` (`state`,
`reason`, `wait_kind`), and `wait_satisfied: true` only for a due condition.
Legacy blocks without a wait and confidently impossible object
targets (missing or currently withdrawn) also re-enter for writer replanning,
with `wait_satisfied: false`. A superseded decision is pending: the stable
target ID follows its current revision, not the obsolete decision. Unsafe or
unavailable target evidence is unknown, never a release: an `unknown` blocked
item is excluded from `ready_work` without suppressing unrelated known-ready
work, and the page reports `coverage_complete: false` with the bounded
`unknown_blocked` count so a zero `total` never reads as an exhaustive claim.
The ledger stays
blocked at the same revision until the writer decides what work or evidence
remains. An ordinary pending wait is not ready, regardless of age. A holder-owned blocked `ci` or
`publication_ci` item also re-enters when its existing
receipt-bound route validates local terminal evidence for its exact current
revision, holder and wait. CI reuses the exact source/publication/check validators
(including declared host publication outcomes). Readiness makes no adapter calls or evidence writes. Its bounded
`wait_liveness` has `state: "terminal"`, `reason: "receipt_route_terminal"`,
`wait_kind`, `outcome` (`success`/`failure` or a declared publication outcome), and `evidence: {record_digest}`; no logs or proposal body
are copied. The digest binds the same envelope as notification delivery without exposing its record ID.
Missing evidence stays pending indefinitely; corrupt, ambiguous, mismatched or
unavailable evidence is unknown and non-ready regardless of age. Notification acknowledgements stay one-shot
and do not remove readiness or cause a new ready-set wake. Re-evaluation neither
imports acceptance nor closes work automatically; terminal evidence is input
for the holder, not completion authority (`wait_satisfied` remains false).

The store's internal `page_ready(limit=..., role_name=...)` shares the
open-page validation, recovery and locking contract. It filters before bounded selection across the
ledger, so blocked or other-holder rows cannot hide later eligible work.
Omitting `role_name` retains the aggregate page used by prompt diagnostics;
the prompt page is not itself an admission grant. Admission retains at most
one ready row; prompts retain at most
`[limits].work_items_prompt` rows (1–100), with the existing 8192-byte row and
1 MiB page bounds. The scan retains O(limit) rows; scan time follows ledger
size, as for `page_open`. Reads never change item status or resolve waits.
Unsafe or malformed stores provide no ready-work wake (other explicit wakes
keep their existing behavior); prompt construction fails closed rather than
silently omitting the unreadable projection. `Unknown` blocked liveness is reported as a coverage gap, never guessed ready:
an `unknown` item is skipped while known ready rows are still returned, with
`unknown_blocked` counting the skipped role-eligible blocked items. Retrying reads yields a new
live view; admission and prompt reads are not a frozen transaction.

New transitions to `blocked` require a supported observable wait; historical
no-wait events remain readable and replayable, without rewriting history. A
work-item-only `object_resolved` wait supports one durable subject:

- `{type:"work_item", id}` is due only on validated target history with status
  `done` or `dropped` (including explicit exhausted-chain retirement). Self
  references and dependency cycles are refused before writing; missing or
  uncertain traversal cannot authorize a new edge.

Missing targets are impossible only when absence is established; corrupt or
unavailable targets are unknown. Only work-item subjects are registered now;
older object subjects stay as inert stored history and never resolve on their
own. Exact `dependency` revision/action waits keep their existing semantics. Read tools never reopen items. Under project
`work_item_wait_resolution = "reopen"`, unit-opening reconciliation may reopen
only the waiting item's holder with immutable proof binding the exact subject,
revision and resolving state. It never mutates or completes the target. Manual
policy leaves reopening to the holder's explicit CAS transition. See
[resolution policy and proof](configuration.md#work-item-wait-resolution).
CI/proposal notifications remain separate holder-routed wakes; validated terminal
evidence also keeps the blocked item ready after notification acknowledgement.
A run-level `wait` does not block unrelated ready ledger work: record a work
item as blocked when it is not actionable. Thus leaving an `open`/`doing` item
while finishing `wait` or `done` intentionally allows another writer unit at
the configured cadence. Cache-affinity ordering is separate.

An already reopened, holder-owned item with a validated `wait_resolution`
also supplies an exact event cause for its writer (with both reconciliation
grants), including a non-autonomous writer. Pre-admission dispatch compares
current eligible proof identities (`item_id:wait_rev`) with that role's exact
receipts and, in schema 5, resolution-only shown-prefix receipt described above.
A previously unseen resolution admits; an unrelated ready-set change cannot
revive a delivered proof. Progress before schema 4 retains conservative
signature-change behavior until a new input is successfully delivered. No generic ready-work event admission is
enabled for explicit-only roles without a new resolution proof; subscriptions
and defaults are unchanged.

Pre-admission dispatch and its locked recheck derive this cause from durable
ledger proof, not an in-process transition callback. A busy admission, failure
before delivery, or restart leaves it discoverable. Successful completion binds
the input tokens in the existing immutable progress record; a later change to
the ready set alone does not repeat the event. No separate event store or
acknowledgement is introduced.

### Blocked-work liveness status

`Project.status().blocked_work` is a read-only live diagnostic over the ledger,
not a new authority or watcher. It never locks, recovers, writes, assigns or
transitions work. Rows carry `id`, `rev`, `holder`, `wait_kind`, `state`, and a
bounded reason code, plus `blocked_since`, `blocked_age_seconds` (integer seconds,
clamped to zero after clock rollback). States are
`absent` (legacy no-wait), `pending`, `due`, `impossible` (missing work-item target, an exact dependency revision superseded
or substantively conflicting, or a held CI/publication_ci wait whose pinned commit can never receive a run),
`terminal` (the same exact CI/publication/proposal route as holder readiness), and `unknown`. Generic wait diagnostics without a current
item retain `receipt_bound`; status scans have the current item and validate its
exact route. The local liveness classifier can report `self_resolvable` as
`sole_writer_code_change` or `sole_requester_ci`. Status omits those effectively
open items from the blocked view. The witness does not change the stored wait
or grant timer re-admission.
`coverage_complete` reports inventory/observation completeness; `truncated` and
`overflow_count` separately identify omitted diagnostic rows. An incomplete
view is not proof that no holder-only or external cause exists.
Unsafe paths, inconsistent revisions and malformed evidence cannot prove an
impossible releaser or terminal outcome.

Blocked age is diagnostic only: elapsed time never changes liveness, ready-work
membership or transition permissions. Pending and unknown releasers remain
blocked indefinitely without exact due, terminal or impossible evidence, or an
explicit holder/operator transition. Age never infers completion or remote
failure. Explicit deadline waits become due only at their `at` timestamp.

The first blocked timestamp is recorded in new transition events as
`blocked_since`. Legacy histories derive it by replay, including blocked decision
seeds, without rewriting any record. It survives publication/delivery rebinding,
scope changes and `blocked` → `doing` → `blocked`. Only an explicit `open` revision
resets it; `doing` and immediate reblocking cannot renew an old episode. Open
rows and work-item projections expose age too; blocked liveness diagnostics
include it even when the item is absent from full and delta `ready_work` prompts.

On `terminal` or `impossible`, the holder must append a reasoned
`blocked` → `open` transition before claiming/reblocking, naming that observation
and what will be reassessed. A terminal completion/drop remains an explicit
holder decision under its existing evidence rules. If the wait became stale
while the holder was already `doing`, a reasoned transition to `open` is required
before another wait can be registered. Registration refuses attempts to evade
this with another wait kind, a new publication or an intermediate `doing` revision.
No read-only projection appends these transitions, acknowledges an event, polls
an adapter, or repairs history.

The default is 25 rows; the internal read API accepts 1–100. Rows and response
retain the existing 8192-byte/1-MiB bounds. Selection retains O(limit) rows in
problem/actionable rows (`absent`, `impossible`, `due`, `terminal`, `unknown`) before
`pending`/`receipt_bound` rows, with ledger order within each group; scan time
follows ledger size. `total` and fixed state `counts`
cover the full scan, including omitted rows. `truncated` and `overflow_count`
report omitted seeded rows, independently of inventory `coverage_complete`.
Unknown observations set coverage false and total/counts to null, with
`diagnostic: "incomplete"`; an unsafe, malformed or incomplete ledger scan
returns `diagnostic: "unreadable"`, empty rows, null totals/counts, null
truncation/overflow, and false coverage. These are unknown
results, never evidence of zero blockers. `Project.status()` derives both
`backpressure.open_work_count` and `blocked_work` from one validated read-only
open scan; it does not run a second count-only pass. Both values fail together
when that inventory is unreadable, and the scan still covers the full seeded
inventory. Within one process, an immutable direct creation record may be reused
only while its directory and `create.json` identity, mode, link count and
timestamps are unchanged; any changed, missing or unsafe path revalidates and
fails closed. There is no cursor: use the existing
work-item read/explain APIs for the authoritative ledger and full history.
Retries simply observe a new live view; no external facts or new CI facts are
polled. Deadline, published-code and exact dependency-result conditions retain
their existing meaning. Object-question waits use the authenticated answer route described above.

### Externality of a blocker

A structured wait observes recorded state; it does not prove that an outside
actor caused the condition. In particular, `code_change`, `object_resolved`,
and `dependency` do not attest who produced the code change or resolution, and
a dependency's `recipient` does not assign the task or restrict who may satisfy
its condition. The task author should use `blocked` only when its condition is
genuinely outside the active writer's control. If the writer can cause the
code change or dependency resolution, or can retrieve the needed CI, log, artifact, or
experiment evidence with its own tools, it should keep the work actionable
rather than waiting on that condition. This is a usage rule; readiness checks
do not mechanically establish externality.

A failure hypothesis is likewise not a standing block. When the condition
that would reproduce a failure has not recurred across the number of
consecutive `ci_branch` runs set by policy, the writer closes the hypothesis
as not-reproduced and settles the work it blocked, unless a fresh
reproduction keeps the hypothesis live. Non-recurrence is evidence the
writer can gather with its own tools (`vcs_read` CI status and logs,
experiments), so an item waiting only for a recurrence is actionable, not
external; the case that motivated this rule had not recurred in five runs.
The threshold and the closure are the writer's policy judgment: no harness
check mechanically counts runs or closes a hypothesis.

### Idle-with-open-work diagnostic

`Project.status()` carries a bounded `idle_with_open_work` diagnostic of a
recorded ready-work observation, not a live admission decision. Historical
parked snapshots remain diagnosable; new parking finishes must first satisfy
the complete-empty-inventory invariant above. It is
active only when the published snapshot's outcome is `wait` or `continue`
with a no-progress record (`has_progress` false) while the ready-work
observation recorded at writer run completion is true. That observation is
one bounded existence probe (`page_ready(limit=1)`) taken before the run's
snapshot is created and stored immutably in the progress record (schema 2 or 3)
(`ready_work` True/False, or null when the probe was unreadable at finish);
Open/doing work, and blocked items whose releaser is due, absent, or confidently
impossible, count as ready. `done` and `bounded` never count, progress
(`has_progress` true) clears it, and a recorded false leaves it inactive.
The value is recorded at finish: later work creation, drops, or due
transitions never change the published run's diagnostic; only a newer writer
run with its own observation moves it. Read-only runs create no snapshot and
never move it.

The durable record is the published snapshot itself (outcome plus progress
with the schema-2/3 `ready_work` observation); no new durable file or lock is
introduced, so immutable snapshots, FIFO order, lock semantics, transitions,
no-progress recovery and publication contracts are unchanged. The recorded
value carries no work-item ids, titles or descriptions — only existence. A
recorded unknown reads as inactive (`unreadable_work_state`), as do a legacy
snapshot without a progress record and schema-1 progress without the observation
(`legacy_no_progress_record`). Malformed progress is rejected by the existing
snapshot validator; it cannot produce an active diagnostic.

### Exact-SHA work-item CI waits

Only `work_items_transition` to `blocked` accepts these CI waits:

```json
{"kind":"ci","publication_id":"<64 lowercase hex receipt ID>","checks":["Offline checks / unit","Offline checks / lint"]}
```

`checks` contains 1–64 unique, nonempty exact names, each at most 256 Unicode
characters with valid UTF-8 and no terminal controls (the VCS status name
contract); the canonical serialized array is bounded to 2048 UTF-8 bytes so
compact work-item rows retain their 8192-byte limit. Names are sorted
deterministically without trimming or case folding.
Caller-supplied branch/SHA overrides are refused. The host loads and validates
the immutable completed receipt, stores its branch and published SHA in the
wait, and revalidates that binding on stored reads. The receipt is either the
product's own publish receipt or one recorded afterwards by an operator-run
publisher; both validate through the same path, and neither is inferred from a
branch head. Retired stored object waits do not accept either CI wait.

Before a publication receipt exists, register the independent snapshot-bound form:

```json
{"kind":"publication_ci","snapshot_id":"<64 lowercase hex retained published snapshot ID>","checks":["Offline checks / unit","Offline checks / lint"]}
```

It has the same exact-check bounds. Registration validates the immutable snapshot,
requires membership in retained published history, derives its whole-capture
`code_digest`, and pins the nonempty configured `vcs.ci_branch`. It holds the
existing publication lock through the work-item append. Caller-supplied digest,
branch, SHA or publication ID overrides are refused. Stored reads revalidate the
immutable snapshot/digest and pinned branch, not current configuration or rolling
history membership. A later snapshot or branch-head change never rebases the wait.
With `vcs.ci_wait_descendants = true` (the default), the host may also
observe a retained published snapshot that is a **strict descendant** of the
pinned snapshot. It validates the exact immutable manifests and every `parent`
link, allowing at most `limits.history_index` edges, using the same ancestry
contract as host publisher supersession. History order alone proves nothing;
missing/legacy parents, incomplete chains and over-budget ancestry cannot
resolve a wait. `false` preserves exact-only observation. Receipt-pinned `ci`
waits do not use descendants. Neither policy nor a later publication changes the
original wait, its branch, or its delivery binding.

For each exact or descendant target, the host CI-wait poll searches local
completed publication receipts for that snapshot's whole code digest and the
wait's immutable branch, using the bounded receipt listing in strict mode.
Exactly one validated match is required: none, multiple matches (even at
the same SHA), truncated/scan-stopped enumeration, unsafe or corrupt candidate
records remain pending. Ordinary publication listings still skip invalid records;
this uniqueness consumer cannot use a skipped record as proof of absence. Both
product and operator-recorded external receipts use their existing validation.
The receipt records the whole capture identity; any public-source projection
retains its separate identity and is not treated as the whole snapshot.

Only after this match does the host query the receipt's exact published SHA.
The declared terminal predicate below is unchanged. Explicit partial/unknown
status metadata (`complete` other than true, `truncated`/`scan_stopped` other than
false, a non-null `cursor`) or supplied branch/SHA mismatches leave this wait
pending. Status without those optional metadata fields follows the existing
bounded checks-array contract. Receipt uniqueness is rechecked before recording
and when delivering/acknowledging a result. There is no first-match or latest-head
fallback. No receipt alone releases the wait; missing publication, refusal and
supersession are not inferred from silence, another snapshot or a changed head.
An explicitly declared [host publisher outcome](shared-records.md#declared-host-publisher-outcomes)
can also wake this exact waiter when its route enables wake: `refused` with a bounded reason, or `superseded`
with the exact newer retained snapshot. These use the existing holder/revision
delivery and explicit acknowledgement path, never automatic work completion.
A held `ci` or `publication_ci` wait is effectively open for its holder only
when that holder is the project's only independently dispatched role with the
`vcs_request` capability and adapter request grant, the local CI request
ledger is completely known and has no unresolved attempt, and an exact poll
has established zero provider runs. This is a ready-set change, not CI success,
a delivery event, an acknowledgement, or a ledger transition. A still-valid
observation applies immediately to the same held wait. Missing evidence leaves
ordinary blocked and terminal handling unchanged.

A held `ci` or `publication_ci` wait is `impossible` in blocked-work and
ready-work liveness only when the operator's VCS adapter answers a bounded
internal `ci_reachable` query for the exact pinned revision with `no` and a
reason; that adapter reason is shown verbatim to the holder. `yes` and
`unknown` answers, adapter failures, current CI evidence, an unresolvable
receipt route and bounded read errors leave the wait `pending`. The adapter
owns reachability: Mizu infers nothing from branch heads, CI requests, poll
run maps or other local facts, and makes no coverage claim of its own. This
is a holder-action replanning signal only; it never dispatches, polls,
acknowledges or transitions work, and the holder must still append a reasoned
`blocked` → `open` revision.

Adapters may attest exact run coverage in the existing `status` reply with
`ci_inventory: {repository, branch, sha, complete: true, run_count: 0}`. The
repository is the 64-hex identity from the project's existing trusted
`[vcs.lineage_target]`; it must also match any repository bound to the receipt.
The branch and SHA must match the queried receipt, with a unique exact receipt
for `publication_ci`. `complete: true` on the outer reply, no partial-page
flags/cursor, and an unfiltered empty `checks` array are also required. The
inventory asserts complete coverage of **all** workflow runs at that target,
including queued workflows that have no check rows yet. A filtered or merely
empty checks listing, a descendant result, or an absent inventory cannot attest
this. The extra field is host-only and not returned by ordinary `vcs_read`.

The poll keeps at most one disposable observation per live wait within the
existing target-call budget. Evidence expires after one configured poll
interval; a newer target poll or local request activity revokes the old
observation. Reads and item writes only check local evidence and do not call the
adapter; they do not retry or reconcile uncertain requests.

There is no deadline, automatic retry/CI dispatch, promotion, or item transition.
The existing daemon's configured observation cadence is used, not a writer
polling deadline. Unrelated work remains schedulable.

The recorded requests are visible per day. `vcs_read` with the `ci_requests` op
lists the project's recorded CI requests from local durable evidence, grouped
by UTC day: each day row counts the requests made on that day and names the
work items they covered, next to the request's own identity, workflows and
provider run identities. An attempt is dated by its dispatch marker: the day the adapter was called,
and a later answer never moves it, whether or not the provider created a run. Each row also names its explicit attempt number. A dispatch with no outcome is
listed as `unresolved` until an explicit same-attempt request obtains validated
runs or typed `not_dispatched` from the adapter's non-dispatch `reconcile_runs`
query. Unknown/malformed/lost answers leave it unresolved. Time never settles
an attempt or authorizes another dispatch; only durable `not_dispatched` permits
an explicit next numbered attempt. Legacy timeout-unknown records stay immutable
and unresolved until a digest-bound resolution sidecar supplies a validated
provider answer (see [CI dispatch uncertainty](ci-acceptance.md#ci-dispatch-uncertainty-independent-of-acceptance)).
`possibly_duplicate_spend` records risk at dispatch time: a new attempt sets it
if any earlier exact attempt is unresolved or has validated runs. Only validated
no-spend outcomes for every earlier attempt clear future risk; missing, unreadable
or over-budget history retains it (`ci_requests.record_limit`, default 1000 prior
attempts). Later reconciliation never rewrites historical flags. Retry permission
remains tied solely to the immediate predecessor's validated `not_dispatched`.
Read-only listings and status polling never settle markers, and no request is
retried automatically. An unreadable or invalid outcome stays unresolved,
never inferred from absent provider status. The listing is bounded (`total`, `truncated` and `scan_stopped`);
invalid records are not returned unvalidated, a dispatch whose marker cannot be
validated is not listed, and no provider is contacted.

Runner wall time is separate from this local count view. `vcs_read` with
`ci_costs` derives an explicitly partial observed subtotal for a page of
1–8 validated dispatches (`limit`, default 8; `offset` 0–999, default 0).
It reuses one exact publication-bound status observation per receipt within
the page and retains its evidence reference. Typed workflow-run/job/attempt/
repository identity is required for attribution; terminal `not_dispatched`
attempts trigger no status read and contribute no run timing. Unresolved attempts (including legacy timeout-unknown)
have no known run IDs, so their possible spend is not converted to zero or
attributed without matching identity. Legacy identityless checks, unresolved
attempts, missing/unavailable status, missing durations and running checks
remain visible, never converted into zero time. Checks are
attributed to the UTC dispatch day even across midnight. Identical execution
tuples are deduplicated within the page; attempts remain distinct, conflicts
are excluded. Page/scan truncation is visible. Pagination is positional and
subtotals are page-local, not additive across pages or polls. No enumeration
completeness is proven; `observed_wall_seconds` is null without timed execution
evidence, and all coverage remains `partial`. The measure is runner wall time,
not billing or savings. See ADR-037 and the configuration protocol for the
exact schema, bounds, repository attestation and failure behavior. Existing
`ci_requests` counts and CI scheduling policy are unchanged.

A run's own record names the CI requests that run made. The run record
carries `ci_requests`: the entries for the requests, each with the request
identity, the UTC day it was made, its workflows, the work items it covered
and whether it is `resolved`, `not_dispatched` or `unresolved` (legacy run
records may still say `unknown`),
plus the attempt number, bounded reason code, and `possibly_duplicate_spend`
flag. An attempt is named before the adapter call, because a CI run may have
been spent from there on; terminal outcomes update its state without erasing
the attempt. The day it was made is the day it was dispatched, and the answer that
arrives later does not move it. The list holds at
most 16 entries and the total is reported beside it, so a truncated list is
visible rather than silent. A replay of an already recorded request made no
new run, so it adds nothing to the list. An explicit unresolved-attempt query
records its validated resolution as an observation, not a new dispatch.

A CI request may cover work items. `vcs_request` takes an optional `covered`
list of at most 16 entries, each naming a work item, the revision it is
covered at, and the checks whose result settles it. The covered items are
validated before the provider is called, so an unknown item, a stale
revision, or an item that cannot take the wait never spends a run, and the
request's durable record keeps the covered set, so the request's wall time is
attributable to what it covered.
After successful dispatch is recorded, the product registers the same
receipt-pinned CI wait on each covered item: the item becomes `blocked` with
that wait, and its holder keeps the completion decision, green or red. A
`not_dispatched` attempt registers no waits and leaves the covered items
unchanged; its response marks all requested coverage `not_applied`. A replay never spends
a second run: successful outcomes return the recorded result and can complete
a coverage whose registration was interrupted. An unresolved-attempt query can
also reconcile this recorded coverage after validating runs; uncertainty cannot
register or complete waits. Only durable no-spend permits a new dispatch attempt. Coverage never applies to no-spend outcomes. A replay reports the requested items the recorded request does not
cover as not applied, and the recorded request's coverage governs the rest:
the comparison is by item identity, so the same items in another order are
the same coverage. Covering work items needs the
work-item capability and automatic CI polling, because a wait that nothing
observes would never resolve.

Registration requires `[vcs].poll_enabled = true` and a configured trusted
adapter, otherwise it fails closed. An active polling daemon is needed to
observe new results; disabling polling later leaves existing waits unresolved
until polling resumes (already retained evidence remains deliverable).

On each due VCS polling tick, the host selects at most
`poll_max_ci_wait_targets` unique receipt-resolved **`(branch, SHA)` pairs**
(default 16, range 1–64), separately from the actual branch-polling budget
`poll_max_branches`. Exact and retained descendant candidates share this target
budget; duplicate items or candidate snapshots on one pair consume only one
slot. There is no newest/nearest-only selection. With `change_stream_enabled`
disabled (the default), a rotating lexicographic window uses one durable pair
cursor in `vcs-poll.json`; legacy item-token cursors restart safely. Failed reads
and pending/unknown responses advance the window too. When enabled, healthy
change pages instead select exact-branch invalidations plus new/unobserved
wait candidates; empty pages do not cause periodic status reads. Unsupported or
invalid pages fall back to the bounded rotating window for that tick. Page
fanout, enrollment and retry state are described in the
[adapter polling contract](configuration.md). Missing or ambiguous
receipts never query status.

The host streams eligible waits/candidates to select the window, then streams
again to query each selected pair at most once and fan out that response to
all matching current waiters/candidates. Each item's exact declared check set,
receipt, revision, holder and wait are revalidated independently before writing
its own immutable evidence. Neither an adapter response nor fan-out releases or
settles an item. The scans retain no waiter list: extra memory is bounded by
the configured target count and parsed status-row limits, with candidate
discovery bounded by snapshot history. Adapter status responses retain their
existing 1024-row and byte limits. Polls use `poll_interval_seconds` and
`poll_status_timeout_seconds`. Every query names the receipt's **recorded branch
and exact published SHA**; a moved branch never substitutes its new head.

Completion is over the declared set, not over whichever rows happen to be
returned. Every declared name must occur exactly once at that SHA, in exactly
`success` or `failure` state. Missing, pending, unknown, duplicate or mismatched
named rows leave the wait unresolved. Unrelated checks impose no requirement.
Once all declared checks are terminal, all-success concludes `success`;
otherwise the conclusion is `failure`. Adapter failures or incomplete status responses do not resolve waits.

Before any wake, the host rechecks the current blocked revision and writes a
create-only record under `evidence/ci-waits/<item>.<rev>.result.json` (128 KiB,
bounded no-follow reads). It binds the item ID, blocked revision, holder,
publication ID, branch, SHA, normalized wait digest, declared check states and
available run IDs; `publication_ci` results also bind the snapshot ID and code
digest of the original pinned wait. Every snapshot-bound result carries the
host-derived `snapshot_relation`: `exact` when the receipt is for the pinned
snapshot itself, or `strict_descendant` only after the complete retained parent
chain has been verified. Descendant results also carry
`observed_snapshot:{snapshot_id,code_digest}` for the exact observed
descendant; the publication ID, branch and SHA name that descendant's unique
receipt. The relation is part of the immutable `ci.wait` payload and its digest,
so the worker can interpret the result from the event without consulting this
document. Delivery and explicit acknowledgement revalidate this **frozen**
manifest, relation, ancestry, receipt uniqueness and exact-SHA check binding,
never reselecting a later descendant or querying a moving head.
Already retained evidence remains readable after history rolls forward or the
policy is disabled. The result retains `kind:"ci"` and the same shared event/ack
route. It includes conclusion and observation time. When the provider reports
them, each declared check also keeps its start, its completion and the derived
job time: the product recomputes that job time from the two instants and never
takes it from the provider, and the stored record fails closed on the same
rules as the status row. Facts remain
external-untrusted. Receipt files are never modified and polling never
publishes. Both green and red **only wake**: neither reopens, completes, drops,
nor otherwise transitions the item. Before `done`, the holder must explicitly
record a same-scope `doing` → `doing` delivery binding to the observed descendant
and satisfy the normal exact snapshot acceptance checks; CI evidence alone is
not completion proof.

Only the matching current blocked holder receives this evidence, in full and
delta prompt `wait_events` and the run's `wait-events.json` (at most ten CI
records per run, separately from other waits). Reopened/replaced waits or a
changed holder do not receive old evidence. Each eligible result is projected
into the existing SharedRecords store as `ci.wait`, writer `host.ci`, identity
`ci-wait:<item>:<blocked-rev>`, with the blocked revision's `updated_at` as event
time. Only current eligible sources without an exact explicit generic receipt
enter the oldest-first bounded page (ten rows or the existing generic 1 MiB
frame measured against the actual serialized `wait_events` prompt contribution).
Legacy per-item auto-ack files are not imported or rewritten; still-current
results without a shared receipt replay. Current WorkItems history, the completed
Publication receipt, exact wait digest, holder and declared check set are still
revalidated at delivery and acknowledgement. The per-source 128 KiB evidence
limit and existing generic record bound are unchanged; no smaller aggregate cap
is added. A page stops before the next oldest row that would exceed the
remaining frame; after the delivered prefix is explicitly receipted, replay
starts at that row. Each individually valid source event fits an empty frame
under the per-source bound. No timestamp cursor is introduced.

The unit freezes a holder-only host manifest. Rows add `record_id`,
`record_digest` and `ack_token`; full and resumed prompts state the
`ci_wait_delivery` contract. Only unique exact tokens explicitly selected in
`finish.ack_events` may produce generic per-role SharedRecords receipts after
successful completion. Omitted/empty/partial selections leave other events
pending. Current revision, blocked status, holder, wait digest, exact publication
and terminal evidence are rechecked both before finish intent and under the
lifecycle lock before receipt writes. A stale selected source refuses the batch
without any CI receipt. Bounded/bound-hit units and finishes registering another
check wait write none. Finishing alone is never handling.

Status diagnostics remain holder-only, bounded and read-only; they report a
corrupt shared receipt only after validating the current source evidence. Legacy
per-item acknowledgement files are ignored, including malformed or unsafe files,
because they are not authority on this route. Invalid or unreadable result
evidence still fails closed for that waiter and does not block unrelated
waiters. Invalid ledger/seed/history or shared receipt bindings fail the whole
scan. Records and receipts use existing backup/restore coverage. Interrupted
create-only writes may leave a prefix: exact replay reuses it and unreceipted
events remain pending. There is no second CI receipt store, model activation
tool, or automatic deployment. See [operations](operations.md) for required
grants and invocation.

Control and configured ordinary spacing remain in force (explicit
`daemon_spacing_seconds` when set, else the policy default: `signals` uses the
role-effective cooldown, `periodic` uses `idle_seconds`; ordinary spacing only,
never deferred re-entry). Daemons recheck on
idle ticks; observation also touches the existing `waits` directory as a
best-effort hint to already-configured platform event watches. Those hints
never carry authority: dispatch revalidates the exact waiter and evidence.
Unscheduled/unsubscribed roles keep their existing explicit-run policy.

A deferred decision does not admit a run on its own: its revisit condition is
recorded on the decision, and later evidence is delivered as insight input for
a new decision. Terminal work items are the only subscribed ledger route:
`work_item_events` selects `done`/`dropped` rows and `work_item_sources`
selects `own` (items this role created) or `any` (every matching item), for both
`_role_events_due` readiness and the dispatch page. Structured due-wait rows
carry exact registration identities and role/source/payload-bound `ack_token`s
in full/delta prompts. Only handled tokens explicitly selected in
`finish.ack_events` are consumed/receipted after durable successful result and
writer publication; unselected rows replay oldest-first, and a re-registration
survives selection of the prior identity. Progress records keep a constant
`obligations_digest` (the digest of an empty identity list) for schema
continuity; there are no delivered obligation rows. CI waits sharing
`wait_events` have no token and keep their existing acknowledgement behavior.
See [the receipt contract](configuration.md) for bounds and failure behavior.

CI-wake default: `ci_wake` defaults to true for `write` roles and false
otherwise (`src/mizu/config.py`). When VCS polling is enabled, red results on
the configured CI wake branch set can wake opted-in roles when a failure/check
or published head changes, or the configured `ci_rewake_seconds` elapses;
status remains separately available to prompts. **Current behavior to revisit,
not a future guarantee:** for outcomes other than terminal `done`, the generic
inbox-generation check in `should_run` runs before the CI-specific gate, so a
writable daemon can still wake on a queue message delivered by a VCS driver
through the generic message signal even with `ci_wake = false`. Do not read
`ci_wake = false` as excluding every VCS-derived input from a writable daemon's
generic wake.

## Platform triggers

One schedule model is rendered per platform by `src/mizu/services.py`
(`event_watches`, `_render_systemd`, `_render_launchd`, `_render_windows`;
covered across hosts with `system=` in `tests/test_platform.py`). `event_watches`
selects non-daemon roles with event subscriptions, unscheduled `on_change`
roles, and scheduled writable roles
(the latter also need to reevaluate their own structured waits); daemon roles
recheck their wake policy on idle ticks instead of file triggers.

- Linux (systemd user units): timers (`OnBootSec`/`OnUnitInactiveSec` for
  intervals, `OnCalendar` for calendar entries) invoke `mizu run`; opted-in
  roles append `--scheduled`;
  daemon roles get a service with restart policy instead of a timer. Every
  watched role additionally gets a `-event.service` (oneshot,
  `mizu run --event`) plus a `.path` unit with `PathChanged` watches on
  `decision-history`, `waits`, and — when `work_item_events` is set — the
  `work-items` parent; unscheduled `on_change` roles additionally watch the
  published pointer `current.json` (the persisted level state). Every
  unscheduled watched role also gets an `-event.timer` with
  `OnBootSec=<event_poll_seconds>s`,
  `OnUnitInactiveSec=<event_poll_seconds>s`, and `AccuracySec=1s` (defaults
  60s/60s), targeting the same event
  oneshot. The install result lists both path and recovery timer for enabling,
  not the event oneshot itself.
- macOS (launchd): `StartInterval` / `StartCalendarInterval` plists invoke
  `mizu run`; opted-in roles append `--scheduled`; daemon roles get
  `KeepAlive` + `RunAtLoad`. Every watched
  role additionally gets an `-event.plist` with `WatchPaths` on the same
  directories and `mizu run --event` arguments, likewise including
  `current.json` for unscheduled `on_change` roles. That same event job also
  has `StartInterval=<event_poll_seconds>` for unscheduled recovery (default
  60); no second job is added.
- Windows (Task Scheduler XML): schedule triggers only for scheduled roles —
  `TimeTrigger` with repetition for intervals, `CalendarTrigger` per `calendar`
  entry (opted-in roles append `--scheduled`), `LogonTrigger` for daemons.
  There is no file trigger in the current
  schema: scheduled subscribers still see event inputs at the next timer
  firing (admission applies on every dispatch). Each event-only role
  (watched but unscheduled, including unscheduled `on_change`) instead gets
  an `-event.xml` task invoking `mizu run --event` on a
  `TimeTrigger` repetition at the role's effective `event_poll_seconds`
  cadence (default `PT60S`); admission still exits cheaply with
  `skipped: no-event`/`unchanged` when nothing is due, so the poll cannot
  change trigger semantics.

The descriptions above apply when `event_batch_seconds` is unset. With it set,
Linux renders an `-event.timer` with `OnBootSec` and `OnUnitInactiveSec` equal to
the batch cadence, targeting the existing event oneshot, and no `-event.path`.
macOS renders `StartInterval` equal to the batch cadence and no `WatchPaths` in
the event plist. Windows renders an `-event.xml` repetition at the batch cadence,
also for scheduled roles. Ordinary schedule files are byte-for-byte unchanged.
Rendering only produces definitions; the operator must replace any previously
installed immediate file triggers to use the new cadence.

Path/WatchPaths notifications are coarse reevaluation hints, not a
durable transactional queue. If a write is coalesced or a dispatch is busy,
there is no trigger record to replay. For every unscheduled watched role,
recovery reevaluation at its effective `event_poll_seconds` cadence (default
60 seconds) re-invokes `mizu run --event`: for unscheduled `on_change` roles
this rechecks persisted `current.json` against the
role's observed code cursor, so a lost notification delays rather than loses the
unobserved published-code input. This is not a model-start guarantee at that
cadence:
service-manager availability, a busy job, and all routing/admission gates still
apply; Linux's interval is measured after the event service becomes inactive,
with up to one second of timer accuracy slack. Scheduled services are unchanged.
Daemon ticks and
scheduled runs likewise recheck their inputs (`src/mizu/services.py` and
`docs/configuration.md` terminal-subscription section).

## Workspace and lock isolation

`Engine._admit` (`src/mizu/engine_admission.py`, lock resources asserted in
`tests/test_admission_queue.py`) holds dispatch locks for the whole run:
every role takes `locks/run-<role>.lock`; `write` roles additionally take
the project `locks/workspace.lock`. FIFO slot admission itself lives in
`src/mizu/admission.py`.

Workspace selection (`Engine.run` in `src/mizu/engine_dispatch.py`): `write` roles
run on the project workspace; any other role runs on `runs/<run-id>/input`,
which is created empty for `workspace = "none"` and otherwise filled by
materializing the published snapshot (`project.snapshots.materialize`).
No role gets an empty run-local input by accident: read roles observe the
same content-addressed snapshot the writer published, mounted read-only by
the sandbox — never the live workspace, never an unseeded directory.

On-change and event attempts evaluate the same scheduling conditions before
entering execution admission and again after obtaining its locks. An unchanged
or unsubscribed attempt returns its ordinary `skipped` reason without queueing
or using a model request. A queued attempt can still become obsolete when
another run consumes its event; the locked recheck returns `skipped` in that
case, preserving pending events until their actual consumer acknowledges them.
A disappeared admission ticket by itself is not evidence of completed work:
consult the command result (`skipped` or `Busy`) or the run's terminal receipt.

A role with no daemon, interval/calendar, or decision/work-item subscription is
explicit-run only. A `check_job` grant adds owned-result wakeups, not authority
to start unrelated project ledger work on every shared check-directory change.
Explicit invocations and actual owned-result continuations remain available;
this distinction uses the existing scheduling configuration, not role names.

## Asynchronous check-result wakes

`roles.<name>.check_result_wake` defaults to `true` for roles with `check_job`
(and has no effect without that capability). A terminal owned result
(`completed`, `interrupted`, `refused`, `cancelled`, or `cleanup_failed`) that
is unacknowledged and has not supplied an automatic dispatch attempt admits
its owner, independent of the previous `continue`, `bounded`, `wait`, `blocked`
or `done` outcome. Joined checks use their own role-bound subscription identity;
another role's completion is not an event. Existing control, check deferral,
producer, budget and execution admission gates still apply. No pending result
means no change to no-progress parking. `false` opts out of this automatic
signal; it does not disable explicit check waits or other wake sources.

`check_job action=result` reads the current state immediately and never holds
the model slot. To pause without polling, finish the unit with
`outcome=wait, wait_for_check=<owned id>`; its detached `wait_seconds` bound
sets the wake deadline
(see [results and waiting](check-jobs.md#results-and-waiting)).

Readiness/status polling has no side effects. After locked admission and prompt
construction, just before execution, Engine writes a `check-jobs/<id>/wake.json`
sidecar under `check-jobs.lock` for each eligible terminal summary in that prompt.
The record contains the job `id`, owning `role` and dispatch `run`; there is at
most one per owned job. Existence suppresses only the automatic wake, including
when malformed (fail closed for scheduling). It is not a second result store,
output acknowledgement, or acceptance evidence. Repeated dispatch bookkeeping
is idempotent. A crash after marking but before the model receives the prompt
can lose this automatic attempt; unread results remain retrievable and an exact
explicit wait can still wake. No model-receipt/exactly-once execution guarantee
is made. Marking failures fail the dispatch instead of acknowledging unread
output. The sidecar lives with the existing job directory; retention and result
authority do not change.

Automatic wake candidates take priority in the existing bounded prompt job page
(32 summaries, within the aggregate 65,536-byte check projection). Only
included terminal summaries are marked: omitted results or jobs completing
after prompt capture keep their independent wake. On both managed engines, Pi
and pi-durable, such a result is additionally steered once into the
already-running unit at its next model turn boundary, using the same capability
and policy gate. That in-unit delivery is presentation only: no polling, no
sleeping, no output acknowledgement and no model receipt; output stays behind
`action=result`. Managed Pi captures the completion index position with the
opening prompt and reads only later indexed changes: a sequence-fenced window
is sorted newest-first by job creation time with a job-id tie-break, and the
cursor advances by an exclusive `(creation time, job id)` row-value key. Work
stays proportional to the fenced window even beside older retained completions.
The captured
completion position binds the sequence high-water to the check-job index
generation: the high-water seals which window is eligible, and each window
drains before a new high opens another. A rebuilt index gets a new generation;
a live cursor re-enrolls from its start and uses the unit's already-presented
IDs to avoid repeats, so a reused sequence cannot hide a fresh result. A
position from a private in-memory repair is rejected by the durable index.
Work per boundary stays bounded, unchanged boundaries are constant-cost, and
pre-opening history cannot delay or repeat a fresh result. A completion that
races into the prompt is seeded as seen and filtered by that position, never
steered twice. Managed
Pi writes no wake marker, so the independent wake and the marker written by a
later dispatch stay unchanged. Managed pi-durable submits the notification as a
durable steer input and marks that result's automatic wake as delivered for the
running unit only after the adapter confirms the durable submission; a failed
or unsubmitted steer leaves the wake available. That acknowledgement is dispatch
bookkeeping, never output acknowledgement or acceptance. An unread result
remains in ordinary job listings after its automatic attempt but cannot cause a
scheduling spin. Existing `result` retrieval and successful-finish
acknowledgement alone consume output; a wake marker never imports verification.
Exact explicit waits retain their signature/deadline/ownership checks and
existing acknowledgement semantics, even with automatic waking disabled or
already marked. A live wait on a queued/running job still holds the role until
completion, expiry or changed inputs, including when another result becomes
terminal.

`mizu status` exposes `check_results`, keyed by enabled owning role, with up to
32 unread terminal summaries per role and an `automatic_wake_pending` boolean
on each. This includes a parked writer and already-dispatched unread results;
acknowledged results disappear. It is scheduling/visibility data, not acceptance.

## Local shadow queue observations

Execution admission optionally supplies a best-effort local observation sink;
its elapsed-float context-manager API and scheduling policy are unchanged.
`Engine.run` retains observations in `started.json` under
`queue_observations = {version: 1, observations: [...], omitted: N}`. The run id
joins them to `dispatch-attempts.json` in the same run directory. This is local,
source-owned evidence, not a public report or an ordering input. It adds no model
calls. FIFO, blocked-head bypass, retry/pending reuse, cancellation and failure
behavior are unchanged.

An observation uses the live FIFO snapshot from the successful queue transaction,
after stale tickets were reaped:

- `admission_id`: this request's 32-hex identity (the reused ticket id if queued).
  `queued` indicates whether its row was present; `fifo_position` is its one-based
  position, or null when unqueued.
- `live_ticket_ids`: live FIFO order, including this request if queued.
  `bypassed_busy_ticket_ids` names only older tickets whose resource probes
  actually raised Busy in the decision scan. External peers skipped without a
  probe are **not** reported as busy.
- `later_tickets_unexamined`: later tickets not examined by the **FIFO decision
  scan**. Choice capture adds no follow-up readiness probes.
- `elapsed_seconds`: the existing monotonic admission duration, also yielded to
  the caller; not a wall-clock instant or a reused ticket's total lifetime.
- `profile`, `engine`, `profile_engine_reason`: fixed configured hints for this
  role only. Selectors remain null with `selector_not_resolved_at_admission`;
  unavailable fixed hints use `configured_hint_unavailable`.
- `affinity` remains null with `affinity_reason = not_available_before_dispatch`.
  Neither this field nor the session facts below assert provider cache state.

`choice_set = {candidates: [...], total: N, omitted: N, complete: bool}` includes
all live queued candidates, including later tickets, and the current request.
An unqueued current request is appended to the FIFO list for evidence only.
Each candidate has `admission_id`, `readiness`, `unknown_reason`, fixed configured
`profile`/`engine` when attributable, and
`session = {key, generation, unknown_reason}`. Session keys are the existing
content-bound SHA-256 local directory keys, not native/provider session IDs.
The selected candidate's generation is read at admission using the dispatch
evidence reader (missing `generation.json` means zero); alternative generations
remain unknown. Admission never reserves a dispatch ordinal, creates a session
or resolves a selector.

Choice capture runs only after the real FIFO scan allows this request through
and its actual resource and execution-slot acquisitions succeed. It acquires
**no additional role/workspace exclusions or execution-slot locks**. The selected
request can be `ready` only with those actual locks held and an attributable
identity; only its session facts are read, while its resources remain held.
An earlier candidate is `busy` only when the real FIFO scan already observed
its resource lock unavailable (`bypassed_busy_ticket_ids`). No alternative's
profile, engine or session identity is resolved. External and later/unexamined
tickets remain `unknown`; they are not probed even when they might be ready.
These facts never change which request is selected or promise later availability.

`unknown` is separate from `ready` and `busy`, and must never be counted as
eligible. For the selected request it also covers legacy tickets without
metadata, absent observers, unresolved selectors, nonpersistent/ephemeral
sessions, unreproducible or stale identity bindings, unsafe session paths or
generation records, and unavailable identity. Pi family indirection remains
unknown rather than invoking its potentially locking/writing selection logic.
Native Codex/Claude persistent identities remain
`session_identity_unavailable_at_admission`: they require the dispatch Context's
pinned goal/policy, not an unbounded pre-dispatch snapshot/policy read or a guess
about that Context. Supported fixed Pi/Pi-durable session identity uses the
existing conversation-identity logic, not a parallel key algorithm.
A bypassed resource-busy candidate has known `busy` readiness but unknown session
facts (`resources_busy`), even without identity metadata. Known selected session
facts are observations at admission, not proof of the session ultimately
selected after routing, recovery or rotation. Unobserved alternatives keep the
choice set incomplete; the scheduler aggregate fails closed rather than
attributing eligibility or affinity to them.

Version-1 queue tickets may now carry optional
`evidence = {binding: <64-hex SHA-256>}`. This hashes the fixed role/profile,
effective settings and content identity; it copies none of those inputs into
the queue. The observer must reproduce it from its current source configuration
and project before reporting the selected request ready. Old tickets still drain unchanged;
even the current request's retained legacy/stale metadata is not silently
upgraded by an observer. An unqueued current request supplies its own fresh
binding. Metadata has the ticket's existing lifetime and is removed with that
row, including on death/reaping.

Bounds and omissions: `total` is at most
`max(admission_queue_capacity, parallel_runs) + 1` (the extra request is unqueued).
`complete` is true only when every candidate is retained and every readiness
is known; unknown session facts on a resource-busy candidate do not themselves
make readiness incomplete. `omitted` counts candidates not retained and
`total = len(candidates) + omitted`. Unknown candidates remain represented, not
silently excluded. The configured `[limits].queue_observations` bounds retained
contended admissions, including successful retry re-admissions. Setting it to
zero retains only the outer omitted count and disables choice-set detail
capture. The existing `started.json` cap (64 KiB or lower `file_bytes`) also
applies: trim alternative candidate details first, keeping the current candidate
if it fits, and mark the choice set incomplete with exact omissions. If necessary
remove whole observations and increment the outer `omitted` count. The ordinary
live-id lists remain subject to the same cap; a view too large even without
candidate details is omitted as a whole. Run retention owns the evidence's
lifetime.

Uncontended admissions with no live tickets, unsuccessful/cancelled admissions
and failed dispatch revalidations emit nothing. Successful unqueued bypasses do
emit evidence, with no queue position. Capture, identity and sink failures never
refuse admission; missing/stale evidence is not proof of no contention. No
resource/project paths, prompts, credentials, token payloads, native/provider
session IDs or arbitrary configuration are copied. No record claims cache
residency, warmth or TTL. Outcome analysis continues to use the existing
`cache_read_tokens / input_tokens` share (unknown when unavailable), not a new
cache measurement; a choice observation alone cannot attribute a cache benefit.

## Internal writer-continuation registry

`mizu.continuations` provides preparation, atomic commit proof and single-use
Engine admission for an **internal host-owned intent**. Ordinary `continue`
seals, drain/deadline wrap-up and model arguments never create intents. There
is no public tool, configuration knob, quantum or yield trigger. Safe-point
preemption remains unavailable; this is its admission substrate only.

The internal host API is intended for a future scheduler-owned yield marker:

1. While owning the writer's role/workspace exclusions, create its normal
   immutable `continue` snapshot. Call `prepare` before publishing, binding the
   enabled writer's role, source run and exact snapshot ID. Its `started.json`
   must name that role and current input snapshot; the new snapshot must name
   that source run, outcome `continue`, and current parent. Other outcomes,
   already-published seals and mismatched input identities are refused.
2. Normal `Snapshots.publish` changes the matching entry from `prepared` to
   `committed` in **the same atomic `current.json` replacement** that commits
   the snapshot/history pointer. A prepared result, existing manifest or
   orphan history generation is not commit evidence. A crash before pointer
   replacement leaves preparation only; a crash after replacement retains
   commit proof even when the caller never observed success.
3. Every subsequent publication carries the pending entries forward. Commit
   proof therefore survives restart and later writers advancing/truncating
   the current history window; it does not depend on the source still being
   head. Source manifests remain retention references.
4. `reservation(engine, project, role, wait=...)` enters ordinary admission,
   with its existing role/workspace/slot exclusions, stop checks, fresh
   authority/budget/disk checks, capacity bounds, FIFO ordering and
   resource-blocked-head behavior. With `wait=True` it joins the FIFO tail;
   a failed or cancelled admission retains the same intent. No publication
   lock is held while waiting. After admission it re-reads the pending entry
   and current published snapshot, persists state `reserved`, and yields a
   descriptor under the execution exclusions. That descriptor names the
   **then-current** tree and the stable successor identity, not the old yield
   tree. Engine uses this path instead of ordinary admission only when it
   observes a committed/reserved intent. An ordinary wake that discovers a new
   intent after queueing skips without allocating a random successor. Racing
   consumers that find their exact intent already settled likewise skip.
5. The enabled writer's committed/reserved intent admits daemon readiness even
   when ordinary progress policy parks the published head; `prepared` is never
   readiness. Armed, paused, draining, check-wait and producer gates remain in
   force. Event/on-change dispatch also recognizes this exact intent, without
   turning ordinary `continue` into an unconditional wake.
6. Before executing, Engine verifies that the workspace is the then-current
   published tree (dirty or uncapturable input refuses without discarding it).
   It creates the deterministic successor directory and durably writes its
   `started.json` **before** its active pointer or any model call. The start
   carries `run`, `role`, current input `snapshot` and `continuation` containing
   exactly `role`, `source_run`, source `snapshot`, and `successor`. Acceptance
   is create-only. A pre-start crash retries the same ID; an empty directory
   or abandoned regular single-link `.new-*` atomic-write temporaries can be
   reused without overwriting files. Other unbound files fail closed.
7. A matching existing start is an execution fence, not permission to replay a
   provider session. Under execution exclusions, return its existing terminal
   `result.json` unchanged (`completed`, `waiting`, `bounded`, `interrupted`).
   Without a terminal result, write/report `interrupted`; a `prepared` result
   is not terminal and does not authorize re-publication. Input snapshot and
   exact continuation binding are validated before reconciliation. Mismatched,
   malformed, unsafe or unreadable evidence fails closed, retaining the row.
   Active pointers matching the recovered ID are cleared; no provider state
   is invented or resumed by recovery.
8. For committed/reserved root rows, only a durable matching start plus terminal
   result allows atomic retirement of that exact row. Engine keeps it throughout
   execution; failed retirement leaves the terminal record pinned for a later
   idempotent reconciliation.
   A crash after start but before any later pointer update resolves from the
   start, never from a guessed registry state. Run evidence and manifests stay
   pinned until retirement. Existing run-envelope reads are bounded to 1 MiB.

### One bounded next-yield child

An executing continuation successor can prepare one same-role child intent.
The parent row must be `reserved`, its `successor` must equal the child's
`source_run`, and its durable `started.json` must carry the parent's exact
continuation identity. The child still needs a new `continue` seal based on
its own started input and the current head. Unrelated same-role preparation
is refused; a second child or deeper pending chain is refused.

Reads and reservation select the root first, regardless of on-disk row order.
A committed child is not independently ready, reservable, startable, recoverable,
or settleable while its parent remains. Recovery reconciles the parent first,
without provider replay. Only validated parent terminal evidence permits atomic
parent retirement; that same pointer replacement exposes the child as the new
root. A still-prepared child remains non-runnable after parent retirement.
Settlement matches the full identity, not merely the role: replaying settlement
of an already-retired parent cannot consume its child or later same-role rows.
Once the child is the reserved, executing root, it may prepare its own one child;
there are never more than two pending rows for that role. This is bounded
chaining support only, not a yield producer or a change to ordinary `continue`.

### Abandoned preparation proof

`reconcile_prepared(store, role)` is an internal cleanup attempt, not a wake
source or workspace repair. The caller owns the role/workspace exclusions
**before** taking the publication lock. An ordinary writer attempts it after
normal admission, before allocating its run; ineligible cleanup does not prevent
normal progress. `prepare` also attempts it before refusing a conflicting row.
Only that role's exact current-pointer row in state `prepared` can be retired,
including a prepared child behind a reserved parent. Cleanup preserves the
parent and all run evidence; ambiguous child failure remains pinned even after
parent recovery/retirement. Committed/reserved rows use the successor settlement
protocol above instead.

Retirement requires all of the following positive evidence:

- Bounded, link-safe source `started.json`, `result.json` and `error.json` reads
  (1 MiB per record), with the exact source run and role, validated together by
  the existing `storage_runs._terminal` lifecycle validator. Other terminal
  sidecars must not contradict that envelope. A lone error or merely prepared
  result is insufficient. The validator currently permits an interrupted
  result/error pair, not a bounded pair.
- The terminal failure result identifies the row's exact snapshot and a valid
  `finish` with outcome `continue`. Its error matches `error.json`, whose status
  is terminal interrupted/bounded, `publication` is `not_published`, and
  `durability` is `confirmed`.
- The immutable seal still names that source run and `continue`, with its parent
  bound to the source's started input. Under the publication lock the current
  pointer/history generation must be consistent and must not contain the seal.
  The atomic row must still be prepared: publication commits it in the same
  pointer replacement, and later publications carry that proof forward.
- No active source pointer, successor start, or unbound successor-run files
  contradict abandonment (only the same safe pre-start temporary forms allowed
  by `start` may remain).

Missing, malformed, unsafe, unreadable, active, contradictory, or
durability-unconfirmed evidence leaves preparation pinned and non-runnable.
Absence from the current head or a truncated history is **never** proof; no
age, timeout, or orphan history generation authorizes cleanup. Later
publications therefore neither authorize cleanup on their own nor invalidate
an exact confirmed failure. Cleanup atomically removes only the row, preserving
other roles, run/error evidence, manifests and the unsealed workspace. It is
idempotent after restart or an uncertain cleanup write. A new source may prepare
after retirement; an exact retry of a terminal failed source cannot recreate
its intent. There is still no yield producer, preemption setting, reset, or
change to drain/deadline semantics.

An invocation-lifetime, nonblocking `locks/continuation-<role>.lock` owns the
identity from reservation through settlement, including selector retry waits
that temporarily release execution admission. This lock holds no model slot;
it prevents a racing wake from reporting a live start as interrupted. Lock
order is identity ownership -> ordinary FIFO/execution exclusions ->
publication. Engine retains identity ownership outside its releasable admission
stack. Process death releases ownership; the durable start then fences recovery.

The optional pointer member is exactly
`continuations = {version: 1, entries: [...]}`. Each row contains `role`
(a normal bounded role ID), `source_run` (32 lowercase hex), `snapshot`
(64 lowercase hex), `successor` (32 lowercase hex), and `state`
(`prepared`, `committed`, or `reserved`). The successor is the first 32 hex
characters of SHA-256 over canonical JSON
`["writer-continuation-v1", role, source_run, snapshot]`; validation recomputes
it. Source runs and successors must each be unique. A role has one root and
at most one immediate child (two pending rows); the parent must be reserved,
the child prepared/committed, and the exact successor/source link must hold.
Unrelated, cross-role, malformed or deeper chains fail closed. There are at
most 64 rows per project; pointer reads are bounded to 64 KiB and source
run-envelope reads to 1 MiB. Missing metadata means no
intent. Malformed, oversized, unsafe or unreadable local state fails closed;
there is no repair-by-guessing or provider/network input. Registry overflow or
a conflicting pending role is `Busy`. Identical preparation/reservation is
idempotent, including after restart. All mutations use the existing publication
lock after execution exclusions, never before admission. The pointer and run
records are already covered by project backup/restore.

Run retirement pins each pending source run and any existing successor run;
an absent successor directory is normal because reservation precedes run
creation. A missing source or invalid registry makes the retirement plan
incomplete. Snapshot audit treats pending source snapshots as live even after
they leave current history; an invalid registry is an incomplete reference
scan. No new snapshot/object reclamation or smoke expiry is introduced.

**Safe-point handoff:** cooperative preemption is integrated but remains
default-disabled by `[limits].safe_point_yield_quantum = 0`. A nonzero quantum
permits an opportunity only at a supported, quiescent driver checkpoint when a
live FIFO waiter is observed; the observation does not admit or prioritize any
request. The host obtains one finish-only seal and preserves its chosen outcome.
Only a published `continue` seal has a prepared continuation intent, written
before normal snapshot publication and committed by that publication's atomic
pointer update. Failed seals or preparations never authorize a handoff; prepared
but unpublished entries remain unrunnable and pinned, and this design provides
no lossy repair/cancel API. Final and wait outcomes are not requeued. Ordinary
`continue`, FIFO admission, drain and deadline behavior remain unchanged.
