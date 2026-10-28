# Scheduling compatibility map (current behavior)

This is a record of current role scheduling behavior, not a target design.
It separates the code mechanisms (admission, locks, service rendering) from
the schedule settings (`daemon` / `interval_seconds` / `calendar`, event
subscriptions, `on_change`, `ci_wake`) that operator policy selects and
`src/mizu/config.py` validates.

## Schedule modes and additive subscriptions

At most one of `daemon`, `interval_seconds`, `calendar` may be set per role
(`src/mizu/config.py` rejects combinations). Event subscriptions
(`decision_events`, `work_item_events` with their `*_sources` routes) are
independent triggers: they may accompany a schedule or run event-only on
platforms with file triggers; they do not count as a second schedule mode.
`on_change` skips unchanged code on ordinary scheduled/explicit runs.
For an unscheduled role, it also selects published-code event wake: service
rendering watches the persisted snapshot pointer and invokes `mizu run --event`;
the role runs only while its observed code digest is stale. Pending eligible
operator records are a separate role-addressed event source, not the project
operator wake-generation cursor.

Enabled operator answers/notices are additional role-addressed input. With the
default `wake = true`, pending SharedRecords admit event/on-change dispatch with
`operator_record`; they also admit armed daemon readiness before the `done`
gate. Existing scheduling controls still win, and no Insight lifecycle is
changed. Service-managed scheduled, event-subscribed and unscheduled on-change
roles additionally watch the sharded `shared-records/operator` partition
(triggers are not recursive); explicit-run-only roles stay explicit-run-only. See [shared-records.md](shared-records.md)
for exact routing, frozen retrieval, acknowledgements and immutable closures.

The shipped example (`config/config.example.toml`) exercises each pattern:

| Role | Workspace | Schedule | Additive gates |
|---|---|---|---|
| worker | write | `daemon = true` | writer-daemon `should_run` gate |
| searcher | read | `interval_seconds = 1800` | none |
| reviewer | read | `interval_seconds = 900` | `on_change = true`, optional `decision_events` / `work_item_events` (commented) |
| reporter | read | `calendar = ["07:00", "19:00"]` | none by default |
| consult | read | none (explicit-run only) | none |
| maintainer | write | `interval_seconds = 21600` | none |
| maintainer-searcher | read | `interval_seconds = 21600` | none |

A role with neither a schedule, subscriptions, nor unscheduled `on_change`
stays explicit-run only (`event_watches` in `src/mizu/services.py` returns no
entry for it). An event-only role uses file triggers on Linux/macOS, including
a coarse `current.json` watch for unscheduled `on_change` roles, plus recovery
reevaluation for every unscheduled watched role at its effective
`event_poll_seconds` cadence (default 60 seconds, valid 60 through 31 days);
Windows has
no file trigger and instead renders a bounded poll task invoking
`mizu run --event` at that same effective cadence for event-only roles, as noted below.

## Wake sources versus delivered inputs

