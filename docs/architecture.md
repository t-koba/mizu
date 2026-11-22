# Architecture

## Boundary first

Mizu separates the control plane, the model process, the execution sandbox and
published evidence.

Mechanism provides capability without prescribing behavior; policy defines
intent and decisions. Keep mechanism minimal (KISS) and avoid embedding
behavioral constraints, heuristics, or assumptions into code. Ask whether a
change provides a capability or enforces a behavior; behavioral rules belong in
operator policy, not mechanism.

Mizu is an operating system for model roles. The kernel ([kernel.md](kernel.md))
provides units, scheduling, declared message queues, accounting, drivers and
tools; it names no role, queue, message kind, state or workflow and keeps no
record state. Roles, what each queue carries and what a reader does with it,
what completion or publication requires, and what models are told are
deployment configuration and policy. [os-migration.md](os-migration.md) records
the stages that removed the built-in workflow: proposals, decisions, questions,
answers, notices and receipts are now queues, work items and host records, and
role policy text. A Markdown policy explains which work to choose; it cannot
grant capabilities or change limits.

The trusted computing base consists of the host OS kernel, the operator account,
Python, the Mizu release, Node/Pi/CLIs and their installed dependencies, the
container runtime (Podman/Docker), and selected container images. Provider
responses, repositories, search results, experiments, and proposals are untrusted
inputs. The trusted host adapter never executes model-provided host commands.

## Components

| Module | Responsibility | Does not decide |
|---|---|---|
| `config.py` | Strict typed TOML, grants, limits, profile resolution | Project priorities |
| `drivers.py` | Engine routing by profile, shared logical admission | Model/provider selection |
| `runtime.py` | Slot/admission primitives, `Context` and `Engine` composition, delegation/deadline/seal/checkpoint gates, capability validation, tool accounting and dispatch; runtime re-exports extracted helpers | Which algorithm is best |
| `context_protocol.py` | Host-owned `Context` bridge operations (request/usage/steer handshakes, engine-tool identities and request admission), their narrow state inputs, and deferred-fault classification; runtime composes the inputs and re-exports the fault helpers | Which operation is a model capability, or which faults count toward the brake |
| `daemon_runtime.py` | Per-project VCS poll coordination (`poll_project_vcs`) and the resident daemon loop; re-exported via `runtime.py` | Which algorithm is best |
| `unit_finish.py` | Unit finish validation, acknowledgement selection, writer-parking evidence and progress records; runtime re-exports its helpers | Which work is ready, which events were delivered, or which finish outcome is desirable |
| `engine_admission.py` | Fresh Engine authority, budget and disk guards plus FIFO dispatch lock admission | Which role should run, request priority, or configured limit values |
| `engine_delegation.py` | Caller-authoritative child-role and delegation-request resolution and reservation, plus the capability gate, mixed back into `Engine`; runtime re-exports the gate names | Which role to delegate to, request authority details, or retry/scheduling policy |
| `engine_collaboration.py` | Consultation and one-shot delegation child execution: caller-fenced snapshots, ephemeral child runs and result records, mixed back into `Engine` | Which roles answer, which task is delegated, or retry/scheduling policy |
| `context_driver.py` | Context exec-session, driver-checkpoint seal lifecycle, and cancellation/wake state, mixed back into `Context` | Whether work is ready, which events arrived, or any dispatch/scheduling policy |
| `context_lifecycle.py` | Context wall/request deadlines, wrap-up bounds, drain refusal and activity/CI accounting, mixed back into `Context`; runtime re-exports `WRAP_UP_OPS` | When a bound is reached, which wrap-up outcome is desirable, or activity/scheduling policy |
| `engine_execution.py` | Driver resolution, one-turn dispatch, safe-point checkpoint sealing and bounded execution, mixed back into `Engine` | Which profile or model runs, when to yield, or dispatch scheduling policy |
| `engine_run.py` | Whole-run `Engine` orchestration: maintenance and probe service, slot admission, prompt projection, the sealed model turn and durable result/health records, mixed back into `Engine`; runtime re-exports its helpers | Which role runs, request priority, or configured limit values |
| `unit_prompts.py` | Full unit prompt, resumed-session delta, and bounded display projections for ready work, decisions, research, CI, and the writer work order | Session selection/rotation/recovery, admission, scheduling, or lifecycle policy |
| `workspace_reads.py` | Role-scoped workspace file listing, paged reads, and snapshot diff projections, including virtual upstream refs | Role grants, workspace write policy, or publication semantics |
| `evidence_records.py` | Exact role-bound retained-evidence reads and durable per-invocation/mutation evidence records | Adapter retries, reconciliation, or evidence truth |
| `run_health.py` | Role-health failure/streak bookkeeping and capture-cause persistence; runtime re-exports its helpers | Terminal outcome classification, recovery/rotation, or successful-run lifecycle decisions |
| `ci_observation.py` | Read-only cached-CI failure projection and wake-branch normalization (bounded, never raises) | Polling, wake policy, admission, dispatch, session lifecycle |
| `ci_wake.py` | Per-role CI wake record/due helpers and wake-version/default constants (bounded, never raises) | Polling, admission, dispatch, session lifecycle |
| `publication.py` | Immutable snapshot publication commit point and its source-commit evidence; runtime re-exports `source_commit` | Which snapshot is published, or why publication was refused |
| `vcs_poll_policy.py` | Pure VCS poll bounds, timeout caps, cadence, settings capping, and due calculations | Poll orchestration, adapter calls, cache/wake state, admission, dispatch, session lifecycle |
| `vcs_proposal_poll.py` | One-page opted-in VCS proposal polling, cursor validation, and proposal recording | Shared poll-state persistence, coordinator error conversion, admission, dispatch, session lifecycle |
| `vcs_upstream_poll.py` | Upstream ref fetch/injection and due periodic fetch events, with capped adapter settings | Sync grant checks, shared poll-state persistence, daemon orchestration, admission, dispatch, session lifecycle |
| `ci_polling.py` | Bounded CI branch polling into the CI notification cache with success-cache merge/prune helpers | Poll coordination, shared poll-state persistence, daemon orchestration, cache projections, per-role wake policy, admission, dispatch, session lifecycle |
| `pi.py`, `adapters/pi/` | Version-specific RPC and session transport | Model selection policy |
| `codex.py`, `claude.py` | Trusted CLI argv, isolated per-run config, event parsing | Research quality, host-tool absence |
| `mcp_loop.py` | Shared LF-framed JSON-RPC stdio loop for MCP servers | Tool semantics or protocol extensions |
| `mcp_proxy.py` | stdio MCP-to-bridge proxy with no authority of its own | Tool semantics |
| `protocol.py` | Tool schemas and strict parameter validation | Tool implementation semantics |
| `sandbox.py`, `process.py` | Bounded execution, termination, cleanup | Experiment usefulness |
| `snapshot.py` | Content objects, immutable manifests, published pointer | Semantic correctness |
| `shared_records.py` | Bounded canonical records, exact role/record/digest receipts and manifest-scoped paged reads | Source eligibility, route policy, lifecycle, or cutover |
| `host_records.py` | Declared publisher-outcome validation and host-only exact-snapshot record route | Publisher observation, generic record storage, work-item lifecycle or completion |
| `work_items.py` | Append-audited records, CAS lifecycle, bounded open pages and by-ID history | Work-item scheduling and policy |
| `work_order.py` | Writer's bounded atomic current plan with CAS, run receipt, and review-gated display only | Item status/scope/evidence, scheduling, admission, or priority |
| `web.py` | Allowed retrieval, policy-bounded protocol observation, and external source receipts | Scientific truth |
| `materialize.py` | Generic offline-input adapter (spec in, digest-bound content out) plus retained-receipt apply validation (spec/result output equality, digest, path safety) | Ecosystem resolution, cache policy |
| `editor.py` | Snapshot export and read/propose MCP | Code editing |
| `report.py` | Escaped static artifact (Markdown + evidence) with atomic latest pointer | Presentation, retention policy |
| `services.py` | Render OS service definitions (systemd / launchd / Task Scheduler) | Implicitly start or arm |
| `storage.py` | Paused backup, bounded restore, conservative pruning | Evidence retention policy |
| `budget.py` | Shared day request budget (configured timezone) and retention reaping | Monetary spending caps |
| `usage.py` | Aggregated token usage facts from completed run records | Billing rates or currency pricing |
| `doctor.py` | Platform, runtime, isolation, and budget health checks | Autonomous repair or bypasses |

There is no long-lived daemon listening for work. Each run opens a short-lived
private bridge channel — a Unix socket where the platform serves one,
otherwise a loopback TCP socket carrying the same per-run token — connecting
each extension to its work unit. Files are the inter-process handoff.
JSON is the machine format; Markdown is the human policy/report format.

[Shared records](shared-records.md) are a bounded immutable store with
manifest-scoped reads and exact receipts for CI waits and host publisher
outcomes. Questions, answers, notices and proposals are declared queues; their
routing, wake rules and meaning are configuration and role policy, not a
runtime authority marker or adapter.

## Work-unit transaction

1. Check arm/pause state, shared request budget, free disk, and role eligibility.
2. Acquire role lock; writers also acquire the workspace lock. Acquire a bounded
   global execution slot. Import any Editor proposals into private storage.
3. Capture the published snapshot, goal digest, current wake generation, and
   inbox generation. Observers get a materialized immutable code view.
4. For command-capable real runs, check container runtime prerequisites and clean
   up leftover labelled containers. Record the run and active marker.
5. Launch the managed Pi SDK, Codex app-server or official Claude SDK adapter.
   Native options and hash-verified local resources come from the selected
   profile. Host command tools cannot bypass the OCI bridge.
