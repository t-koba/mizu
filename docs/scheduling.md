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
`on_change` is a skip gate, not a trigger — `Engine.run` in
`src/mizu/runtime.py` returns `skipped: unchanged` when the code snapshot is
already observed and nothing else admits the run.

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

A role with neither a schedule nor subscriptions stays explicit-run only
(`event_watches` in `src/mizu/services.py` returns no entry for it). An
event-only role uses file triggers on Linux/macOS; Windows currently renders
no task for an unscheduled event-only role, as noted below.

## Wake sources versus delivered inputs

`should_run` (`src/mizu/runtime.py`) is the writer-daemon readiness gate. It
first rejects unarmed/paused/draining control, then checks changed goal
digest and operator wake-generation, then an independent ready-work read.
Ready work means a seeded ledger item whose current status is `open` or
`doing` and which carries no structured wait. Any such item admits the writer
even after `done`, an unexpired run-level `wait`, or a no-progress park. This
is a project-wide source, not filtered by holder or terminal-event subscription.
Next, unacknowledged receipt-pinned CI wait evidence admits only its current
blocked holder (daemon reason `ci_wait`), including after `done`. Without ready
work or that evidence, `done` still ignores inbox, routed events, general CI,
`wake_at` and progress; a changed goal or operator wake continues to admit it.
For other outcomes, it checks inbox generation; role-routed decision/terminal
events and due structured waits (`_role_events_due`); due CI reds for opted-in
roles (`_ci_due_for`); expired `wait` `wake_at`; and finally the no-progress
self-admission guard for `continue`/`bounded` units
(`progress_disposition != parked_no_progress`).

Progress self-admission uses project `[limits].progress_signals` (example
default: `tool`, `code`, `decision`, `insight`, `input`; `state` is
optional), `no_progress_continuations`, and the effective per-role
`no_progress_backoff_seconds` (`roles.<name>.no_progress_backoff_seconds`
when set, else the project `[limits]` value).
It bounds continuation of writer daemons; it is not a platform trigger.

Non-write daemon readiness is only control state: the daemon loop in
`src/mizu/runtime.py` runs a read role at its idle cadence when the project
is armed and neither paused nor draining, without consulting `should_run`.

A readiness signal does not itself name a delivered event. The normal
prompt projects pending insights; decision events, terminal work-item events,
due waits, and CI facts are fetched by separate readers with their own role,
route, and batch policies.

Scheduled timer dispatches are plain one-shot runs: `mizu run` without
`--event` (`src/mizu/cli.py` passes `event_trigger` only for `--event`).
Event-triggered dispatch (`mizu run --event`, invoked by the platform file
triggers below) admits on a substantive decision event, a terminal
`work_item_events` row, a due structured wait, a due CI red for that
role, or ready work for a writable role, and otherwise exits with
`skipped: no-event` before allocating a run directory or any model use. For `on_change` roles whose code snapshot is
already observed, `Engine.run` otherwise skips unchanged-code dispatch unless
there is a substantive decision, terminal event, due wait, or due CI red.
Writable roles additionally bypass that skip for changed goal, operator wake,
inbox generation, prior-wait expiry, progress self-admission, or ready work;
read-only on-change roles do not use those snapshot-wide wakes, progress or
ready work. A changed code snapshot bypasses the skip for either workspace mode. The recorded
`admission` label is `decision` / `work_item` / `wait` / `ci` / `ready_work` /
`change` / `schedule`. Existing event labels take precedence over ready work;
ready work has its own label for event-triggered or unchanged-code admission.
It does not add or consume terminal work-item events. The daemon's configured
`cooldown_seconds` spacing still applies; ready work does not accelerate it.

### Bounded ready-work input

Both full and delta prompts always include `ready_work`, independently of the
ordinary `open_work_items` page. It has `items`, `total`, `overflow_count`,
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
Its prompt row includes the normalized `wait` and `wait_satisfied: true`;
the ledger stays blocked at the same revision until the writer decides what
work or evidence remains. A missing or unsatisfied wait is not ready. CI
waits keep their existing receipt-bound result notifications. Re-evaluation
uses current local time, published code and exact-revision decisions; it
neither imports acceptance nor closes work automatically.