`should_run` (`src/mizu/runtime.py`) is the writer-daemon readiness gate. It
first rejects unarmed/paused/draining control (and deferred sandbox-check
waits). For an enabled writer, an exact committed/reserved internal continuation
then supplies readiness, subject to the producer veto. Prepared intents never
supply readiness; see [the registry](#internal-writer-continuation-registry).
Otherwise it checks changed goal digest and operator wake-generation, then an
independent ready-set change, owned check-result wake / receipt-pinned CI wait, and eligible operator-record
read in evaluator order. Owned check results and operator records are checked before the `done` gate;
the existing project wake-generation cursor remains a separate signal.
Ready work for a named role requires the `work_items_transition` capability. The writer work order (`work_order`) never admits a run: `review_due` is display only.
It means an unheld `open` item the role can claim, or a `doing` item held by
that role, with no structured wait. Satisfied blocked waits below are ready
for their holder, or claimable by any transition-capable role when unheld.
Another role's held work never admits it. A changed eligible ID set admits the writer
even after `done`, an unexpired run-level `wait`, or a no-progress park. This
is independent of terminal-event subscriptions, but filtered by role/holder
before page selection and counting. Unnamed diagnostic callers retain the
aggregate project view. Workspace mode is not a ready-work capability.

Ready work is a change signal, not standing admission. The existing locked
full-ledger scan computes a fixed 64-hex fingerprint of **all** eligible IDs,
including rows beyond the prompt limit. Unique domain-separated SHA-256 ID
hashes are summed modulo 2**256, then hashed with cardinality; the validated progress role binds the comparison,
scan order, metadata changes, prompt size and equal totals cannot substitute
for membership. This probabilistic fingerprint is change evidence, not an
authorization or collision-free set proof, and retains constant extra memory.
Full and resumed/delta inputs capture that scan's signature when the ready
projection is built. Successful writer snapshots and observer completion
cursors store it in validated progress schema 3 as `ready_work_signature`
(64 lowercase hex, or null for no delivered input). Finish-time `ready_work`
remains the separate True/False/null idle diagnostic, not an admission cursor.
Work added or becoming ready during execution therefore remains new input.

An unchanged set after `wait` parks until independent input, wait expiry or
another genuine wake; unchanged ready work does not bypass the existing
requirement for new actionable input or a writer handoff for `continue`/`bounded`. Additions, removals
(including the last item), replacements with equal totals, and blocked-wait
satisfaction change membership and wake once after delivery. Claiming the same
ID or rewriting its metadata does not. Legacy schema 1/2 or absent signatures
admit nonempty current sets for reassessment, but empty sets create no event.
Malformed progress or unreadable current ledgers fail closed for this signal;
control/goal/operator short-circuit behavior and other wake sources are unchanged.
No mutable index, ordering policy or new configuration is added.
Next, an eligible owned check-result wake or unacknowledged receipt-pinned CI
wait evidence admits the role (daemon reason `ci_wait`), including after `done`.
The CI wait routes only to its current blocked holder. Without changed ready
work, an internal continuation or either check/CI signal, `done` still ignores
inbox, ordinary routed events, general CI, `wake_at` and progress. Pending eligible
operator records are checked before this gate; a changed goal or operator
wake-generation also admits it.
For other outcomes, it checks inbox generation; role-routed decision/terminal
events and due structured waits (`_role_events_due`); due CI reds for opted-in
roles (`_ci_due_for`); expired `wait` `wake_at`; and finally the
writer handoff for `continue`/`bounded` units. Mere progress or an unused
no-progress allowance/backoff does not admit another writer unit.

### Writer Next handoff

A writer declares its next actionable step in the existing durable finish
`state`, as exactly one line `Next: <step>`. The state is limited to 32,000
characters; the step must be 1–4,096 printable characters with no surrounding
whitespace. Empty, duplicate, malformed and `none`/`nothing`/`n/a` declarations
are absent. This is an explicit writer declaration, not natural-language
classification: use `wait` with the exact check or a deadline when the next
step is not actionable yet.

Readiness requires `continue` or `bounded`, a validated role-matching progress
record, and at least one selected work signal (`tool`, `code`, `decision`, or
`insight`). State rewording and delivered input alone are not work. Pure reads
(including check-job `list`, `logs`, and queued/running `result`) do not
count as tool work. Terminal result retrieval still qualifies as work; exact
check-result wake/acknowledgement and verification import are unchanged. The
declaration must differ from the exact published parent's Next. Legacy/missing/corrupt evidence or an unavailable parent fails
closed. This reads at most one parent, adds no store or task lifecycle, and
never changes WorkItems eligibility or any wait/event routing.

The published parent pair retains readiness across daemon restarts and repeated
readiness probes. A successful successor publication consumes it; repeating the
same Next, even after work, does not renew it. When this is the dispatch cause,
admission is `next_task`, and both full and resumed/delta unit openings carry
the exact step as the first `task` field. Persistent session identity, stable
system context and cache remain unchanged. Independent events keep precedence;
`--event` probes still require their existing subscribed event, not Next alone.

Progress records and their allowance/backoff disposition remain bounded evidence
and the read-only on-change policy below; that disposition alone is not writer
readiness. No-progress parking, counters, recovery rotation on an already
admitted run, and missing-finish nudges remain defect backstops, not normal
observation-only scheduling sources.

Daemon readiness follows the explicit per-role `daemon_policy` in
`src/mizu/runtime.py`, never `workspace`: `signals` (the default for `write`
roles) consults the role-aware `should_run` gate, while `periodic` (the
default for `read`/`none` roles) runs at the ordinary cadence when the project
is armed and neither paused nor draining, without consulting `should_run`.

A readiness signal does not itself name a delivered event. The normal
prompt projects pending insights; decision events, terminal work-item events,
due waits, and CI facts are fetched by separate readers with their own role,
route, and batch policies.

Scheduled timer dispatches are plain one-shot runs: `mizu run` without
`--event` (`src/mizu/cli.py` passes `event_trigger` only for `--event`).
Event-triggered dispatch (`mizu run --event`, invoked by the platform file
triggers or the recovery/poll triggers below) admits on a pending eligible
operator record, a substantive decision event, a terminal `work_item_events`
row, a due structured wait, a due CI red for that role, a changed role-eligible
ready set, an unconsumed role-cursored operator wake for a read-only role, or
an unobserved code change for an `on_change` role, and otherwise exits with `skipped: no-event` before
allocating a run directory or any model use. The code-change event carries
admission `change` when no more-specific event wins. For `on_change` roles
whose code snapshot is already observed, `Engine.run` otherwise skips
unchanged-code dispatch unless there is a pending eligible operator record, a
substantive decision, a terminal event, a due wait, or a due CI red.
The snapshot-publishing role additionally bypasses that skip for changed goal,
inbox generation or prior-wait expiry. Operator wake-generation, pending eligible
operator records and changed role-eligible ready work bypass it independently
of workspace mode. A writer Next handoff or a read-only role-specific progress
continuation can also bypass the unchanged-code gate. Read-only roles never inherit goal/inbox/run-wait generations or the
publisher's progress. Event probes still exclude generic goal/inbox/run-wait
changes, Next handoffs and progress-only continuation; writer probes retain their existing
subscribed-event gate. A changed code snapshot bypasses the skip for either
workspace mode. The recorded
`admission` label is `operator_record` / `decision` / `work_item` / `wait` /
`ci` / `operator` / `ready_work` / `next_task` / `change` / `schedule`, or `continuation` for an exact internal
committed/reserved intent. Such an intent bypasses both dispatch skips, not
control/producer/check holds or fresh admission checks. Without that intent,
existing event labels take precedence over ready work;
ready work has its own label for event-triggered or unchanged-code admission.
It does not add or consume terminal work-item events. The daemon's configured
ordinary spacing still applies (`roles.<name>.daemon_spacing_seconds` when set,
else the policy default: `signals` uses the role-effective `cooldown_seconds`
(`roles.<name>.cooldown_seconds` when set, else the project `[limits]` value),
`periodic` uses `idle_seconds`); it affects ordinary daemon spacing only, never
deferred `next_evaluation_at` re-entry or the `cooldown_seconds` retry policy.
Ready work does not accelerate it.

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
state, decision, insight and delivered role-input signals are recorded; code
progress is always false. State comparison uses a digest of the role's last
completion state, not another role's state. The consecutive no-progress count
saturates at 101. A progressful `continue`/`bounded` completion can self-admit
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
`truncated`, `unseeded_decisions`, and `coverage_complete`. Each item contains
`id`, `title`, `status`, `stale`, `redecision`, ledger `updated_at`, and
`age_seconds`: time since the latest work-item ledger update (normally creation
or a transition), not time since the daemon last ran. Age is
floored to integer seconds, clamps future timestamps to zero and saturates at
2,147,483,647 seconds; invalid/missing/naive timestamps or a nonfinite clock
produce JSON null, never a fabricated age. Age is display-only and does not
prioritize admission.

A blocked item with a satisfied `deadline`, `code_change`,
`insight_decided`, or `dependency` wait re-enters `ready_work` for reassessment.
Decision tasks may start blocked and unheld with one of these waits, atomically
with their open or blocked siblings. No per-task transition or wait registration
is needed. Once satisfied, an unheld blocked seed is claimable just like unheld
open work; a held blocked item is routed only to its holder. Dependency
`recipient` keeps its existing condition semantics, not task assignment.
Its prompt row includes the normalized `wait` and `wait_satisfied: true`;
the ledger stays blocked at the same revision until the writer decides what
work or evidence remains. A missing or unsatisfied wait is not ready. CI
waits keep their existing receipt-bound result notifications. Re-evaluation
uses current local time, published code and exact-revision decisions; it
neither imports acceptance nor closes work automatically.

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
silently omitting the unreadable projection. Unseeded legacy decisions are
reported as a coverage gap, never guessed ready. Retrying reads yields a new
live view; admission and prompt reads are not a frozen transaction.

A blocked item, even one without a structured wait, is not ready. Exact-SHA
CI wait evidence is a separate holder-routed wake, not ready work.
A run-level `wait` does not block unrelated ready ledger work: record a work
item as blocked when it is not actionable. Thus leaving an `open`/`doing` item
while finishing `wait` or `done` intentionally allows another writer unit at
the configured cadence. Cache-affinity ordering is separate.

### Externality of a blocker

A structured wait observes recorded state; it does not prove that an outside
actor caused the condition. In particular, `code_change`, `insight_decided`,
and `dependency` do not attest who produced the code change or decision, and
a dependency's `recipient` does not assign the task or restrict who may satisfy
its condition. The task author should use `blocked` only when its condition is
genuinely outside the active writer's control. If the writer can cause the
code change or decision, or can retrieve the needed CI, log, artifact, or
experiment evidence with its own tools, it should keep the work actionable
rather than waiting on that condition. This is a usage rule; readiness checks
do not mechanically establish externality.

### Idle-with-open-work diagnostic

`Project.status()` carries a bounded `idle_with_open_work` diagnostic that
makes one accepted scheduler slice visible without changing scheduling. It is
active only when the published snapshot's outcome is `wait` or `continue`
with a no-progress record (`has_progress` false) while the ready-work
observation recorded at writer run completion is true. That observation is
one bounded existence probe (`page_ready(limit=1)`) taken before the run's
snapshot is created and stored immutably in the progress record (schema 2 or 3)
(`ready_work` True/False, or null when the probe was unreadable at finish);
`open`/`doing`, or a `blocked` item whose structured wait is satisfied,
counts as ready. `done` and `bounded` never count, progress
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

Only `work_items_transition` to `blocked` accepts this additional wait:

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
branch head. Insight `decide`/defer waits
do **not** accept `ci`; the existing work-item wait types remain unchanged.
The recorded requests are visible per day. `vcs_read` with the `ci_requests` op
lists the project's recorded CI requests from local durable evidence, grouped
by UTC day: each day row counts the requests made on that day and names the
work items they covered, next to the request's own identity, workflows and
provider run identities. A request is dated by its dispatch: the day it was
made is the day the provider was asked, and a later answer never moves it, so
the day comes from the dispatch marker whether the request is resolved or
not. A request whose outcome is unknown is listed too,
marked `unresolved`: the dispatch marker proves the request reached the
provider, so a CI run may have been spent, and the request is never retried
blindly. An outcome record that cannot be read is such an unknown outcome:
it is never returned unvalidated, and it never hides the request, which stays
listed as `unresolved` with no provider run. The listing is a bounded page
(`total`, `truncated`
and `scan_stopped`, as the publication listing is), an unreadable or invalid
record is skipped rather than returned unvalidated, a dispatch whose marker
cannot be validated is not listed at all, and no provider is contacted. The derived job time of a run is not part of this view: it needs
the check instants in the status rows, and a provider that reports none leaves
it empty.

A run's own record names the CI requests that run made. The run record
carries `ci_requests`: the entries for the requests, each with the request
identity, the UTC day it was made, its workflows, the work items it covered
and whether it is `resolved` or still `unresolved`, plus the total number of
requests the run made. A request is named from the moment it is dispatched,
because a CI run may have been spent from there on, and a later failure
cannot drop it. The day it was made is the day it was dispatched, and the
answer that arrives later does not move it. The list holds at
most 16 entries and the total is reported beside it, so a truncated list is
visible rather than silent. A replay of an already recorded request made no
new run, so it adds nothing to the list.

A CI request may cover work items. `vcs_request` takes an optional `covered`
list of at most 16 entries, each naming a work item, the revision it is
covered at, and the checks whose result settles it. The covered items are
validated before the provider is called, so an unknown item, a stale
revision, or an item that cannot take the wait never spends a run, and the
request's durable record keeps the covered set, so the request's wall time is
attributable to what it covered.
After the request is recorded, the product registers the same receipt-pinned
CI wait on each covered item: the item becomes `blocked` with that wait, and
its holder keeps the completion decision, green or red. A replay never spends
a second run: it returns the recorded outcome whatever became of the covered
items, completes a coverage whose registration was interrupted, and reports
the items it could not register, so a holder that resumed an item keeps that
decision. A replay reports the requested items the recorded request does not
cover as not applied, and the recorded request's coverage governs the rest:
the comparison is by item identity, so the same items in another order are
the same coverage. Covering work items needs the
work-item capability and automatic CI polling, because a wait that nothing
observes would never resolve.

Registration requires `[vcs].poll_enabled = true` and a configured trusted
adapter, otherwise it fails closed. An active polling daemon is needed to
observe new results; disabling polling later leaves existing waits unresolved
until polling resumes (already retained evidence remains deliverable).

On each due VCS polling tick, the host observes at most `poll_max_branches`
work-item waits, in addition to the existing branch/proposal polling budgets.
It uses `poll_interval_seconds` and `poll_status_timeout_seconds`, rotates by
item ID with a durable cursor in `vcs-poll.json`, and advances even on adapter
failure. Selection scans the ledger with O(limit) retained rows, not an
unbounded queue. Adapter status responses retain their existing 1024-row and
byte limits. Every query names the receipt's **recorded branch and exact
published SHA**; a moved branch never substitutes its new head.

Completion is over the declared set, not over whichever rows happen to be
returned. Every declared name must occur exactly once at that SHA, in exactly
`success` or `failure` state. Missing, pending, unknown, duplicate or mismatched
named rows leave the wait unresolved. Unrelated checks impose no requirement.
Once all declared checks are terminal, all-success concludes `success`;
otherwise the conclusion is `failure`. Adapter failures do not resolve waits.

Before any wake, the host rechecks the current blocked revision and writes a
create-only record under `evidence/ci-waits/<item>.<rev>.result.json` (128 KiB,
bounded no-follow reads). It binds the item ID, blocked revision, holder,
publication ID, branch, SHA, normalized wait digest, declared check states and
available run IDs, conclusion and observation time. When the provider reports
them, each declared check also keeps its start, its completion and the derived
job time: the product recomputes that job time from the two instants and never
takes it from the provider, and the stored record fails closed on the same
rules as the status row. Facts remain
external-untrusted. Receipt files are never modified and polling never
publishes. Both green and red **only wake**: neither reopens, completes, drops,
nor otherwise transitions the item.

Only the matching current blocked holder receives this evidence, in full and
delta prompt `wait_events` and the run's `wait-events.json` (at most ten CI
records per run, separately from insight waits). Reopened/replaced waits or a
changed holder do not receive old evidence. With the default
`[vcs] ci_wait_shared_records = false`, a completed, non-bounded run
acknowledges the exact evidence digest in a create-only per-item/revision ack;
there is no moving timestamp watermark. Unchanged terminal evidence is neither
repolled nor redelivered after acknowledgement, avoiding scheduler spin even
when the item remains blocked. Interrupted/failed/bounded delivery replays
until acknowledged; a partial ack can only replay the remainder. Changing the
wait requires the normal CAS lifecycle (leave `blocked`, then block again).
There is no automatic retry of publication or CI rerun, no wait cancellation
API beyond that lifecycle, and no auto-transition on either conclusion.