6. Require the bridge handshake/MCP connection before sending input. Pi and
   Codex verify the configured model/provider during startup. Grants apply to
   every Mizu operation; extra native tools require `engine_tools`.
7. `finish` seals the work unit. Pi additionally requires `agent_settled`,
   Codex requires successful `turn/completed`, and Claude checks terminal_reason.
   A writable role's finish first checks that the workspace can be captured:
   a file above the byte limit, a total above the snapshot bound or a file
   count above the bound refuses the finish and names the offending path and
   size, so the writer can remove or ignore it and finish again in the same
   unit instead of losing the unit to an unnamed publication failure.
8. A writer captures actual files again. Verification is retained as passing
   only when it applies to exactly the captured code. Write a prepared result,
   publish the immutable snapshot pointer, then finalize the run record.
9. Remove active state and reproducible observer input; dispose processes and
   containers. Evidence and model sessions are retained.

Every capture bound names what tripped it: the offending path with its size
against the per-file limit, the running total against the snapshot byte bound,
or the file count against the count bound. The refusal reaches the run record,
the health record and the pause reason, and the next unit of that role reads
the cause from its context, so one stray file no longer costs repeated units
and a silent pause. The recorded cause is the one that still applies: it is
set when a capture fails, kept while it applies, and cleared when a capture
succeeds, so a later unit never reads a resolved cause as the current
blocker.