The store's internal `page_ready(limit=...)` shares the open-page validation,
recovery and locking contract. It filters before bounded selection across the
ledger, so even a general page filled with blocked items cannot hide ready
work. Admission retains at most one ready row; prompts retain at most
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
changed holder do not receive old evidence. A completed, non-bounded run
acknowledges the exact evidence digest in a create-only per-item/revision ack;
there is no moving timestamp watermark. Unchanged terminal evidence is neither
repolled nor redelivered after acknowledgement, avoiding scheduler spin even
when the item remains blocked. Interrupted/failed/bounded delivery replays
until acknowledged; a partial ack can only replay the remainder. Changing the
wait requires the normal CAS lifecycle (leave `blocked`, then block again).
There is no automatic retry of publication or CI rerun, no wait cancellation
API beyond that lifecycle, and no auto-transition on either conclusion.

Malformed or unreadable per-waiter result/ack records fail closed for that
waiter: they are neither delivered nor acknowledged or overwritten. They do
not prevent delivery, acknowledgement or polling of unrelated waiters. Polls
retain valid observation counts alongside at most `poll_max_branches` per-item
errors (each at most 512 characters). Invalid ledger/seed/history state or
receipt bindings still fail the whole scan; they are not per-waiter evidence
errors. An acknowledgement error does not abort the remaining batch;
unacknowledged valid events remain eligible for replay.

Control and configured cooldown spacing remain in force. Daemons recheck on
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
runs for other reasons and never admit a run on their own.

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
selects non-daemon roles with event subscriptions and scheduled writable roles
(the latter also need to reevaluate their own structured waits); daemon roles
recheck their wake policy on idle ticks instead of file triggers.

- Linux (systemd user units): timers (`OnBootSec`/`OnUnitInactiveSec` for
  intervals, `OnCalendar` for calendar entries) invoke plain `mizu run`;
  daemon roles get a service with restart policy instead of a timer. Every
  watched role additionally gets a `-event.service` (oneshot,
  `mizu run --event`) plus a `.path` unit with `PathChanged` watches on
  `decision-history`, `waits`, and — when `work_item_events` is set — the
  `work-items` parent.
- macOS (launchd): `StartInterval` / `StartCalendarInterval` plists invoke
  plain `mizu run`; daemon roles get `KeepAlive` + `RunAtLoad`. Every watched
  role additionally gets an `-event.plist` with `WatchPaths` on the same
  directories and `mizu run --event` arguments.
- Windows (Task Scheduler XML): schedule triggers only — `TimeTrigger` with
  repetition for intervals, `CalendarTrigger` per `calendar` entry,
  `LogonTrigger` for daemons. There is no file trigger in the current
  schema: scheduled subscribers still see event inputs at the next timer
  firing (admission applies on every dispatch), but **an event-only role
  with no schedule currently gets no Windows task at all**. This gap is
  current behavior to revisit, not a desired guarantee.

Path/WatchPaths notifications are coarse reevaluation hints, not a
durable transactional queue. If a write is coalesced or a dispatch is busy,
there is no trigger record to replay; eventual pickup depends on a later
evaluator call (for example, a daemon tick, a scheduled run, another path
notification, or an explicit run; `src/mizu/services.py` and
`docs/configuration.md` terminal-subscription section).

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

Bounds and failure behavior: each id list has at most `limits.parallel_runs`
entries, each id is 32 hex characters, and all other fields are fixed-schema
bounded scalars or fixed reasons. A run retains its first eight observed
contended admissions (including later successful selection-retry re-admissions)
and counts further omissions instead of growing a log. Uncontended admissions
with no live tickets, unsuccessful/cancelled admissions and failed dispatch
revalidations emit nothing. A successful unqueued bypass does emit evidence,
with no queue position. Capture/sink failures do not refuse admission; missing
or stale evidence is not proof of no contention. No resource/project paths,
prompts, token payloads, native/provider session ids or arbitrary configuration
are copied. Queue schema, ticket lifetime, pending reuse, FIFO and blocked-head
bypass, lock order and cancellation remain unchanged.