The host may explicitly enable `ci_wait_shared_records` (default false; examples
remain inactive). This changes only terminal waiter delivery/receipts, not
polling, CI requests, status authority, routing or admission policy. Current
WorkItems history and the completed Publication receipt are still replayed and
validated. Terminal evidence is projected into the existing SharedRecords store
as `ci.wait`, writer `host.ci`, identity `ci-wait:<item>:<blocked-rev>`, with the
blocked revision's `updated_at` as event time. The payload is the exact validated
external-untrusted result plus its evidence digest. The per-source 128 KiB
evidence limit is preserved at poll/write and each shared envelope uses the
existing generic record bound; no smaller aggregate cap is added. Pages stop at
ten rows or the existing generic 1 MiB frame measured against the actual
serialized `wait_events` prompt contribution (not canonical list framing),
oldest blocked revision then item ID first, after source eligibility and explicit
receipt filtering. A page stops before the next oldest row that would exceed
the remaining frame; after the delivered prefix is explicitly receipted, replay
starts at that row. Each individually valid source event fits an empty frame
under the per-source bound. No timestamp cursor is introduced.

The unit freezes a holder-only host manifest. Rows add `record_id`,
`record_digest` and `ack_token`; full and resumed prompts state the enabled
`ci_wait_delivery` contract. Only unique exact tokens explicitly selected in
`finish.ack_events` may produce generic per-role SharedRecords receipts after
successful completion. Omitted/empty/partial selections leave other events
pending. Current revision, blocked status, holder, wait digest, exact publication
and terminal evidence are rechecked both before finish intent and under the
lifecycle lock before receipt writes. A stale selected source refuses the batch
without any CI receipt. Bounded/bound-hit units and finishes registering another
check wait write none. Finishing alone is never handling on this route.