`current.json` is the publication commit point. Its optional, bounded
`continuations` member carries internal scheduler-intent commit evidence in
that same replacement; see [the continuation registry](scheduling.md#internal-writer-continuation-registry)
for schema, retention roots and single-use Engine acceptance/recovery. A crash
may leave a prepared result with or without publication; inspect the pointer
rather than treating a prepared record as completed. The last published state
remains readable while a writer is active. No rollback of uncommitted files happens automatically.

Atomic replace, staged-data fsync and locks provide local filesystem durability
on POSIX, where the containing directory is also fsynced. Windows gets atomic
replacement and staged-data fsync but no containing-directory fsync or
power-loss guarantee. Use a local filesystem, not NFS/object-store/FUSE mounts
with weaker locking/rename guarantees. Disk/host failure recovery is not a
substitute for independent backups. Snapshots have hashes, not digital signatures.

### Driver quiescence checkpoints and safe-point handoff

`Context.driver_checkpoint` is an optional, host-local `DriverCheckpoint`
(engine, declared unit, execution attempt, cumulative admitted-unit count).
It is not model input, a public tool, a persisted scheduling decision, or a
finish seal. Only managed writer runs where `Context.ephemeral` is false,
returning from successful Claude **query** and Codex **turn** units with no
`engine_tools` grants, can issue one. These units are not physical provider
request counts and are not equivalent across engines.

The driver must first observe its existing successful terminal event, close
its channel (including joined-child cleanup), and exit its authenticated
Bridge (which stops admission and joins host-tool handler threads). Bounded
channel teardown alone is insufficient: the process must be observed exited
and all channel readers stopped. Context also verifies no request timer,
host tool, or joined child remains active. Exceptions, incomplete teardown,
already sealed units and unproven engine-tool paths issue no checkpoint.

Pi and Pi-durable remain **unsupported** at their declared `model_request`
unit. Current whole-agent/submission settlement cannot substitute for an
individual request boundary. Their request-done and engine-tool admission
messages do not establish an idle tool batch or prevent the agent from
starting another request; engine-tool completion is not acknowledged. This
needs a separate proven adapter/upstream boundary, not a coarser counter.

A host caller may consume a token with `Engine._seal_driver_checkpoint` at
this driver-return boundary, before normal continuation. It rechecks
stop/authority/deadline, existing bounds, token identity, attempt/count and
idle state under the Context mutex. A successful one-shot claim latches
**finish-only**. The helper arms exactly one execution of the configured
Claude/Codex driver and one query/turn admission, not the normal `_execute`
missing-finish/deadline retry loop. The admission consumes existing daily, per-run and wrap-up allowances. It
does not extend an existing request, idle or wall timer; the one seal unit
receives only its ordinary per-unit request timer.
All further driver launches and admissions are refused, including after failure.

The real authenticated Bridge advertises only `finish`; host dispatch also
refuses ordinary operations, engine tools and request-admission messages.
The one startup handshake is internal, as are bounded usage/completion
bookkeeping (which cannot admit requests or tools). After finish only that
bookkeeping remains. Role authority and session identity are unchanged, so
existing finish validation, check waits/releases, writable verification and
capture bounds still apply; a claim alone cannot bypass model admission with
a direct finish call. The helper returns the driver evidence only after an
existing durable finish and proven quiescent teardown. It neither chooses
`continue` nor prepares/publishes a continuation. Existing spend-bound finish
normalization remains unchanged. Stop/cancellation, failed or missing seals,
and failed settlement propagate without retry; they never reopen ordinary work
or authorize handoff. The latch is not undone. Tokens are host-local and
one-shot; another dispatch, admission or accepted operation invalidates an
unclaimed token, and restart loses it entirely. The caller retains ownership
of its workspace/slot and responsibility for integrity/publication; this
helper releases neither and publishes nothing.

Cooperative handoff is controlled by `[limits].safe_point_yield_quantum`
(default `0`, range 0–1000); zero leaves preemption unavailable. A nonzero
quantum counts completed supported units, each in its adapter's declared unit
(`query` for Claude, `turn` for Codex), not physical requests and not a
cross-provider equivalent. At a quantum boundary the host takes a bounded,
nonblocking observation of the global admission queue under `queue.lock`; it
requires a live pending-owner lock and an attributable resource row. A busy,
malformed, or ambiguous queue means no demand. The observation never edits a
ticket, takes a slot, reorders the queue, or selects a role; any resumed unit
uses normal FIFO admission. Drain, deadline, stop, authority, and unsupported
driver/tool paths remain outside this trigger.

When demand is observed, the host consumes the checkpoint through exactly one
finish-only seal. The model's chosen outcome is authoritative. Only `continue`
creates a continuation: after the immutable snapshot is created, the exact
intent is prepared before publication, and the existing atomic snapshot
pointer publication commits that intent. A prepared-but-unpublished row is
not runnable and follows strict reconciliation; a failed seal or preparation
never authorizes handoff. Final and wait outcomes are published normally with
no continuation, and ordinary model-selected `continue` remains unchanged.
The default keeps the mechanism inert; enabling it remains an explicit
operator configuration choice, not a role-name or cache-affinity preference.

## Activity evidence

Each runtime context records a schema-1 `activity` aggregate in durable run
records: `result.json` for normally sealed or wall-bounded main runs,
`error.json` for interrupted main runs, and `consultation.json` /
`delegation.json` (or child `error.json`) for child runs. It contains
`elapsed_seconds`, `exclusive_seconds`, aggregate `counts`, and
`max_concurrent_model_requests`, `open_model_requests`, and
`reaped_model_requests`. Exclusive buckets are `model_request`,
`local_exec:exec`, `local_exec:experiment`, `local_exec:verify`, `tool:<operation>`,
`join:consult`, `join:delegate`, and `other`; the applicable bucket precedence is
join, tool, active logical-request span, then other. Thus spans within one
context are not double-counted. Counts are totals by fixed operation kind,
not an event log, and request concurrency is maximum overlap within that
context only. `open_model_requests` identifies active requests at capture;
`reaped_model_requests` counts active spans discarded when retrying after a
transport failure. Count keys are `model_request`, `tool_call:<operation>`,
and `join:<kind>`; they count starts/attempts. Child and parent wall spans can
overlap and are not a global sum.

This is host-observed wall time from context creation through evidence capture,
not CPU time. A `model_request` is the logical interval from Mizu request admission through
completion bookkeeping (or deadline reaping/evidence capture if an
interrupted request remains unclosed); it does not measure or attribute
provider-side wait. Tool spans cover Mizu bridge operations; the three
`local_exec:*` spans cover Mizu exec, experiment, and verification handlers.
Opaque provider work and native tools are not timed. The aggregate is persisted
at run boundaries and does not report global concurrency. A live gauge of the
same buckets is published alongside: whenever the current bucket changes, the
runtime best-effort rewrites `active/<role>.json` with `activity` (the current
bucket), `activity_since` (when the bucket started), and cumulative
`activity_seconds` plus `activity_total_seconds`. The gauge preserves the
admission fields (`run`, `role`, `started_at`) and never overwrites a record
whose run id no longer matches, so a superseded write cannot clobber a newer
run. Explain surfaces the gauge read-only in the `active` block; `yieldability`
remains `unknown` because the gauge is an observation, not a scheduler verdict.

## State and history

A snapshot ties together goal, compact state, code digest, verification evidence,
summary, outcome, run ID and wake conditions. Code objects are content-addressed
and deduplicated. Files are byte-preserving; executable status participates in
the code digest. Symlinks, hardlinks and special files are not materialized as
ordinary source. Plain-directory import refuses unsupported skipped entries;
verification cannot pass if the captured tree contains them.

Read-only `Project.status()` separates immutable snapshot `verification` from
`retained_verification`: one validated acceptance receipt for that code digest,
with `applicability: "unassessed"`. For acceptance it reads existing per-code scope
indexes (lexical selection, not a latest verdict) and the selected receipt; it
neither fingerprints configured mounts/cache inputs nor takes `cache_guard`.
No new selector, migration or acceptance write is required for existing late
proofs. Status leaves `effective_verification` null with an explicit
`unassessed`/`read_only_status` diagnostic. `acceptance.effective_projection`
remains the fresh scope/fingerprint path for execution/reuse, unit prompts,
selector evidence and completion; retained status evidence cannot authorize
those decisions. Index/receipt code and scope bindings and safe paths fail
closed, while whole-store audit and existing crash recovery remain authoritative.
See [check jobs](check-jobs.md) for the status and effective-proof schemas.

Snapshot retention is storage-owned policy, not a publication side effect.
At the existing per-project automatic-prune boundary, `snapshot_retention_days`
(default 31; 0 disables) permits removal of old superseded history generations
and unreferenced manifests; no timer or pause is added. Current history,
continuations/fences, runs, artifacts, pending
editor submissions/claims, and every historical work-item delivery/done citation
remain roots. Recent superseded histories also pin their IDs until expiry.
Generation age uses immutable file mtime; manifest age uses validated
`created_at`, strictly older than cutoff. Every reference root and recent manifest
also retains up to `history_index` parent edges for merge, using a cycle-safe,
nonrecursive traversal. Dependency-only ancestors do not restart the bound;
missing/invalid required parents refuse deletion, never count as protected.
Observed/session delta cursors are not retention roots. Objects survive while any retained
manifest uses them. Manual storage accounting/reclaim keeps its preview schema,
all-history roots and pause requirement.

The existing collector validates all generations, including expired ones, before
allowing deletion; incomplete scans fail closed. Automatic GC takes the existing
workspace/role/editor/record quiescence exclusions nonblocking, plus publication,
all check-slot exclusions and the editor outbox submission lock. Check runners
capture content outside role units, so even never-used configured slots must be
excluded. CLI submit/revise/report/export take publication before observing a
snapshot and hold it through storing its citation/export. Capsule writers have
only the outbox mount and serialize submissions on `.submit.lock`; no project
access is added. These writers acquire nonblocking and refuse as Busy on
contention before observing/storing a reference; callers can retry later (CLI
exit 75, editor tool error), rather than waiting for a maintenance scan. Publication precedes record writes. The
acceptance audit also opts into nonblocking locking for this sweep. There is no
new snapshot-write locking layer or changed publication authorization. Unlink
histories before manifests before unused objects; interruption leaves extra data
for a later rescan, not deleted retained dependencies. A bounded result joins
`maintenance/automatic-prune.json`; GC failures never fail requested work. Run
and shared-smoke retention are independent and unchanged. See
[configuration](configuration.md#automatic-snapshot-retention) for the exact
root/age/result contract and outbox lock permissions.

Work orders keep one bounded canonical `work-order/current.json` record, atomically replaced under `records.lock` with exact revision/generation CAS. Each committed revision receives a distinct immutable `work-order-<sha256>.json` receipt under the authoring run and existing run retention; the tool result names its exact receipt. Legacy history/checkpoint files are unused and left untouched; valid schema-1 current records remain readable. There is no recovery scan, cleanup or automatic retry. See the writer work-order contract in `configuration.md` for schema, bounds and failure behavior.

The history index retains published IDs up to the configured limit (default
128, tunable 8–1000000 via `limits.history_index`). The active generation/window
is always retained; older storage follows reference-aware retention below. Prompts carry a bounded, capability-gated projection: up to
`limits.prompt_snapshots` recent anchored summaries with exact IDs and code
digests, plus only the actionable context the role is granted to use.
`work_order` freshness (`absent`/`current`/`review_due`/`unavailable`) is offered only to roles with `work_order`, sharing the unit-frozen full pending-inventory generation across full and delta even when a delta omits already-offered bodies;
`acceptance_commands` only to roles with `verify`. Exact snapshot references
(`id`, `code_digest`, `state`, `verification`) are always pinned, never
summarized. `diff` compares the anchored
snapshot with the role's last observed snapshot when that cursor is still
retained and ancestral (naming it as `base_snapshot`, empty when the cursor
already names the anchor), otherwise with the preceding distinct code digest. Large and binary files are
listed rather than inserted into a huge diff. This is bounded context, not a
vector database or an automatically complete history search. Usage day buckets
and the usage window edge use the configured `timezone` day boundary so
`mizu usage` groups agree with `mizu budget` for the same day.

`continue` starts another meaningful unit. `bounded` is a `continue` that hit a per-run spend bound or entered finite-wall wrap-up before sealing: it saved what it could under the wrap-up allowance and runs again without counting toward `max_failures`. A host-sealed bounded yield may retain role-eligible ready work; it is not a writer-selected parking request. The items remain available for a later independent admission, but unchanged readiness alone does not bypass the no-progress self-admission policy. Writer-selected `wait` and `blocked` finishes are accepted with role-eligible ready work: the host keeps the delivered ready-set signature, so the row stays visible but an unchanged set is not a wake, even when every ready row is this holder's own future deadline. Repeated unchanged units count toward no-progress recovery and park until the set changes or an independent wake arrives, without requiring any item transition. A writer-selected `continue` or `bounded` finish still requires a complete empty ready inventory unless it hands off through a validated progress-backed `next_step`. A whole-unit wall exhausted or deadline wrap-up ended without a seal instead
records a bounded run result, without creating or publishing a snapshot or
counting toward `max_failures`. Unrelated pre-seal faults still fail even if the
wall also expires.
`wait` sleeps until its deadline,
new information, a changed goal, a human wake, or a cached current CI failure
on the effective configured wake set (`ci_branch` plus deduplicated
`ci_wake_extra_branches`; an empty set disables the wake) for roles with
`ci_wake` (writable roles by default; an unchanged red re-wakes only on a new
branch/SHA/check/run fact, a moved head, or every `ci_rewake_seconds`). Failures
remain visible independently of role judgements until green or superseded.
`blocked` needs a wake or new information. `done` is not reopened by news
alone; an explicit wake or changed goal can reopen it. Other scheduled roles
are independent until the project is paused/disarmed. Projected red CI checks
may carry `nondeterministic: true` and one validated `passed_on` branch when
bounded success evidence matches the same SHA/check. This is informational: the failure
stays red and affects no gates or retries; missing annotation is not proof of
determinism because evidence is bounded.

Writable `continue` and `bounded` snapshots also carry a compact, digest-bound
progress record. The effective per-role `progress_signals` list (`roles.<name>.progress_signals`
when set, else `[limits] progress_signals`) selects which observed signals
qualify; any selected true signal is progress. The default selects successful
non-finish Mizu operations except pure reads of existing workspace, proposal,
research, VCS or retained command/experiment evidence and exact web-receipt
replays; new native engine-tool authorizations (not idempotent replays), changed
code, successful queue writes and work-item transitions, and fresh/due one-shot input
also qualify. The free-text `state` signal remains opt-in; a paraphrase alone is
not progress under the default. Input means a changed inbox or operator
wake generation, consumed decision/wait events, CI failures due for this
snapshot, or an expired prior wait. Persistent pending obligations and cached
red CI presence do not count repeatedly: pending obligations are fingerprinted
by stable identity, so a newly offered obligation counts once while unchanged
visibility does not reset the streak. Finish alone is
not progress. The default `no_progress_continuations = 0` therefore parks after
the first `continue`/`bounded` unit without a selected progress signal. Every
later unit with the same input observation advances the bounded streak whatever
its finish outcome; progress or a changed observation, including a changed
ready-work signature, starts a new streak. A
missing-finish continuation may still use the full work grant when no tool was
called; otherwise it remains finish-only. Operators may allow 0–100
consecutive no-progress continuations for read-only on-change roles; writers do
not self-admit from that allowance or elapsed no-progress backoff. Writer
continuations require a validated progress-backed `next_step` or an independent wake.
For read-only roles, `no_progress_backoff_seconds = 0` disables the timer wake;
a positive value permits a parked on-change cursor to wake after that delay
measured from its recorded progress timestamp. Writers inherit
`writer_no_progress_backoff_seconds = 300` as their `no_progress_backoff_seconds`
default unless a per-role override applies, but the writer self-resolvable-wait
timer fallback was removed: a parked writer is not re-admitted by a timer.
Holder-only waits surface as open ready work and progress-backed continuations
require a validated `next_step` handoff; neither changes the daemon poll cadence
or bypasses other gates. `no_progress_recovery_after = 2`
rotates a persistent writer conversation to a fresh generation on the next
independently admitted dispatch after two unchanged-input empty writer
snapshots with a `wait`, `blocked`, `continue` or `bounded` outcome; it does not
admit or schedule a run. `mizu status` exposes whether that recovery threshold
is due and the count, so parked empty work is visible while awaiting its next
trigger; these fields are evidence, not a scheduler verdict.
Writer-daemon readiness checks goal and operator generations, role-eligible
ready work, and retained work-item CI wait evidence (including
snapshot-bound waits resolved to an exact receipt/SHA) for the current blocked
holder before the saved outcome; inbox generations, routed
decision/terminal work-item events, due CI, waits, and wait expiry then precede
progress self-admission. Ready work is role-scoped: a named role needs
`work_items_transition` and may claim an unheld `open` item or continue its own
held `doing`/due `blocked` item. Other roles’ held work does not wake it. This
ledger authority is independent of workspace mode and does not grant source
write or publication authority. Eligible work overrides a prior `done`, `wait`
or no-progress park and appears in the bounded `ready_work` prompt projection
with its ledger age. The no-progress park applies only when no eligible ready
item exists, and writer-daemon cooldown still applies. Read-only on-change
roles use per-role completion cursors for progress and one-shot operator wakes;
they do not inherit publisher inbox/goal/run-wait generations or its progress
record. Event-triggered dispatch admits role-routed decision/work-item/wait/CI
events, eligible ready work, a role-cursored read-only operator wake, or an
unobserved code change where subscribed; generic snapshot wakes and
progress-only continuation remain outside the event probe. See
[scheduling](scheduling.md) for the complete ordering.

Readiness reads current work-item state from the ledger index (below), so
a poll costs one indexed query for the blocked items plus the route-owned CI
and proposal evidence reads; it replays no history and reopens no manifest.
Legacy snapshots without progress keep their previous admission behavior.
Schema 1 records the six observed booleans,
the selected signal names, role ID, aggregate progress boolean, saturated
no-progress count (0–101), and a timezone-aware timestamp. The snapshot digest
binds the record. A malformed progress object makes the snapshot unavailable
rather than authorizing an admission; `mizu status` and `mizu explain` expose
only its bounded signal/count/timestamp projection and a finite disposition,
not a scheduler prediction.

## Current-state index

The revision files are the durable, append-only history. Current state is read
only from the per-project ledger index `ledger.sqlite3` in the project root
(stdlib SQLite, WAL journal, `synchronous=FULL`, connections opened per
operation with the configured lock timeout as busy timeout). Its `items` table
holds one row per seeded item: ID, revision, status, holder, creation and
update order keys, opaque origin, wait kind, the derived doing
start and blocked start, and the complete current record. `meta` holds the schema version and whether
the build completed; `intents` holds announced writes. Page, count, readiness,
closed, event and wait reads are indexed queries; origin staleness, hold ages
and wait liveness are still computed from the current record on every read.

Every commit of a work-item record (direct creation, transition, wait
resolution, retirement) runs inside
`WorkItems.committing(ids)` under the record lock: it commits an intent row,
the caller writes its file, and the named items are then replayed once from
their files, with full chain validation, into their rows while the intents are
removed in the same transaction. Invariant: an item without an intent row has
the row its committed files replay to. A crash between the steps leaves the
intent; the next locked use replays exactly those items, and a reader that
sees an intent replays them into a private in-memory copy, so no read returns
a state no committed revision had. A missing database, an older schema or an
interrupted build is rebuilt from the files by the next process that holds the
record lock (installation needs no step), committing item by item so an
interrupted build resumes; readers meanwhile build a private in-memory index
and never write the file. The index is derived: backups exclude it and a
restored project rebuilds it on first locked use.

The CLI's separate incremental current-projection mode adds one indexed
`change_seq` to each current item row and one singleton `item_changes` row
(counter and cursor authentication key). Replacing a changed row advances the
counter in the same transaction as the row replacement and intent removal;
no-op replay preserves its position. There is no extra event journal or
per-reader state. Each build creates a new authentication key, invalidating
prior cursors; an unexpected removal of an indexed item also invalidates them
rather than hiding a deletion. Current source items remain retained.

Change pages require a complete persistent items domain with no pending item
intents, checked in the same read transaction as the cursor and range query.
They refuse private repairs/builds: their positions could otherwise disagree
with the next durable replay. A crash before/after source-file commit leaves
an intent and refuses a change read until locked repair; a crash during index
replacement rolls back the position and row together. Committed positions and
keys survive process restart. A fixed sequence high-water mark plus a last
position pages the current-row change index in bounded queries. Updates moving
unvisited rows past that mark are not lost: a completed sequence checkpoints
only its original mark, and the next sequence selects those newer positions.
This is coalesced current state, not an immutable snapshot across requests or
a promise to deliver every intermediate revision. See the CLI response/retry
contract in [operations](operations.md).

Full-chain validation runs when a record is written, when the index is built
and in `WorkItems.audit` (`mizu work-items PROJECT --audit`), which holds the
record lock, replays every chain in the strictest mode (every delivery
manifest, retained receipt and stored wait evidence) and compares the result
with the index. It reports corrupt chains and divergent rows without changing
anything; `--repair` rebuilds the index when every chain validates. Reads
never revalidate history.

Direct creation calls a lock-held `WorkItems.create_seed` preflight: title,
optional description and scope, opaque origin validation, safe empty item slot
and exact ID binding. New seeds recover a recognized regular orphan temporary
(removed without promotion) and then refuse any pre-existing direct record,
imported sidecar, numeric history, unknown entry, or unsafe/ambiguous temporary.
Preflight writes no seed; direct creation commits `create.json`. Committed
retries use a shared storage/chain validator.

Mutations share `locks/records.lock` with seed creation. A positive non-bool expected revision must match against a full
replay of the item's chain; each change exclusively atomically appends the
full state to `work-items/<item-id>/<next-rev>.json` and updates its index row.
Reconstruction requires consecutive revisions, with at most 256 revisions
including the seed and 128 KiB per seed/event record. Every revision states the
scope in force for the item at that revision: a revision that supplies one sets
it from that revision on, a revision that omits one inherits the previous
revision's scope, and a scope is never cleared. A revision that supplies a
different scope is that item's own scope-change event, visible in its history
and in by-ID reads; it carries a reason naming what it supersedes, and it may
stand at the same status, so a scope change is recorded as an event rather
than absorbed into a closure. A `done` closure may not change the scope it
inherits: it is refused unless it states the same scope, and the change must
be recorded in an earlier transition. An item whose seed has no scope must record any scope before binding delivery;
a new closure cannot introduce a scope. Older unmarked history remains readable. The compact page rows
carry the scope in force as a bounded preview. Unsafe links, unknown IDs,
malformed/oversized records, gaps and exhausted bounds fail closed; a stale
CAS writes nothing and must be re-read, not blindly retried. Writes and index
builds recover under the shared lock by removing only recognized
regular `.new-<8-char>` atomic-write leftovers; an uncommitted temporary is
never promoted, and a post-link alias
is removed only when it matches one committed revision, direct creation record,
or legacy sidecar inode. Unknown names and unsafe
links still fail closed. Delivery is an explicit same-status `doing` revision carrying `snapshot_id`
with unchanged accepted scope and normal revision CAS. The binding event stores
`snapshot_id` as null and persists the delivery object; `snapshot_id` is refused
for other statuses.
It validates the exact immutable manifest and retained published membership
under the snapshot store's `locks/publication.lock`
then `records.lock`, holding both through append. `Snapshots.publish` takes only
the publication lock, never the records lock. Thus eviction either precedes the
binding and refuses it, or follows a committed binding.

New transition records carry `delivery_schema: 1`, `delivery` (null before a
binding), and `verification_scope` (null except for retained acceptance).
A delivery holds exactly `snapshot_id`, `code_digest`, `scope_digest` (SHA-256
of canonical accepted scope, or null) and `binding_rev`. Every subsequent
revision inherits it, unless an explicit later doing-to-doing revision binds a
new snapshot. Scope changes retain the old binding but require re-binding
before snapshot-contract completion. The schema cannot disappear; removing a binding's fields
cannot turn that same-status event into a legacy no-op. Older unmarked chains
and closures remain readable without rewriting them.

Under the default `verified_snapshot_v1` contract, new completion requires a
prior binding, exactly its `snapshot_id` and scope,
and revalidates its immutable manifest. It does not capture or compare the live
workspace, nor require that an already-bound snapshot remain in the moving
history index. `delivered_tree` is the bound snapshot code digest. Ordinary
completion copies valid embedded passing digest-bound verification, or uses
`Project.effective_verification(snapshot)` to select late retained acceptance.
The latter stores exactly `{source: "retained_acceptance", receipt_id, passed,
code_digest}` and separately pins the receipt's canonical acceptance-scope
digest in `verification_scope`. Replay calls `acceptance.get` on that exact
immutable receipt ID and validates code and scope, without reselecting through
current indexes or configuration. Snapshots are never mutated. Storage audit
and reclamation retain all historical delivery manifests, including bindings
on still-doing or dropped items and superseded bindings needed for replay.

Ordinary transitions permit only a forward `dropped` -> `done` terminal correction. Under
`verified_snapshot_v1` it requires a previously recorded valid delivery and
independently retained acceptance,
even if the snapshot already embeds passing verification. It appends a new
revision and leaves the dropped event intact. Done-to-open instead requires
the separately authorized audited reopening operation described below. The
binding is an attributed writer declaration of delivery for the accepted scope, not a semantic proof of arbitrary source changes. Missing,
unsafe, corrupt or mismatched evidence refuses; lock/I/O errors propagate.
Cancellation/process death releases kernel locks; stale CAS writes nothing and
requires a fresh read, never an automatic retry.

Work-item completion is a versioned policy, separate from the writer
`mizu_finish(outcome="done")` verification gate. Root `completion_evidence`
selects `verified_snapshot_v1` by default, with overrides resolved only by the
runtime-authenticated closing actor's role. The caller cannot select a policy.
The opt-in `holder_attestation_v1` instead requires a bounded inline `evidence`
object with a nonblank string `statement`; other finite JSON fields are kept
verbatim. Ordinary done requires the current holder. Dropped-to-done requires
a nonempty reason and attributes responsibility to the correcting actor.
Attestation does not invent a snapshot or verification: `snapshot_id`,
`verification`, `delivered_tree`, and `verification_scope` are null. Inherited
`delivery` stays as history, not proof; completion skips snapshot verification
callbacks. The strict contract rejects non-null inline evidence.

New done revisions record `completion_policy` and `completion_evidence`.
Both delivery and completion replay dispatch on that recorded policy, never
current configuration; legacy records without it replay strictly unchanged.
Attestation replay checks finite JSON, string keys, depth 32, a fixed durable
65536-byte canonical format ceiling, holder/correction attribution and null
snapshot fields. Writes additionally enforce the configured attachment budget
(default 8192 bytes). Lowering that budget cannot invalidate accepted history.
Unknown contracts and malformed evidence fail closed, with contract/condition
errors that do not echo payloads.

`work_items_reopen` is a separate default-disabled capability/tool. The runtime
requires both that explicit role capability and the authenticated actor in
`completion_evidence.reopen_roles` (default empty); actor/policy cannot be
supplied by the caller. The store's `reopen(id, *, expected_rev, reason, actor)`
uses the existing lock/CAS/index append path. It accepts only current done at
revision <= 252 and a nonblank 1–4000-character reason. Above that cutoff it
refuses for revision headroom: the four strict reopen/claim/bind/close appends
must fit under the unchanged revision-256 ceiling.

The immutable full-state open revision preserves identity/title/origin/
description/creation time/scope and delivery schema, but clears holder, wait,
blocked start, active delivery and all snapshot/completion proof fields.
Its bounded `reopen` marker binds `challenged_rev`, `challenger`, and the digest
of the canonical event reason. Replay permits only done-to-open with this exact
marker and cleared state, never consults mutable policy, and rejects markers
on other transitions. Later transitions do not inherit the marker. Historical
strict and attested closures retain their recorded contracts and delivery
manifests remain auditable; old evidence is historical only, not new proof.
Open/role-ready projections carry the challenge reason. Unresolved object
waits follow the new pending head; recorded resolution proofs continue to
validate against the historical revision. Terminal receipts bind item/revision,
so the old acknowledgement cannot cover a later completion, which is delivered
anew. No verification is synthesized and attestation is not marked verified.
This is not same-ID generation rollover: revision/history are never reset.
Writer finish(done) verification, seals, acknowledgements, admission and parking
are unchanged.

The separately granted `work_items_create` operation calls
`Project.work_items.create(*, idempotency_key, title, actor, origin=None)` for
self-directed work and splits. The runtime binds `actor` to the role, never a
model argument. The example grants it only to `worker`; consultation and
delegation children explicitly forbid it. Creation itself does not assign work or publish; the independent writer readiness gate may
admit a run for role-eligible ready work, as described in
[scheduling](scheduling.md); a read-only role needs the separate
`work_items_transition` capability and still cannot edit source through its
workspace. The
work-item ledger is the single record of accepted and planned work, including
blocked conditions; the finish outcome and structured wake source record why a
unit stopped, while prose `state` is a short working note and typed `next_step` proposes an
immediate handoff, not a second backlog.

Required arguments are `idempotency_key` (1–128 characters) and `title` (1–200).
Optional `origin` is an opaque nonblank string of at most 512 characters, or
null. Stored seeds may carry an older small JSON object, which is replayed as
written and never interpreted. A local I/O error propagates; the origin is
provenance only and grants no routing, lifecycle or dependency semantics.

The actor-scoped ID is `wi_` plus the first 32 hex characters of SHA-256 over
canonical JSON `["direct-work-item", actor, idempotency_key]`. The immutable
`work-items/<id>/create.json` contains exactly `kind: "direct"`, `actor`,
`idempotency_key`, and `work_item` (the revision-1 seed). Direct creation uses the shared seed constructor and shape validator.
A seed without `description` retains the exact v1 field set;
a supplied `description` adds only that field (v2), with no version marker.
Descriptions must be strings of 1–4000 characters containing non-whitespace;
whitespace is preserved, and explicit null is invalid. Both seed shapes are
accepted on reads/replay. New transitions preserve their seed's field set: v1 never gains a description,
and v2 must retain exactly its immutable text, rejecting change or removal.
No migration or rewrite occurs.

First writes return `{record, created: true}`. Exact retries return the original
creation `{record, created: false}`, not the current transitioned item; a changed
title, origin, description text or description presence with the same actor/key is denied. Retry comparison precedes further validation so the immutable seed stays stable.
Callers must retain the key and exact request across cancellation or uncertain
responses; there is no automatic retry. Atomic exclusive writes commit the seed
before returning; a pre-commit temporary is never promoted. Replays validate
the committed chain and do not count as tool progress. Direct items use the
same open/closed pages, full/delta prompt inventory, by-ID reads, CAS/history,
256-revision and record/page byte bounds, done-verification gates, event receipts
and backup/restore tree as imported items.

By-ID reads and history retain the full description. Compact open/closed rows,
full/delta prompt inventories, and terminal event payloads include only
`description_preview` (at most 1024 UTF-8 bytes) and `description_truncated`
when the seed carries a description; both are absent for v1 seeds. Open-work
text previews may be shortened further when the new hold-age fields would
otherwise exceed the fixed row bound; each shortened preview remains explicitly
marked truncated. Descriptions are untrusted recorded content, not instructions
or authority. Preview clipping never raises the 8192-canonical-byte row/event
or 1-MiB response bounds; escaped JSON and all other fields still count, and
excess remaining after permitted preview clipping fails closed.

Terminal routing by origin is unknown: origins are opaque, and routing never
falls back to the creator, holder or transition actor. Exact receipts identify
each terminal item/revision and are routed by the configured role subscription.
The operator configures which role subscribes and on what schedule; a terminal
event admits focused work to that subscriber, and the submitting role takes its
topic back at its next scheduled run when nothing resolved it.
Keeping scheduled resolution opt-in avoids routine polling when an explicit wake can admit the same work.

Statuses are `open`, `doing`, `blocked`, `done`, `dropped`. Same-status changes
require a scope change or an explicit doing-to-doing delivery binding. Done
reopens only through the separate opt-in audited operation; dropped permits only the forward done correction
described above. Other statuses can move to any different status, subject to
completion evidence and the ordinary constraints. Title/origin/description/creation time stay fixed; every event
records its revision, update time, caller-supplied actor, from/to transition,
and explicit reason/wait/snapshot/verification fields (null when absent).
Actor is trusted internal attribution, not a new authorization grant. `doing`
assigns the actor as holder, `blocked` preserves the holder or assigns the
actor, and other statuses clear it. New transitions to `blocked` require a
supported structured wait: a deadline, code-change or exact dependency-result wait,
a work-item-only `object_resolved` wait on a work item (unknown evidence stays
unknown), or a `ci` wait pinned to a completed immutable publication
receipt. Historical blocked records without waits remain readable and are never
rewritten or released automatically. These waits never assign work. Under configured wait-resolution policy,
proven object/code-change waits may reopen only their holder with immutable
proof; read-only liveness itself never changes status or the target object. The CI wait wakes only
its current holder on retained terminal evidence, without changing item status.
Dropping requires a nonempty reason. Completion validates and records its
resolved contract as described above; the default snapshot contract keeps its
exact delivery and digest-bound verification requirements.
Backup/restore includes these histories; storage audit/reclaim protects every
historical delivery and completion snapshot manifest and every validated host
publisher-outcome snapshot citation. Work-item expiration and finite host-record
retention are not implemented, so these citations remain durable.

Terminal `done`/`dropped` transitions can wake roles explicitly subscribed via
`work_item_events` (default disabled). The event adapter reuses these same seed
and full-chain validators at write time and records the validated payload in a
bounded pending-event index; reads use that index and never replay terminal
chains or enumerate receipts. A one-row terminal-change marker in the work-item
ledger domain pins the index to the latest terminal head change, so reads check
freshness in constant cost and fail closed on a stale or malformed index. New
project roles are seeded at project start or settings refresh from validated
terminal history and exact receipts. Per-role exact
terminal-identity receipts live outside the immutable work-item records and
outside decision timestamp cursors. Each delivered terminal row has a
role/payload-bound `ack_token`; only the tokens explicitly selected in
`finish.ack_events` are acknowledged after a durable successful result and,
for writers, publication. Empty/omitted selections acknowledge none, regardless
of outcome or check wait. Tokens are validated before finish intent and never
enter the existing terminal receipt shape. The terminal batch uses the role-effective
`work_item_events_limit` (role override when set, else the project default),
independent of the decision-event batch; both stay under the fixed 100-event
mechanism cap. Inbox/archive origin submitter routing never
substitutes the transition actor. Terminal appends and successful/partial receipt writes touch the work-items
parent metadata as a best-effort non-recursive platform wake hint; this is not
event storage, assignment, or an admission decision. See
[configuration](configuration.md#terminal-work-item-subscriptions) for bounds,
unknown routing, acknowledgement and crash/retry behavior.

The store provides internal `get(item_id)` plus the locked readers
`page_open(cursor=None, limit=25)`, `projection(item_id)` and
`explain(item_id, *, after_rev=0, limit=25)`, the unlocked, non-recovering
read-only `page_closed(cursor=None, limit=25, status=None, origin=None,
closed_after=None, closed_before=None)`, and CAS transitions; the runtime exposes the three
page modes through the single `work_items_read` grant and exposes transitions only through their
separately granted operations described below. `get()` stays internal-only. Origins are opaque;
no reader falls back to a creator, holder or transition actor on routing.

`page_open(cursor=None, limit=25)` is the bounded page reader used by the
explicitly granted `work_items_read` operation and by the project-wide
`open_work_items` prompt projection. Full and delta prompts use the same
bounded page, capped by `limits.work_items_prompt` (default 20, range 1–100).
Roles holding a work-item capability receive it by default;
`roles.<name>.context_sections` can include or exclude it. The runtime calls
the fail-closed reader directly,
so store errors are not converted into empty inventory. The explicitly granted
read-only tool defaults to 25 rows and accepts an optional opaque cursor (at
most 128 characters) and limit (1–100). Local recorded content may be
untrusted. Repeat reads are fresh live views, not frozen snapshots; a
continuation uses `next_cursor`, and concurrent changes can affect membership.
The operation is recorded in normal per-run tool evidence. It delegates to the
shared-lock page reader: its only store writes are the recognized local
atomic-write recovery cleanup described below, not work-item transitions.
Failure and cancellation retain the existing store lock/read behavior; the tool
adds no retry or cancellation handling.

The same explicitly granted `work_items_read` operation also accepts an exact
seeded `id` to read an item in any current status, including terminal `done` and
`dropped`. This mode cannot be combined with open-page `cursor`; `after_rev`
is exclusive and requires `id` (exact integer 0–256, default 0), while `limit`
is 1–100 (default 25). It returns the live immutable current item plus the
same current-origin staleness projection described above, and an ascending page
of the existing full-state records, starting with seed revision 1. Thus each
record retains its actual reason, structured wait, or completion snapshot and
verification evidence; the reader does not synthesize a narrative. `history_total`
is the current revision, `cursor` echoes `after_rev`, and `next_cursor` is the
last returned revision only when more records remain. History is append-only,
but each call replays the complete chain and reports a live current projection,
not a frozen snapshot. The complete response is at most 1 MiB and the page is
bounded by 100 rows (each full row uses the existing 128 KiB bounded record
reader). On reaching the byte bound the reader stops retaining contiguous page
rows but continues replaying and validating the chain/current status. It retains
only the current record and bounded page under the existing maximum of 256
revisions. This read uses recognized temporary recovery under
`records.lock`, preserves fail-closed seed/chain validation, and remains
observational: it does not account progress or affect scheduling/wakeup.

The same `work_items_read` grant selects a third mode with `closed=true`:
`WorkItems.page_closed(cursor=None, limit=25, status=None, origin=None,
closed_after=None, closed_before=None)` pages seeded terminal items (`done` and
`dropped`) ordered by status, then newest `updated_at` first, then ascending ID.
The exclusive `cursor` carries the last row's status, update-order key and ID
so a caller reaches the whole set. `status` (done or dropped), `origin` (exact opaque text), and the timezone-aware `closed_after` / `closed_before` timestamps
narrow matches but never cap `total`, which counts all rows after filters.
Rows and pages are byte-bounded; malformed cursors, filters or committed rows
fail closed. Unlike the open-page and by-ID readers this mode reads with an
unlocked, non-recovering store view: it neither acquires `records.lock` nor
performs atomic-write recovery cleanup. It cannot combine with by-ID `id` or
`after_rev`, and closed filters require `closed=true`. The response is a fresh
live view, not a frozen snapshot, and remains observational.

The separately granted `work_items_transition` operation exposes only
`Project.work_items.transition`: required `id`, positive `expected_rev`, and
status (`open`, `doing`, `blocked`, `done`, `dropped`), with optional nonempty
reason (1–4000 characters), structured wait (required for new blocked
transitions), accepted scope, and 64-character lowercase digest `snapshot_id` (status-dependent: delivery binding on `doing`,
exact prior binding on `done` under the default contract), and optional inline
`evidence` for configured holder attestation. The configured worker
example role alone holds this grant; `work_items_read` does not imply it, and
consultation/delegation roles are explicitly forbidden from holding it. The
runtime binds `actor` to the role name and accepts no model-supplied actor. It
returns the store's bounded record and preserves store errors. A stale CAS
refuses without writing: re-read the fresh work-item projection and use its
revision before retrying. First record delivery with `snapshot_id`
on a doing-to-doing transition with unchanged scope (the binding event stores
`snapshot_id` as null); then close with
`snapshot_id` exactly equal to that binding and valid embedded or retained
acceptance proof. The live workspace may have advanced independently.
`dropped` requires a reason, and new `blocked` transitions require the same
deadline, code-change or exact dependency-result wait, a work-item-only
object wait (`{kind:"object_resolved", subject}` with `{type:"work_item",id}`),
or a work-item-only CI wait (`{kind:"ci", publication_id, checks}` or
`{kind:"publication_ci", snapshot_id, checks}`). Historical blocked
records without waits, and retired object subjects in stored history,
remain readable and are not rewritten. New object waits require validated
`done`/`dropped` target history. They reject self/cyclic work-item edges and
fail closed on uncertain traversal; missing is impossible only when established. CI wait results use
the host event path only to wake the holder; they never change item status.
Receipt-ID CI waits
validate a completed immutable publication receipt at registration and on stored reads,
binding the recorded branch and published SHA; absent or uncertain receipts
are refused. The snapshot-bound form instead derives the immutable digest and
pins `vcs.ci_branch` before a receipt exists; the host requires a unique validated
matching receipt before querying its exact SHA. Unknown or ambiguous evidence
remains pending, never a refusal/supersession inference. See [exact-SHA CI waits](scheduling.md#exact-sha-work-item-ci-waits)
for terminal evidence, holder wake and acknowledgement semantics. This mutating operation is not observational
and receives no exception to the existing per-run or wrap-up spend bounds.

### Requesting CI at an exact published commit

CI usually starts from a push, which spends a run per publication whether the
work needs one or not. `vcs_request` lets a role ask for one run per workflow
at one exact published commit instead. The target is a completed publication
receipt, never a branch head, and the product verifies that every run the
provider reports names that commit, so a run at another commit is refused
rather than accepted as close enough. The request and the returned run
identities are recorded durably, and a dispatch marker is written before the
provider is called. The adapter request carries only the SHA and workflow
set; returned run IDs are stored under the durable request locally but are not
provider-attested as its outcome. An exact-SHA status row may prove a check
result but cannot resolve an uncertain dispatch, identify which request created
a run, or prove that no late run can appear. An
unresolved marker remains non-retriable, and an empty status observation is
not evidence of ``not-dispatched``. Any future reconciliation must use a
provider-independent adapter contract that binds run identity to the exact
request and provides authoritative terminal no-dispatch evidence after a
documented bounded condition that rules out late creation; adapters unable
to provide those guarantees cannot offer reconciliation. A request may cover
work items: each
covered item names the revision it is covered at and the checks whose result
settles it, and the product registers a receipt-pinned CI wait on it, so the
item waits for the request's result and its holder keeps the completion
decision. The role capability and the operator's
`request_grant` are both required, so the path exists without anything
triggering it until the operator grants them.

### Capability-based publication review

Review authority comes from the fixed `review_verdict` role capability, not a
role-name convention. Holders are read-only roles with `finish`, without
`delegate` or `vcs_publish`; consultants and delegates cannot seal verdicts.
Every holder's `on_change` completion validates a typed receipt for the reviewed
code digest and host-bound role/run identity, keeping the reviewed input snapshot
as evidence.
`publication.reviewer_role` is policy selecting which holder's observed cursor
publication consumes; it neither scans other holders nor falls back to older
approvals. Config load automatically confers the capability on that selected
role for compatibility, then validates every holder. Defaults grant/select
nobody and refuse publication. A selected holder's typed `approve` receipt,
code-digest binding, and the independent human `GO <branch>` bound to
the code digest are all required; findings and prose never authorize publication.

### Externally recorded publication receipts

The product's own publish path records a schema-3 receipt in
`evidence/publications/` as `<publication_id>.intent.json`,
`<publication_id>.dispatch.json` and `<publication_id>.receipt.json`. An
operator-run publisher that pushes approved commits to the development branch
outside the product leaves no such record, so its publications had no durable
source-to-published identity.

A publication by such a publisher may be recorded afterwards as a schema-3
external receipt: one create-only file
`evidence/publications/<publication_id>.external.json` holding exactly the
operation `external`, the branch, the published SHA, the whole-capture code
digest, the versioned public-source projection (schema/algorithm/digest), the
approval reference, the optional source commit and run reference, and the
recording time. Its identity is the digest of the operation, branch, code
digest, published SHA and projection identity, so one publication keeps one ID
across recordings. Projection-less writes are refused; schema-1/2 records
remain readable as immutable legacy history (ADR-049).
The write is create-only and idempotent: an identical request replays the
stored receipt unchanged, a request that disagrees about the approval,
source commit or run reference is refused by naming the conflicting
publication ID, and nothing is ever overwritten.

Trust boundary: the receipt is operator-asserted evidence. The product never
infers a published SHA from a branch head, and a recorded receipt does not by
itself prove that CI passed; CI evidence must still confirm the exact SHA the
receipt names. A role reads recorded receipts from the project's own durable
evidence through the `vcs_read` op `publications`, which contacts no provider
and returns one bounded page of at most 100 summaries, newest first by recorded
time (then publication ID), filtered by exact code digest, branch or published
SHA, with the match count across all pages, a `truncated` flag, a `next_cursor`
to continue after the last row and a `scan_stopped` flag so a caller can tell an
absent receipt from one the scan never reached. An unreadable,
oversized or invalid record is skipped, never returned unvalidated, and it
never denies the valid records beside it. A narrow page reads the writer-owned
key index as a complete inventory instead: its schema-3 entries carry the
ordering instant and exact filter fields of validated receipts, one bounded
fixed-path read supplies the page order, filters and match count, and only that
page's receipts are revalidated from bytes and checked against the metadata,
so continuations and exact filters reach receipts beyond a stopped scan. A
wider request is capped by both budgets and returns a shorter page with a real
cursor. A missing, stale, incomplete, legacy or mismatched index or a strict
reader uses the bounded directory scan and its `scan_stopped` semantics.
The same validation path lets an exact-SHA CI wait pin a `publication_id` to
either receipt shape.

A product receipt may carry an optional lineage observation returned by the
VCS adapter and bound by Mizu to the project-specific `project.toml` `[vcs.lineage_target]`
(schema 2); the publication identity does not include it. An external
receipt may carry the same bound observation as schema 4, supplied at record time
with `--lineage` and bound to the configured descriptor; schema-2/3 external
receipts and legacy unbound lineage (schema 1) remain readable without proof
but never establish configured-target readiness. Exact-publication `vcs_read
status` retains both the immutable receipt fact and a fresh optional adapter
observation in run evidence, projecting only `ready`, `stale` or `unknown`.
This is external-untrusted evidence, not a promotion gate; malformed,
unavailable, unconfigured, unbound or descriptor-mismatched lineage never
upgrades readiness.

Source-identity boundary (ADR-049): `code_digest` always names the whole
immutable capture, never a filtered tree. New schema-3 records of both shapes
additionally bind the versioned public-source projection
(`projection_schema`/`projection_algorithm`/`projection_digest`,
`mizu-public-source-v1`) derived from captured file metadata with the same
curated inventory and private-name policy as the distribution packager; the
Editor export writes it to `public_source.json` and the publish request
carries it explicitly, with the receipt ID binding it. The host adapter
recomputes that manifest from the Git publication tree and rejects mismatch,
and never filters Git ignored state while claiming an exact whole-snapshot
match. Schema-1/2 records stay readable and are never rewritten.

Under one `records.lock` section, the reader scans committed
`decision-history/*.json`, validates and reconstructs every seeded item
(including closed states), and returns only
`open`/`doing`/`blocked` rows ordered by status (`doing`, `blocked`, `open`),
oldest creation timestamp, then ID. Its bounded opaque cursor carries that full
exclusive sort key; `total` counts all currently open seeded items,
independent of the cursor. `overflow_count` counts matching open rows remaining
after the input cursor and beyond this page; `truncated` is exactly whether that
count is positive. Rows include a structured wait for blocked items and an optional reason preview
of at most 1024 UTF-8 bytes. Limits are 1–100 rows,
8192 canonical bytes per row, and 1 MiB for the complete response; selection
uses O(limit) row memory plus temporary/alias metadata bounded to at most the
configured `limits.history_temporaries` entries of each type (default 64, range
1–4096). This is a
recovery work budget, independent of normal history length and retention.
`MAX_HISTORY_TEMPORARIES = 4096` is a separate absolute metadata safety ceiling,
not a format limit. A synthetic validation of 4,096 recognized names used about
3,421,734 bytes of Python peak allocation and 149 ms on the measured host; this is not a
native-platform timing claim. The configured threshold fails closed before
cleanup when exceeded. It bounds neither the full `os.scandir` name enumeration
nor normal decision-history length. The temporary-name pass precedes the
O(decision-history) enumeration under the shared lock, and stats only
recognized temporary files. Event inode
matching is done only when a post-link alias is found, before any origin
projection is read. No list index is maintained.
Unsafe or malformed committed history, seeds, or state fail closed. Recognized
regular `.new-<8-char>` orphan temporaries are removed, never promoted. A
post-link hard-link alias is removed only when exactly one regular committed
event filename has the same inode; unmatched aliases, unexpected names, and
unsafe temporaries fail closed. Recognized per-item atomic-write leftovers are
also recovered under the lock. Paging is a live view across calls, not a
transaction snapshot. The exclusive last-key cursor avoids offset shifts when
an earlier row closes, but new IDs that sort at or before the cursor may be
absent from that continuation; an
empty page after a cursor does not mean the collection is empty. `total` and
`coverage_complete` describe only each call's locked scan. A fresh traversal
starts with a null cursor and checks `total`, `truncated` and
`coverage_complete` on each page, but still receives no transaction-consistent
multi-page snapshot. The prompt and tool are observations only: they add no
transition, scheduling, reconciliation, or Tasks-retirement behavior. The page
reader preserves existing store error classifications and fails closed on
malformed/unsafe records.

Legacy data from before the kernel migration is not converted by any command.
Work items are immutable records with opaque origins; an operator can carry a
previously tracked item forward by writing an `imported` seed directly (see
[operations](operations.md#re-seed-work-items-that-were-seeded-from-decisions))
and can reopen a blocked item whose old wait kind is no longer evaluated (see
[operations](operations.md#reopen-a-blocked-item-whose-wait-kind-was-retired)).
Role-to-role information lives in declared [queues](kernel.md#queues). No reader
performs migration, reconciliation, or Tasks retirement, and no current tool
advertises those operations.

Editor messages are first atomically claimed into a private ingest directory;
they are not parsed while still mutable in the Editor outbox. Malformed records
and non-regular objects are quarantined. A crash after claim can be recovered.
The outbox claim generation belongs to the beginning of a unit, so messages
arriving during that unit are delivered on the next ingest, not lost behind an
advanced cursor.

## Multiple models and recovery

Profiles explicitly select engine/provider/model/session and native options. Persistent sessions are
keyed by role, model, goal and policy/grants; live keys
(`thinkingLevel` on pi/pi-durable, `model_reasoning_effort` on Codex, `effort`
on Claude, plus `durable_compaction_*` thresholds on pi-durable) apply live to
the resumed conversation instead of forking it.
A changed key opens fresh context;
old sessions are kept for audit. No silent provider fallback or automatic replay
is implemented. Operators can change profiles at a boundary, and Worker can
request independent consultations against the same snapshot.
Each consultation or delegation child has its own role wall (unset inherits the
project value, `0` removes only that wall) and positive logical-request timer.
The parent's wall does not shorten an admitted child, cancel its synchronous
join, or trigger transport cleanup that releases the workspace early. Cleanup
drains joins; explicit stop/pause/unarm still cancels the shared scope, and a
child's local timeout cancels only the child. Parent deadlines keep ticking:
after joining an expired finite parent cannot infer again. Claude/Codex request
bounds enclose the entire query/turn, including tools; they do not claim to time
every opaque provider HTTP subcall.
Consultation and delegation lend the already admitted caller's execution slot
while it joins; no additional slot is acquired, even with `parallel_runs = 1`.

Caller admission uses a bounded FIFO list in `data/slots/queue.json`, protected
by a short, nonblocking `queue.lock` transaction.
`limits.admission_queue_capacity` bounds pending FIFO tickets (default 16,
integer range 1–1073741824), independently of `limits.parallel_runs`, which
exclusively caps active execution slots via `N.lock` file locks. Waiting callers
enqueue when blocked; ordinary fail-fast callers do not. The daemon uses the
explicit `enqueue_busy` option to retain a reusable process-local ticket across
fail-fast attempts, under the same capacity and FIFO rules. Overflow and
fail-fast contention remain `Busy` infrastructure deferrals. All callers may
skip older tickets whose role/workspace locks are unavailable, but never a
ready one. Skipped tickets retain their original FIFO position.

Waiting holds only a pending-liveness lock, never role, workspace or slot
locks. Before attempting admission and after acquiring dispatch locks, normal
control, stop, budget and disk guards are checked afresh; snapshots, triggers
and selectors are still evaluated after admission, not serialized in tickets.
The queue transaction performs no model calls or policy selection. Pending
lock files use fixed indices and are never unlinked; the kernel releases them
on process death. Successful dispatch removes the ticket and releases its
pending lock within the same queue transaction, so the pending place is
immediately reusable. Timeout, stop and errors invalidate the ticket by
releasing that lock; removal is immediate if the queue guard is available,
otherwise the next transaction reaps the inactive record before ordering or
capacity checks. No PID-reuse test or provider-cache TTL is needed.

Queue state is local coordination data, not authority: version 1 contains only
`version` and FIFO `tickets`; each ticket contains a unique 32-character lowercase
hex `id`, a unique integer pending-slot `owner` (not an execution-slot index),
and zero to two `resources`. New owners are in `[0, admission_queue_capacity)`.
Version-1 queues previously bounded owners and ticket count by `parallel_runs`;
reads accept owners in `[0, max(admission_queue_capacity, parallel_runs))` and
at most that many tickets so existing valid tickets can drain. New enqueues
still require a ticket count below `admission_queue_capacity` and a free owner
within its range, never raising that limit to accommodate old tickets. Resources are canonical
`project/locks/run-role.lock` or `project/locks/workspace.lock` names relative to
`data/projects`, with project and role using the normal identifier schema
(at most 63 characters). Absolute paths, traversal, aliases and other lock names
are invalid. The file read/parser
byte bound derives from the accepted owner/count bound and the largest canonical
ticket. Queue state is opened nonblocking and without following the final
symlink where the OS supports those flags; `fstat` must identify a regular file
before any read. FIFOs and other special files fail closed with generic
`Busy`, as do open/read errors (a missing state file alone means an empty queue).
On systems without `O_NOFOLLOW` (notably Windows), a pre-open `lstat` rejects
existing links and special files, but cannot prevent a concurrent replacement
with a symlink; there is no portable atomic no-follow/nonblocking open there.
The local data directory and its parent paths must remain operator-controlled.
The opened file's size is checked against the byte bound
before reading; individual reads request at most 65,536 bytes rather than
allocating for the potentially enormous configured theoretical bound. Reading
stops at EOF or after at most the byte bound plus one overflow-detection byte,
also rejecting growth after the size check before parsing. This adds no separate
total-state-byte policy; memory usage still scales with actual
state bytes. Oversized, unreadable or malformed state fails closed with `Busy`
before liveness or resource probes or queue rewrites. This is local filesystem
coordination, not a remote or network-filesystem scheduler. It does not rank
cache affinity, preempt a running driver, or change caller slot lending or writer
workspace ownership.
Only the live `Engine.run` caller owns that lease; unadmitted calls fail with
`Busy` before child inference, and children cannot lend onward. Consultation
fan-out stays bounded by `parallel_consults` and shares the daily budget;
answers fail independently and cannot edit code. Delegation retains the caller's
workspace lock throughout the join, preserving one writer.
Delegation results and retained child records report `status = "bounded"` for a
bounded child seal or typed local exhaustion, with the host-known `bound`
(`requests`, `tools`, `deadline`, `drain`, or `authority`) when present. Other
child faults, infrastructure refusals, cancellation, and result-storage failures
retain the interrupted path. Shared spend counters and non-deadline spend bounds
still constrain the caller's seal, with its final request/tool reserve intact;
pending child admission/tool refusals stay invocation-local. The returned child
result is the evidence, not grounds to reclassify a later caller provider fault.
The caller's own subsequent admissions retain their own host refusal evidence.
A resumed persistent session receives only an append-only delta (goal pin,
current snapshot pin and the ready-work/queue facts not yet offered), never the
full history again; a new or rotated session receives the full prompt with the
same current facts. The durable adapter exposes
no internal compaction event; the harness does not infer one.

`limits.session_delta_details` selects `full` or the default `elide` for both
full and resumed prompts. Every bounded work/check row remains explicit,
including identity, title/status/revision/wait/liveness, result and discovery
metadata, page totals, truncation and coverage. Elision replaces only
`description_preview`, `scope_preview`, `reason_preview`, and role-owned
`check_jobs` `command_preview`/`error` text with `details_reference` requests:
`mizu_work_items_read` by ID or own `mizu_check_job` result by ID. Only roles
already holding that exact read capability receive references; other roles
retain their previews. Reads are bounded live views, not frozen detail copies;
check result may acknowledge completion/import matching acceptance.
Shared-check candidates retain previews: discovery precedes validation and
`join` revalidates/subscribes, so it is not a guaranteed details read.
Only validated same-session fingerprints add `details_unchanged: true`;
new/changed rows never claim unchanged. Ready-work, captured work-order inputs
and hashes, dispatch/liveness/authority and check execution are unchanged.
This display-only byte reduction makes no cross-run or provider-cache claim.

The existing atomic `prompt_state.json` cursor replaces bounded per-row SHA-256
fingerprints under `row_details`, never retaining a growing seen-set. These hash
the complete current bounded row except `age_seconds`, `doing_age_seconds` and
`blocked_age_seconds`; stale/liveness transitions are not ignored. Cursor groups
are limited to the configured work-item page and the existing two 32-check
pages. A 64 KiB managed cursor envelope accommodates their ID/digest pairs and
the protocol-bounded grant. Missing, malformed, oversized, key/session-ID/
creation-time/generation-mismatched cursors never mark details unchanged. Fingerprints are
written only after a settled dispatch whose observed selection proves the
persistent session/generation, alongside (without replacing) grant evidence.
No new cursor store, compaction detection or adapter guarantee is introduced.
The session's existing `prompt_state.json` cursor may also carry a tail-only
`broker_grant` (`session` fingerprint, protocol-bounded tool names, exact `bound`,
`wrap_up` boolean). It is replaced after settled publication, not appended;
legacy cursors still work. The fingerprint binds the observed driver's persistent
session ID, creation time and generation without exposing the ID in prompts.
Unobserved/mismatched driver selection supplies no grant evidence. On the same
resumed session only, a validated prior settled-session cursor is compared
with the current broker tool surface. A changed surface
produces a compact `grant_transition` beside `unit_tool_surface`, with exact
added/removed tool names. When the prior physical dispatch proves a per-run
bound and wrap-up-only grant, the prompt says that restriction ended with that
unit; otherwise it states that the recorded tool grants differ without
inventing a cause. Missing/stale/corrupt evidence, unchanged tools,
fresh/rotated sessions or a still-active bound produce no claim. This is
explanatory context only, never authority, and does not advance the
inbox/snapshot cursor.
Both modes carry only `goal_digest` for the charter. Every engine dispatch,
including resume, places the ordered role policy and exact pinned charter in
stable system instructions, independently of compactable unit history.
Writers pin the live operator goal, observers pin the snapshot goal, and
children inherit the parent's goal. Codex/Claude session fingerprints include that
goal's digest. Ordinary Pi applies authority sections live as described below;
pi-durable preserves its separate live-configured store identity,
with both resume-authority and turn grants binding the composed system text
through `policy_text` to refuse stale crash recovery.
Ordinary Pi (version pinned in [upstream contract](upstream.md)) uses a fixed base prompt plus host-resolved
`mizu_authority_policy_0`, `mizu_authority_policy_1`, … in source order and
`mizu_authority_charter` for the exact pinned goal. Sources are independently
read as UTF-8 with the same 64 KiB per-file and 256 KiB aggregate policy bounds
and fail-closed errors as composed policy; the configuration allows 1–16 files.
Unlike the composed-text interface, section values preserve original whitespace.
Names are synthetic, never policy source paths, and cannot collide with Pi's
managed sections. `pi-effective.json` carries `systemPrompt` (the fixed base)
and `authority_sections` (an ordered array of `[name, text]` pairs, so canonical
JSON key sorting cannot reorder sources). `system.md` is the auditable
host-owned base/section projection; it excludes Pi-generated sections such as cwd
and any unrelated extension sections, and never adds source-path labels.

The existing inline `register()` extension replaces only the `mizu_authority_`
namespace in `before_agent_start.systemPromptOptions.sections`, leaving other
extensions' current sections intact. Pi persists section diffs as positional
SystemMessages, including removals when the source list shrinks. Do not append
manual authority messages before `createAgentSession`: that API rebuilds initial
system state. Pi replay and compaction checkpoints retain current sections;
provider adapters either transmit positional updates or resolve them into the
current leading prompt according to the provider's transcript support. This is
not a promise of a provider cache hit. No extra inference or retry is introduced;
reads fail before launch, and normal dispatch cancellation/errors apply.

Only ordinary-Pi identity excludes policy text, goal digest, capabilities and
`settings.engine_tools`, as well as its already-live thinking/cache options.
Provider/model, session mode, workspace, command, resources, MCP, local model
settings, all other effective settings and **adapter digest** still bind the
conversation. Current SDK tool registration/loadout reconciles additions and
removals; historical declarations are not executable grants. Current Bridge and
host admission remain authoritative, including for finish-only continuations.
This change deliberately causes a **one-time rekey** of existing ordinary-Pi
sessions. Old directories are untouched: no scan or ambiguous adoption is
attempted. Opt-in families likewise use the new `families-v2` reference namespace,
leaving old `families` pointers untouched. Subsequent adapter changes remain
identity-bound; this is not a general family migration facility. Non-Pi and
pi-durable identity, prompts and execution paths are unchanged.

Rotation is operator policy via `[limits] session_max_tokens`,
`session_max_cost_usd`, and `session_max_age_seconds` (0 disables each, age bounds
conversation lifetime from its stable creation surviving same-id saves):
a due rotation removes `session.json` so the next dispatch mints a fresh
provider session while the published snapshot and composed system context reload
unchanged, recording the reason in `rotation.json` and the mode/key in
`prompt_projection.json` (ADR-028). Rotation also bumps the session
`generation` counter (native engines ignore it): a pi-durable persistent
session keeps one conversation store per generation, so rotation starts a
fresh conversation and retires the abandoned generation stores, while an
undisturbed retry reuses its generation and resumes the active run.
Ephemeral and one-shot pi-durable runs never share a store: each run gets
a distinct key that is stable across retries of the same run.

Repeated failures pause a project. A restarting worker inspects actual files and
previous failure evidence before further action. It does not resend the last
command blindly, run `git reset --hard`, or manufacture a successful observation.

The runtime prompt carries only protocol framing (goal, anchored snapshot
references, capability-gated acceptance commands, the bounded `open_work_items`
page for roles holding a work-item capability (`roles.<name>.context_sections`
restores or removes optional sections), then trailing volatile `run`,
`now`, and `timezone` fields so repeated stable content forms a longer cacheable
prefix); sealing
is directed by the `finish` tool contract. Each run
records `prompt_projection.json` with the sent counts (`recent_snapshots`,
`acceptance_commands`) and `prompt_bytes` as evidence for
reliability review. Its `open_work_items` metadata (null when the role's prompt carries no inventory) records only `shown`, `total`,
`overflow_count`, and `coverage_complete`, never item text.
All work judgment lives in the role policies under `policies/`. Likewise the `report`
fallback document (used by `mizu report` with no LLM) and the smoke probe goal
are labeled mechanism/test vectors, not policy.

## Performance model

Idle polling is ordinary Python and filesystem reads. Observer change detection
avoids no-change reviews. Content-addressed storage reduces repeated file bytes;
prompts and wire records have bounds; consultations have bounded concurrency;
experimental output is capped. There is no latency/throughput benchmark claim.

Each execution command starts a fresh container. This intentionally trades some
startup latency for isolation and simple cleanup. Each work unit starts a Pi
process but can reuse its durable session. Large monorepositories pay for a
bounded tree scan per capture; tune exclusions and limits or split projects.
Before adding caches, workers or a distributed queue, measure this cost on the
actual repository and preserve the publication/security contracts.

The updated publication/history, run, pagination and retention contracts are documented in [completion-contracts.md](completion-contracts.md).

## Declarative model selection

`selection.py` evaluates bounded predicates and persists observed availability;
`classification.py` supplies attributes through rules and optional existing-driver
inference. Runtime resolves a profile once per work unit. Ranking, error matching,
recovery and classification meaning are operator TOML/Markdown, with no provider
heuristics. See [the selection contract](model-selection.md) for trust, evidence,
unknown values, cancellation, limits and explicit activation.