Legacy `evidence/ci-waits/*.ack.json` files are not imported or consulted for
shared delivery; an otherwise-current result may replay after activation.
Status diagnostics remain holder-only, bounded and read-only, including corrupt
legacy or shared receipt visibility. Unsafe/unreadable paths fail closed;
corrupt bytes are never repaired. Records and receipts use existing backup and
restore coverage. Interrupted create-only writes may leave a prefix: exact
replay reuses it and unreceipted events remain pending. There is no second CI
receipt store, model activation tool, or automatic deployment.

Malformed or unreadable per-waiter result/ack records fail closed for that
waiter: they are neither delivered nor acknowledged or overwritten. They do
not prevent delivery, acknowledgement or polling of unrelated waiters. Polls
retain valid observation counts alongside at most `poll_max_branches` per-item
errors (each at most 512 characters). Invalid ledger/seed/history state or
receipt bindings still fail the whole scan; they are not per-waiter evidence
errors. An acknowledgement error does not abort the remaining batch;
unacknowledged valid events remain eligible for replay.

For the holder's exact-publication `vcs_read status` diagnostic, a selected
item reports `acknowledgement: "corrupt"` only when safely read acknowledgement
content is malformed or invalid against valid retained result evidence. Valid
or absent acknowledgements omit the field; omission is not an acknowledgement
inference. Reads are bounded and return no raw bytes; no record is changed.
Unsafe paths, oversized/unreadable records, invalid result evidence and shared
ledger errors refuse the projection rather than being labeled corruption. The
existing ten-item bound and truncation flag apply, and neither this view nor
its failures change polling or delivery. See [operations](operations.md) for
the required grants and invocation.

Control and configured ordinary spacing remain in force (explicit
`daemon_spacing_seconds` when set, else the policy default: `signals` uses the
role-effective cooldown, `periodic` uses `idle_seconds`; ordinary spacing only,
never deferred re-entry). Daemons recheck on
idle ticks; observation also touches the existing `waits` directory as a
best-effort hint to already-configured platform event watches. Those hints
never carry authority: dispatch revalidates the exact waiter and evidence.
Unscheduled/unsubscribed roles keep their existing explicit-run policy.

Deferrals ride along in `own`-source mode but do not admit a run on their
own; with `decision_sources = "any"` a matching action — deferrals
included — admits one focused run (`Engine.run` filters `substantive_events`
by source mode, and `_role_events_due` applies the same rule). Terminal
routing is filtered independently by `work_item_sources`. Delivered
obligations (`pending_obligations` in `Engine.run`) ride along when the role
runs for other reasons and never admit a run on their own. Structured due-wait
and obligation rows carry exact registration identities and role/source/payload-bound
`ack_token`s in full/delta prompts. Only handled tokens explicitly selected in
`finish.ack_events` are consumed/receipted after durable successful result and
writer publication. Unselected rows replay oldest-first; a re-registration
survives selection of the prior identity. Obligation receipts suppress visibility
only, never assign work or satisfy a dependency. CI waits sharing `wait_events`
have no such token and keep their existing acknowledgement behavior. See
[the receipt contract](configuration.md) for bounds and
failure behavior.

CI-wake default: `ci_wake` defaults to true for `write` roles and false
otherwise (`src/mizu/config.py`). When VCS polling is enabled, red results on
the configured CI wake branch set can wake opted-in roles when a failure/check
or published head changes, or the configured `ci_rewake_seconds` elapses;
status remains separately available to prompts. **Current behavior to revisit,
not a future guarantee:** for outcomes other than terminal `done`, the generic
inbox-generation check in `should_run` runs before the CI-specific gate, so a
writable daemon can still wake on a VCS-sourced insight through the generic
inbox signal even with `ci_wake = false`. Do not read `ci_wake = false` as
excluding every VCS-derived input from a writable daemon's generic wake.

## Platform triggers

One schedule model is rendered per platform by `src/mizu/services.py`
(`event_watches`, `_render_systemd`, `_render_launchd`, `_render_windows`;
covered across hosts with `system=` in `tests/test_platform.py`). `event_watches`
selects non-daemon roles with event subscriptions, unscheduled `on_change`
roles, and scheduled writable roles
(the latter also need to reevaluate their own structured waits); daemon roles
recheck their wake policy on idle ticks instead of file triggers.

- Linux (systemd user units): timers (`OnBootSec`/`OnUnitInactiveSec` for
  intervals, `OnCalendar` for calendar entries) invoke plain `mizu run`;
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
  plain `mizu run`; daemon roles get `KeepAlive` + `RunAtLoad`. Every watched
  role additionally gets an `-event.plist` with `WatchPaths` on the same
  directories and `mizu run --event` arguments, likewise including
  `current.json` for unscheduled `on_change` roles. That same event job also
  has `StartInterval=<event_poll_seconds>` for recovery (default 60);
  no second job is added.
- Windows (Task Scheduler XML): schedule triggers only for scheduled roles —
  `TimeTrigger` with repetition for intervals, `CalendarTrigger` per `calendar`
  entry, `LogonTrigger` for daemons. There is no file trigger in the current
  schema: scheduled subscribers still see event inputs at the next timer
  firing (admission applies on every dispatch). Each event-only role
  (watched but unscheduled, including unscheduled `on_change`) instead gets
  an `-event.xml` task invoking `mizu run --event` on a
  `TimeTrigger` repetition at the role's effective `event_poll_seconds`
  cadence (default `PT60S`); admission still exits cheaply with
  `skipped: no-event`/`unchanged` when nothing is due, so the poll cannot
  change trigger semantics.

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

## Producer admission

Producer admission (`src/mizu/scheduling.py` `ProducerFacts` /
`evaluate_producer`, `src/mizu/runtime.py` `producer_hold`) holds only
configured non-writer project roles that carry `submit_insight` (the writer
can still resolve, merge, defer, or reject input at capacity), before any model
or run-slot use, when their role-name source's unresolved live input count
(undecided plus deferred; resolved and archive-only history excluded) reaches
`[limits].producer_inputs_per_source`. The open/doing/blocked WorkItems
count reaches `[limits].open_work_items` only for a role that explicitly opts in
with `roles.<name>.held_at_capacity = true` (default false); the project
work-item seed cap itself remains active for every role. Per-source holds are
unaffected by this opt-in. Status reports each held source (role name), reason,
and exact `thresholds` map in `backpressure.held`. The policy applies to direct, scheduled,
event-triggered, and daemon admission: `Engine.run` checks
before FIFO slot admission, and `should_run` vetoes an otherwise-ready daemon
iteration. A held attempt returns `skipped: producer-held`, claims no run
directory, slot, or lock, and acknowledges no event or cursor, so pending
decision, terminal-work-item, wait, CI, and operator inputs stay durable. It
fails closed when the load cannot be read; a running unit may finish. Release
follows the wake-semantics default: no duplicate queue or implicit schedule
is added, so a held producer resumes only through an existing schedule tick,
interval/calendar firing, event trigger with pending input, or daemon
readiness re-evaluation, with repeated state-based triggers coalescing
through the existing durable stores. `submit_insight` does not implicitly
subscribe its sender to decision events for its proposals. A role without a
schedule or declared event subscription has no subscribed events pending to
lose; its proposals remain durable, but submission creates no wake path.

## Workspace and lock isolation

`Engine._admit` (`src/mizu/runtime.py`, lock resources asserted in
`tests/test_admission_queue.py`) holds dispatch locks for the whole run:
every role takes `locks/run-<role>.lock`; `write` roles additionally take
the project `locks/workspace.lock`. FIFO slot admission itself lives in
`src/mizu/admission.py`.

Workspace selection (`Engine.run` in `src/mizu/runtime.py`): `write` roles
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

Automatic wake candidates take priority in the existing bounded prompt job
page (32 summaries, within the aggregate 65,536-byte check projection). Only
included terminal summaries are marked: omitted results or jobs completing
after prompt capture keep their independent wake. An unread result remains in
ordinary job listings after its automatic attempt but cannot cause a scheduling
spin. Existing `result` retrieval and successful-finish acknowledgement alone
consume output; a wake marker never imports verification. Exact explicit waits
retain their signature/deadline/ownership checks and existing acknowledgement
semantics, even with automatic waking disabled or already marked. A live wait
on a queued/running job still holds the role until completion, expiry or changed
inputs, including when another result becomes terminal.

`mizu status` exposes `check_results`, keyed by enabled owning role, with up to
32 unread terminal summaries per role and an `automatic_wake_pending` boolean
on each. This includes a parked writer and already-dispatched unread results;
acknowledged results disappear. It is scheduling/visibility data, not acceptance.

## Local shadow queue observations

Execution admission optionally supplies a best-effort local observation sink;
its elapsed-float context-manager API and scheduling policy are unchanged.
`Engine.run` retains these observations in `started.json` under
`queue_observations = {version: 1, observations: [...], omitted: N}`. The run id
in that file joins them to the later `dispatch-attempts.json` records in the
same run directory. This is local evidence, not a public report or an ordering
input. It does not add model calls or resource/slot lock probes.

Each observation records facts from the live ticket snapshot used by the
**successful queue transaction**, after stale tickets were reaped; it includes
this admission's row when the request was queued:

- `admission_id`: the current request's 32-hex identity (the reused ticket id
  when it had a persistent row); `queued`: whether that row was present;
  `fifo_position`: its one-based position, or null for an unqueued admission.
- `live_ticket_ids`: the exact live FIFO id order, including this request if
  queued. `bypassed_busy_ticket_ids`: only older tickets whose resource probes
  actually raised Busy in the existing scan. On a successful scan all older
  tickets were resource-blocked; otherwise a ready older ticket refuses entry.
- `later_tickets_unexamined`: the number of later tickets whose resource
  readiness was **not checked** (zero for an unqueued admission).
- `elapsed_seconds`: the existing monotonic elapsed admission time, also
  yielded to the caller; it is not a wall-clock instant or the age of a reused
  ticket across previous fail-fast invocations.
- `profile` and `engine`: fixed configured hints only for a role with no
  selector. Otherwise both are null with `profile_engine_reason` set to
  `selector_not_resolved_at_admission` (or `configured_hint_unavailable` when
  a fixed hint cannot be read); the reason is null for known hints.
- `affinity`: always null, with `affinity_reason` set to
  `not_available_before_dispatch`. Admission does not know a local session or
  cache-affinity identity, much less provider residency, warmth or TTL.

The live order is **not a complete ready candidate/choice set**. Readiness of
later tickets is unknown, and no other tickets' profiles or affinities are
available from queue rows. Do not rank choices or attribute cache benefits from
these observations alone.

Bounds and failure behavior: each id list normally has at most
`limits.admission_queue_capacity` entries. While prior version-1 tickets drain,
reads allow up to `max(admission_queue_capacity, parallel_runs)` entries; this
does not increase new-enqueue capacity. Each id is 32 hex characters, and all
other fields are fixed-schema bounded scalars or fixed reasons. A run retains
its first eight observed contended admissions (including later successful
selection-retry re-admissions)
and counts further omissions instead of growing a log. Uncontended admissions
with no live tickets, unsuccessful/cancelled admissions and failed dispatch
revalidations emit nothing. A successful unqueued bypass does emit evidence,
with no queue position. Capture/sink failures do not refuse admission; missing
or stale evidence is not proof of no contention. No resource/project paths,
prompts, token payloads, native/provider session ids or arbitrary configuration
are copied. Queue schema, ticket lifetime, pending reuse, FIFO and blocked-head
bypass, lock order and cancellation remain unchanged.

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
