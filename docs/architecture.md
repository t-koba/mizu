# Architecture

## Boundary first

Mizu separates the control plane, the model process, the execution sandbox and
published evidence.

Mechanism provides capability without prescribing behavior; policy defines
intent and decisions. Keep mechanism minimal (KISS) and avoid embedding
behavioral constraints, heuristics, or assumptions into code. Ask whether a
change provides a capability or enforces a behavior; behavioral rules belong in
operator policy, not mechanism.

Role names are configuration, not a class hierarchy. The Python runtime executes
capability-bearing work units. A Markdown policy explains which work to choose;
it cannot grant capabilities or change limits.

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
| `runtime.py` | Admission, locks, capability dispatch, publication | Which algorithm is best |
| `unit_prompts.py` | Full unit prompt, resumed-session delta, and bounded display projections for ready work, decisions, research, CI, and the writer work order | Session selection/rotation/recovery, admission, scheduling, or lifecycle policy |
| `workspace_reads.py` | Role-scoped workspace file listing, paged reads, and snapshot diff projections, including virtual upstream refs | Role grants, workspace write policy, or publication semantics |
| `evidence_records.py` | Exact role-bound retained-evidence reads and durable per-invocation/mutation evidence records | Adapter retries, reconciliation, or evidence truth |
| `run_health.py` | Role-health failure/streak bookkeeping and capture-cause persistence; runtime re-exports its helpers | Terminal outcome classification, recovery/rotation, or successful-run lifecycle decisions |
| `ci_observation.py` | Read-only cached-CI failure projection and wake-branch normalization (bounded, never raises) | Polling, wake policy, admission, dispatch, session lifecycle |
| `ci_wake.py` | Per-role CI wake record/due helpers and wake-version/default constants (bounded, never raises) | Polling, admission, dispatch, session lifecycle |
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
| `insights.py` | Submission identity, revisable proposals, decision history/legacy receipts, source-current shared-decision projection and page construction, initial work-item seeds | Acceptance or rejection |
| `insight_tools.py` | Context-bound insight tool adaptation: paged reads, role/run/snapshot provenance, grant checks, and evidence-attachment audit records | Insight persistence/decision semantics or dispatch/schema policy |
| `shared_records.py` | Bounded canonical records, exact role/record/digest receipts and manifest-scoped paged reads | Source eligibility, route policy, lifecycle, or cutover |
| `decision_delivery.py` | One durable-marker-selected `insight.decision` runtime route for prompt, wake, selection, retrieval and acknowledgement | Host activation or other event-family routing |
| `decision_reconciliation.py` | Host-only exact legacy-receipt preflight/apply and one-way activation after per-role pending-set parity; publishes the authority marker last | Model-granted activation or task lifecycle |
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

The [shared-record delivery](shared-records.md) mechanism is implemented as a
bounded immutable store with manifest-scoped reads and receipts. A host-only,
one-way marker can activate shared delivery for `insight.decision` after exact
legacy-receipt migration and full per-role pending-set parity; legacy remains
the default until that explicit activation. Other event families keep their
existing authorities and are not switched by this pilot.

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

Atomic replace plus fsync and locks provide local filesystem durability under
standard local filesystem semantics. Use a local filesystem, not NFS/object-store/FUSE mounts
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

Snapshot retention is storage-owned policy, not a publication side effect.
At the existing per-project automatic-prune boundary, `snapshot_retention_days`
(default 31; 0 disables) permits removal of old superseded history generations
and unreferenced manifests; no timer or pause is added. Current history,
continuations/fences, runs, artifacts, live/revised/archived insights, pending
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
workspace/role/editor/insights quiescence exclusions nonblocking, plus publication,
all check-slot exclusions and the editor outbox submission lock. Check runners
capture content outside role units, so even never-used configured slots must be
excluded. CLI submit/revise/report/export take publication before observing a
snapshot and hold it through storing its citation/export. Capsule writers have
only the outbox mount and serialize submissions on `.submit.lock`; no project
access is added. These writers acquire nonblocking and refuse as Busy on
contention before observing/storing a reference; callers can retry later (CLI
exit 75, editor tool error), rather than waiting for a maintenance scan. Publication precedes insights, as in delivery binding. The
acceptance audit also opts into nonblocking locking for this sweep. There is no
new snapshot-write locking layer or changed publication authorization. Unlink
histories before manifests before unused objects; interruption leaves extra data
for a later rescan, not deleted retained dependencies. A bounded result joins
`maintenance/automatic-prune.json`; GC failures never fail requested work. Run
and shared-smoke retention are independent and unchanged. See
[configuration](configuration.md#automatic-snapshot-retention) for the exact
root/age/result contract and outbox lock permissions.

Work orders keep one bounded canonical `work-order/current.json` record, atomically replaced under `insights.lock` with exact revision/generation CAS. Each committed revision receives a distinct immutable `work-order-<sha256>.json` receipt under the authoring run and existing run retention; the tool result names its exact receipt. Legacy history/checkpoint files are unused and left untouched; valid schema-1 current records remain readable. There is no recovery scan, cleanup or automatic retry. See the writer work-order contract in `configuration.md` for schema, bounds and failure behavior.

The history index retains published IDs up to the configured limit (default
128, tunable 8–1000000 via `limits.history_index`). The active generation/window
is always retained; older storage follows reference-aware retention below. Prompts carry a bounded, capability-gated projection: up to
`limits.prompt_snapshots` recent anchored summaries with exact IDs and code
digests, plus only the actionable context the role is granted to use.
`work_order` freshness (`absent`/`current`/`review_due`/`unavailable`) is offered only to roles with `work_order`, sharing the unit-frozen full pending-inventory generation across full and delta even when a delta omits already-offered bodies; `pending_insights` is offered only to roles with `insights`/`decide`, newest
kept up to `limits.pending_insights`;
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

`continue` starts another meaningful unit. `bounded` is a `continue` that hit a per-run spend bound or entered finite-wall wrap-up before sealing: it saved what it could under the wrap-up allowance and runs again without counting toward `max_failures`. A whole-unit wall exhausted or deadline wrap-up ended without a seal instead
records a bounded run result, without creating or publishing a snapshot or
counting toward `max_failures`. Unrelated pre-seal faults still fail even if the
wall also expires.
`wait` sleeps until its deadline,
new information, a changed goal, a human wake, or a cached current CI failure
on the effective configured wake set (`ci_branch` plus deduplicated
`ci_wake_extra_branches`; an empty set disables the wake) for roles with
`ci_wake` (writable roles by default; an unchanged red re-wakes only on a new
branch/SHA/check/run fact, a moved head, or every `ci_rewake_seconds`). Failures
remain visible independently of insight decisions until green or superseded.
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
code, successful decisions and insight actions, and fresh/due one-shot input
also qualify. The free-text `state` signal remains opt-in; a paraphrase alone is
not progress under the default. Input means a changed inbox or operator
wake generation, consumed decision/wait events, CI failures due for this
snapshot, or an expired prior wait. Persistent pending obligations and cached
red CI presence do not count repeatedly: pending obligations are fingerprinted
by stable identity, so a newly offered obligation counts once while unchanged
visibility does not reset the streak. Finish alone is
not progress. The default `no_progress_continuations = 0` therefore parks after
the first `continue`/`bounded` unit without a selected progress signal. A
missing-finish continuation may still use the full work grant when no tool was
called; otherwise it remains finish-only. Operators may allow 0–100
consecutive no-progress continuations; selected progress or a role/policy change
resets the streak. `no_progress_backoff_seconds = 0` disables a timer wake; a
positive value permits a parked continuation to wake after that delay measured
from its recorded progress timestamp. `no_progress_recovery_after = 2` rotates a
persistent writer conversation to a fresh generation on the next independently
admitted dispatch after two empty writer snapshots; it does not admit
or schedule a run. `mizu status` exposes whether that recovery threshold is due
and the count, so parked empty work is visible while awaiting its next trigger.
Writer-daemon readiness checks goal and operator generations, role-eligible
ready work, and retained exact-SHA work-item CI wait evidence for the current
blocked holder before the saved outcome; inbox generations, routed
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
Legacy snapshots without progress keep their previous admission behavior.
Schema 1 records the six observed booleans,
the selected signal names, role ID, aggregate progress boolean, saturated
no-progress count (0–101), and a timezone-aware timestamp. The snapshot digest
binds the record. A malformed progress object makes the snapshot unavailable
rather than authorizing an admission; `mizu status` and `mizu explain` expose
only its bounded signal/count/timestamp projection and a finite disposition,
not a scheduler prediction.

## Insight handoff

The submitting route assigns the source identity. A record contains ID, source
(authority channel), opaque origin (actual caller sharing the transport, null
means unknown and never guessed), created time, base snapshot, title, Markdown
body, revision identity (`rev`) and update time. Origin is set once at submit by
trusted host callers, preserved across revisions, never routed on, and never
grants operator approval. Ordinary prompts, lists, and reads expose only
the current content, its current decision/evidence gap and `rev`; obsolete
claims are replaced, never appended. The first submission creates `rev` 1.
The original submitter may revise under the stable ID via compare-and-swap on
`expected_rev`; identical retries are no-ops that do not advance the inbox
generation, while a meaningful revision archives the prior record to
`insight-revisions/<id>.r<rev>.json` and becomes pending again. A crashed
revise converges on retry: an orphan archive for the still-current rev is
overwritten, and a same-content retry preserves the stored `run` (revision
provenance recorded only on meaningful change). The transport exposes revise/history/withdraw to the operator (`mizu insight revise/history/withdraw`);
each runtime role holding `revise_insight`/`withdraw_insight` may revise/withdraw its own insights
via the runtime, which fixes source to the role name and binds to the read rev. A current
`accept`/`modify` blocks withdrawal unless that role also has `withdraw_decided`; no role
receives that supplemental grant by default. The direct operator CLI remains deny-by-default
for a current `accept`/`modify`. History stays a read path. Decisions carry the same binding:
the decide interface accepts the `rev` from the read, and a decision naming
a superseded revision is rejected without writing anything, so a prior
rationale never authorizes changed content while the current revision stays
pending for reassessment. Prior
revisions are returned only by the explicit history path, never injected into
routine context, and are durable audit like decisions (backed up, never
pruned). Direct ID reuse with different content outside revise is rejected.
Decisions bind to the reviewed `rev`, so a prior approval never authorizes
changed content; deferral still requires a revisit condition and stays
pending. A substantive accept/modify/reject prevents conflicting further
decisions on that revision; an exact keyed replay is a no-op returning the
original event, and a meaningful revision reopens it. Accept/modify store the resolved
`work_item_intent` (`create`/`none`), from an explicit override or configured
trusted-source default (global fallback `create`; see configuration.md).
`none` durably records no work with no seed. When the task list is omitted,
`create` places an initial durable `work_item` seed in that same immutable decision-history event (and the
matching current-decision record). Its ID is `wi_<event-id>`; it records
the current insight title and `{insight_id, rev}` origin, starts `open` at item
revision 1 with no holder, and uses the decision timestamp for creation and
update. `defer` and `reject` do not create a seed.

An explicit `work_items` list instead names 1–16 tasks (title, description
and optional structured wait), without parsing the reason. Its resolved intent must be `create`;
policy `none` requires an explicit create override. The history event embeds
the ordered `work_item_specs` and complete `work_items` seed manifest in one
atomic write. For groups with waits, a parallel `work_item_waits` array binds
each normalized condition (including a captured code-change baseline) to its
seed. No per-member seed files or mutable index are needed: group
lookup scans immutable events for a hashed task ID; enumeration reuses each
manifest. IDs hash the decision key, complete ordered specs and member index,
so missing/reordered/tampered members fail closed against the whole set.
All specs and seed slots, then the complete event's `PREVIEW_BYTES` bound
(131072 UTF-8 bytes), are checked before commit. A crash exposes no group or
the entire group; an exact ordered-list retry validates every seed/chain and
repairs only a missing/stale non-substantive current pointer. Conflicting
retries fail. Old single-seed/no-work events keep their exact shapes. Task
seeds inherit any decision-level scope and remain unheld. Tasks without waits
keep the open seed shape. Tasks with one existing observable wait (`deadline`,
`code_change`, `insight_decided`, `dependency`; not `ci`) start blocked with the
normalized wait in that same immutable event. Raw specs, including waits, bind
IDs and exact replay; first commit captures the code-change baseline in
`work_item_waits`, and replay validates that manifest against the blocked seeds
without rebasing. Other waits must strictly correspond to their normalized raw
specs. No sidecar or second transaction is needed; `_wait_due` supplies the
existing readiness re-evaluation. Only group
seeds accept blocked/wait shapes; direct and legacy seed rules are unchanged.
Task-specific scope is not part of this mechanism. Current projections name all
`work_item_ids`, and submitter status uses `{items: [...]}` for groups only.
See configuration.md for field bounds and the CLI file interface.

An accept/modify decision may also state the accepted scope: the work the
decision authorizes (1-4000 nonblank characters). It is recorded in that same
immutable decision event and in the seeded work item, so the scope a work item
is held to is a recorded fact rather than prose absorbed into a description or
reason. A decision that states no scope leaves its item without one; that
older shape stays readable history, and such an item's closure may state the
scope it delivers. The scope is immutable in the seed, like the title and the
origin, and is validated as bounded nonblank text. The compact page rows carry
the scope in force as a bounded preview beside the description it belongs to.

New decisions carry a `decision_key`: the first 32 lowercase hex characters of
SHA-256 over canonical JSON `["insight-substantive-decision", insight_id, rev]`.
Accept/modify/reject use that key as their immutable event filename; defer keeps
a random 32-hex event filename. First decisions and keyed defers use one keyed
content lookup, not a scan of historical event contents. Before any event write
(including reject, defer and withdraw), the shared preflight validates/recovers
recognized atomic-write temporaries in decision-history and refuses a symlinked
or otherwise unsafe decision-history, decisions (always), or waits (when a
registered wait will be written) parent without following it; it enumerates
metadata, not historical event contents. Key collisions (including old random-ID events at
that filename) fail closed. Work-item history readers validate every present
key against the event's insight/revision and require the keyed filename for
accept/modify/reject; legacy records without a key and without an explicit
work-item intent marker remain readable. If the event
committed but its current pointer write
failed, an exact retry (action, reason, revisit, and an explicit intent if
supplied) restores the pointer and returns the original record/seed; omitted
intent reuses the recorded value and a retry run does not replace original run
provenance. Seed replays first validate the item storage and complete transition
chain; corrupt history cannot succeed or repair a pointer. Valid transitions
leave the returned creation event unchanged. Conflicts are refused; a completed
current substantive decision still requires revision to reopen.
Only a revision-current legacy defer lacking `decision_key`, with no keyed
substantive event, requires scanning old random-ID history to detect a hidden
substantive decision. Old events are never rewritten.

`Project.work_items` provides internal `get(item_id)` and the locked readers
`page_open(cursor=None, limit=25)`, `projection(item_id)`, and
`explain(item_id, *, after_rev=0, limit=25)`, plus
`transition(item_id, *, expected_rev, status, actor, reason=None, wait=None,
snapshot_id=None)`. `get()` remains internal-only; the runtime exposes the two
page modes through the existing `work_items_read` grant and exposes transition
only through its separately granted operation described below. `get()` and
the chain replay used by `projection()`, `explain()`, and transition resolve an
item seed directly from `decision-history/<event-hex>.json` or
`work-items/<item-id>/create.json`, validating the immutable seed without scanning
the inbox or wider decision history. Origin staleness separately checks current origin records and any
legacy decision binding; `page_open()` enumerates decision history and direct
creation directories to build its inventory with O(limit) retained rows. The event's title is the immutable title authority, not the
subsequently revisable insight.
Both direct creation and decision seeding call the same lock-held
`WorkItems.create_seed` preflight: title, optional description and typed origin validation, source/ID
binding, safe empty item slot, and exact current origin revision. New seeds
recover a recognized regular orphan temporary (removed without promotion) and
then refuse any pre-existing direct record, legacy sidecar, numeric history,
unknown entry, or unsafe/ambiguous temporary. Preflight writes no
seed: direct creation commits `create.json`, while decision seeding commits only
the embedded seed in its immutable decision event (never a two-file seed/event
transaction). Committed retries use a shared storage/chain validator without
rechecking the live origin. Project wires this service to Insights; standalone
Insights readers do not instantiate it.

Reads and mutations share `locks/insights.lock` with insight revisions and
seed creation. A positive non-bool expected revision must match; each change
exclusively atomically appends the full state to
`work-items/<item-id>/<next-rev>.json`. There is no mutable index or pointer.
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
CAS writes nothing and must be re-read, not blindly retried. `get()` and
`transition()` recover under the shared lock by removing only recognized
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
then `insights.lock`, holding both through append. `Snapshots.publish` takes only
the publication lock, never the insights lock. Thus eviction either precedes the
binding and refuses it, or follows a committed binding.

New transition records carry `delivery_schema: 1`, `delivery` (null before a
binding), and `verification_scope` (null except for retained acceptance).
A delivery holds exactly `snapshot_id`, `code_digest`, `scope_digest` (SHA-256
of canonical accepted scope, or null) and `binding_rev`. Every subsequent
revision inherits it, unless an explicit later doing-to-doing revision binds a
new snapshot. Scope changes retain the old binding but require re-binding
before completion. The schema cannot disappear; removing a binding's fields
cannot turn that same-status event into a legacy no-op. Older unmarked chains
and closures remain readable without rewriting them.

New completion requires a prior binding, exactly its `snapshot_id` and scope,
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

Only a forward `dropped` -> `done` correction is possible: it requires a
previously recorded valid delivery and independently retained acceptance,
even if the snapshot already embeds passing verification. It appends a new
revision and leaves the dropped event intact. No other terminal reopening is
allowed. The binding is an attributed writer declaration of delivery for the
accepted scope, not a semantic proof of arbitrary source changes. Missing,
unsafe, corrupt or mismatched evidence refuses; lock/I/O errors propagate.
Cancellation/process death releases kernel locks; stale CAS writes nothing and
requires a fresh read, never an automatic retry.

The separately granted `work_items_create` operation calls
`Project.work_items.create(*, idempotency_key, title, actor, origin=None)` for
self-directed work and splits. The runtime binds `actor` to the role, never a
model argument. The example grants it only to `worker`; consultation and
delegation children explicitly forbid it. Creation itself does not assign work,
make an insight decision or publish; the independent writer readiness gate may
admit a run for role-eligible ready work, as described in
[scheduling](scheduling.md); a read-only role needs the separate
`work_items_transition` capability and still cannot edit source through its
workspace. The
work-item ledger is the single record of accepted and planned work, including
blocked conditions; the finish outcome and structured wake source record why a
unit stopped, while `Now`/`Next` are short working notes, not a second backlog.

Required arguments are `idempotency_key` (1–128 characters) and `title` (1–200).
Optional `origin` is exactly `{"insight_id": <id>, "rev": <positive int>}` or
`{"work_item_id": <wi_id>, "rev": <positive int>}`. Omission stores null.
The referenced insight (live or archived) or parent item's **current** revision
must match at first creation under the shared lock. Missing/unsafe references,
bool revisions, unknown fields and self-parent references are denied; local
I/O errors propagate. This is provenance, not decision coverage: direct work
never resolves an unseeded accept/modify decision. Insight staleness retains
its existing semantics. Null/parent origins project `current_origin_rev: null`,
`stale: false`, `redecision: null`; parent provenance is an immutable split
reference, not a dependency or a staleness subscription.

The actor-scoped ID is `wi_` plus the first 32 hex characters of SHA-256 over
canonical JSON `["direct-work-item", actor, idempotency_key]`. The immutable
`work-items/<id>/create.json` contains exactly `kind: "direct"`, `actor`,
`idempotency_key`, and `work_item` (the revision-1 seed). Direct creation and
`Insights.decide(..., work_item_intent="create")` share the same seed constructor
and shape validator; old decision IDs, keys, intent fields and stored seeds
are not rewritten. A seed without `description` retains the exact v1 field set;
a supplied `description` adds only that field (v2), with no version marker.
Descriptions must be strings of 1–4000 characters containing non-whitespace;
whitespace is preserved, and explicit null is invalid. Both seed shapes are
accepted on reads/replay. New single-seed decisions use the exact decision
`reason` as their description; explicit task seeds use their task descriptions; readers validate this binding only when the description
field exists, so old events (including longer reasons) remain replayable.
New transitions preserve their seed's field set: v1 never gains a description,
and v2 must retain exactly its immutable text, rejecting change or removal.
No migration or rewrite occurs. Direct seeds never live in decision history. A direct ID
colliding with a decision event fails closed.

First writes return `{record, created: true}`. Exact retries return the original
creation `{record, created: false}`, not the current transitioned item; a changed
title, origin, description text or description presence with the same actor/key is denied. Retry comparison precedes
live reference checks so advancing an insight/parent cannot break replay.
Callers must retain the key and exact request across cancellation or uncertain
responses; there is no automatic retry. Atomic exclusive writes commit the seed
before returning; a pre-commit temporary is never promoted. Replays validate
the committed chain and do not count as tool progress. Direct items use the
same open/closed pages, full/delta prompt inventory, by-ID reads, CAS/history,
256-revision and record/page byte bounds, done-verification gates, event receipts
and backup/restore tree as decision items.

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

Terminal routing resolves only an explicit insight origin's submitter.
Null/parent provenance remains unknown routing (including for `sources="any"`
subscriptions); it never falls back to the creator, holder, parent actor or
transition actor. Exact receipts still identify each terminal item/revision.

Statuses are `open`, `doing`, `blocked`, `done`, `dropped`. Same-status changes
require a scope change or an explicit doing-to-doing delivery binding. Done
never reopens; dropped permits only the stronger forward done correction
described above. Other statuses can move to any different status, subject to
completion evidence and the ordinary constraints. Title/origin/description/creation time stay fixed; every event
records its revision, update time, caller-supplied actor, from/to transition,
and explicit reason/wait/snapshot/verification fields (null when absent).
Actor is trusted internal attribution, not a new authorization grant. `doing`
assigns the actor as holder, `blocked` preserves the holder or assigns the
actor, and other statuses clear it. A blocked transition may include an
Insights-style wait (stored only, no scheduler wake), or a work-item-only `ci`
wait pinned to a completed immutable publication receipt. The latter wakes only
its current holder on retained terminal evidence, without changing item status.
Dropping requires a nonempty reason. A new completion revalidates the
immutable snapshot manifest named by its earlier delivery binding; it need not
remain in the moving publication-history index. Embedded verification must
have `passed is True` and match the snapshot's code digest, or completion pins
an exact retained acceptance receipt and scope digest. The binding and
verification reference are copied into the immutable completion event.
Backup/restore includes these histories; storage audit/reclaim protects every
historical delivery and completion snapshot manifest. Work-item
retention/expiration is not implemented, so those citations remain durable.

Terminal `done`/`dropped` transitions can wake roles explicitly subscribed via
`work_item_events` (default disabled). The event adapter reuses these same seed
and full-chain validators, not a separate glob projection. Per-role exact
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

`projection(item_id)` returns one bounded view (`item`, `current_origin_rev`,
`stale`, `redecision`) without changing the immutable seed or CAS record. A newer
origin revision is stale until that revision has an `accept`, `modify`, `defer`
or `reject` decision; `withdraw` does not clear staleness. Re-decisions must
match the action-specific decision schema; an accept/modify must also match its
immutable decision-history event and pass seed validation (or the explicit
no-work contract with no seed). Missing current
origin data is reported stale, while unsafe or malformed origin records fail
closed. This projection does not yet gate internal transitions.

`page_open(cursor=None, limit=25)` is the bounded page reader used by the
explicitly granted `work_items_read` operation and by the project-wide
`open_work_items` prompt projection. Every full and delta prompt includes the
same projection regardless of role grants, capped by `limits.work_items_prompt`
(default 20, range 1–100); the runtime calls the fail-closed reader directly,
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
`insights.lock`, preserves fail-closed seed/chain validation, and remains
observational: it does not account progress or affect scheduling/wakeup.

The separately granted `work_items_transition` operation exposes only
`Project.work_items.transition`: required `id`, positive `expected_rev`, and
status (`open`, `doing`, `blocked`, `done`, `dropped`), with optional nonempty
reason (1–4000 characters), structured wait, accepted scope, and 64-character
lowercase digest `snapshot_id` (status-dependent: delivery binding on `doing`,
exact prior binding on `done`). The configured worker
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
`dropped` requires a reason, and a `blocked` transition may include the same
structured wait schema and normalization semantics as `decide`, or a
work-item-only `{kind:"ci", publication_id, checks}` wait. Existing wait kinds
remain stored on the item only, without scheduler effects. CI waits validate a
completed immutable publication receipt at registration and on stored reads,
binding the recorded branch and published SHA; absent or uncertain receipts
are refused. See [exact-SHA CI waits](scheduling.md#exact-sha-work-item-ci-waits)
for terminal evidence, holder wake and acknowledgement semantics. This mutating operation is not observational
and receives no exception to the existing per-run or wrap-up spend bounds.

### Requesting CI at an exact published commit

CI usually starts from a push, which spends a run per publication whether the
work needs one or not. `vcs_request` lets a role ask for one run per workflow
at one exact published commit instead. The target is a completed publication
receipt, never a branch head, and the product verifies that every run the
provider reports names that commit, so a run at another commit is refused
rather than accepted as close enough. The request and the returned run
identities are recorded durably, a dispatch marker is written before the
provider is called, and an unresolved dispatch is never retried blindly: a
second request would spend a second run for the same commit, so the caller
re-observes the CI status instead. A request may cover work items: each
covered item names the revision it is covered at and the checks whose result
settles it, and the product registers a receipt-pinned CI wait on it, so the
item waits for the request's result and its holder keeps the completion
decision. The role capability and the operator's
`request_grant` are both required, so the path exists without anything
triggering it until the operator grants them.

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
and returns one bounded page of at most 100 summaries, filtered by exact code
digest, branch or published SHA, with the match count, a `truncated` flag and a
`scan_stopped` flag so a caller can tell an absent receipt from one the scan
never reached. An unreadable,
oversized or invalid record is skipped, never returned unvalidated, and it
never denies the valid records beside it. The same validation path lets an
exact-SHA CI wait pin a `publication_id` to either receipt shape.

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

Under one `insights.lock` section, the reader scans committed
`decision-history/*.json`, validates and reconstructs every seeded item
(including closed states), and returns only
`open`/`doing`/`blocked` rows ordered by status (`doing`, `blocked`, `open`),
oldest creation timestamp, then ID. Its bounded opaque cursor carries that full
exclusive sort key; `total` counts all currently open seeded items,
independent of the cursor. `overflow_count` counts matching open rows remaining
after the input cursor and beyond this page; `truncated` is exactly whether that
count is positive. Accept/modify history events without a `work_item` seed are
counted as `unseeded_decisions` only while unresolved. An explicit
`work_item_intent: "none"` resolves coverage without a sidecar or seed.
Explicit task manifests also resolve coverage, validating every member instead
of treating the absence of singular `work_item` as an unseeded legacy event.
`coverage_complete` is true only when every such event has an explicit no-work
marker or a valid reconciliation sidecar; malformed
sidecars fail closed rather than claiming coverage. No legacy state is inferred. The prompt carries this count,
truncation, and coverage state; an incomplete projection is not an exhaustive
inventory. Rows include current origin staleness/redecision, a structured wait
for blocked items, and an optional reason preview of at most 1024 UTF-8 bytes. Limits are 1–100 rows,
8192 canonical bytes per row, and 1 MiB for the complete response; selection
uses O(limit) row memory plus bounded temporary/alias metadata (at most 64 of
each). A bounded temporary-name pass precedes the O(decision-history) enumeration
under the shared lock, and stats only recognized temporary files. Event inode
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
**Free-text Tasks in writer state are still needed until legacy reconciliation
is complete.**

The separate `work_items_legacy_read` grant exposes
`page_legacy(cursor=None, limit=25)`: a live read-only audit inventory of **all**
accept/modify history events without an embedded `work_item`: legacy
unresolved/reconciled events and new explicit no-work dispositions. It returns `items`, `total`, `cursor`, `next_cursor`,
`overflow_count`, `truncated`, `unseeded_decisions`, and `coverage_complete`.
Rows contain `event_id`, `action`, valid source `insight_id`/`rev`/`created_at`
(or null for invalid/missing fields), a decision `reason_preview` (1024 UTF-8
bytes) with `reason_truncated`, `openable`, and `resolution`. `openable` means
unresolved with all origin fields needed for opening; it does not imply a
model-supplied title has been validated. Explicit no-work rows have
`work_item_intent: "none"`, `openable: false`, and
`resolution: {outcome: "none"}`. For legacy rows, resolution is null or contains
`outcome`, runtime `actor`, reconciliation `created_at`, `event_sha256`, a
bounded reason preview/truncation flag, and `work_item_id` for open. Proposal
bodies are never copied. The same 1–100 row, 8192-byte row, 1-MiB response and
128-character cursor bounds apply. An opaque versioned last-event-ID cursor
orders events lexicographically, exclusively; selection retains O(limit) rows
while scanning all committed history. Resolution does not remove audit rows.
Each page validates committed records even beyond its cursor, under
`insights.lock`, with the same recognized temporary recovery and fail-closed
behavior as `page_open`. Pages are live, not frozen multi-page snapshots.

`work_items_reconcile` explicitly resolves **one exact event**. Required
arguments are its 32-character lowercase hex `event_id`, `expected_action`
(`accept` or `modify`), `outcome` (`open` or `drop`), and nonblank `reason`
(1–4000 characters). Optional positive integer `expected_rev` must match the
source if supplied. `open` requires a 1–200 character `title` and valid source
insight ID, positive exact integer revision, and timezone-aware decision
timestamp. `drop` requires no title and works even with missing/invalid legacy
origin fields. It creates no active item or seed. Seeded and non-accept/modify
events, and events with explicit work-item intent, are refused. The runtime binds actor to the role and rejects a
model-supplied actor; only the example worker holds either legacy grant.
Consultation and delegation children cannot hold reconciliation authority.
Legacy reads are observational; reconciliation is not. Neither grant widens
writer admission or receives a wrap-up/tool-budget exception.

Under the shared insights lock, reconciliation exclusively atomically creates
`work-items/wi_<event-id>/legacy.json`. Its immutable fields are `event_id`,
`event_sha256` (SHA-256 of the **canonical JSON** source event), `action`,
`revision` (valid source revision or null), `outcome`, `actor`, `reason`, and
`created_at` (reconciliation timestamp). An open sidecar additionally contains
`work_item`: the normal initial seed with ID `wi_<event-id>`, supplied title,
origin `{insight_id, rev}`, status `open`, rev 1, holder null, and both timestamps
from the original decision. No current or historical decision is rewritten.
An exact retry returns `{record, created: false}`, preserving the original
sidecar timestamp and not counting as progress; the first write returns
`created: true`. A conflicting reason/title/outcome/actor or stale expectation
is denied.
After interruption, re-read/retry the same event: uncommitted temporaries are
never promoted, and post-link aliases are removed only with one matching
committed sidecar/revision inode. Recovery scans have the same fixed per-item
bound (256 revisions including the seed, plus one sidecar and bounded temporary
space); unsafe links, paths, unexpected entries, corrupted sidecars, and source
hash mismatches fail closed. Store Denied/LimitExceeded and local I/O/lock errors
retain their classifications. Cancellation releases the lock; no automatic retry
or scheduling is added.

`page_open`, `get`, and CAS transitions resolve open legacy seeds through that
validated sidecar without changing decision history. Open resolutions replay
normal revision history; drop resolutions count as covered but are never open
items. Current legacy accept/modify records without a seed retain strict
legacy-field validation and must match exactly one immutable history event for
redecision projection; embedded seeds, when present, still require the full
immutable seeded-event contract. Coverage is a locked
history scan result, not a claim that deployed data has been reconciled. Keep
free-text Tasks until an authorized per-event pass proves coverage. These APIs
do not perform automatic migration, retire Tasks, or change scheduling.

Author withdrawal (`mizu insight withdraw`, submitter or operator only)
closes a pending proposal with an actor-labelled rev-bound record in
the same decision store. A revision-current `accept` or `modify` blocks
withdrawal by default, including on the direct operator CLI; only a runtime
role already holding `withdraw_insight` can opt in with supplemental
`withdraw_decided`. Pending, deferred and rejected proposals remain
withdrawable. Stale expected revisions are refused, repeated withdrawal is
a no-op, and `reject` keeps meaning substantive rejection. A permitted
post-decision withdrawal appends its own history record and never removes the
original decision-history entry. A role with configured
`decision_events` is additionally admitted by unacknowledged decisions routed
to it (insight source equals role name in `own` mode, any source when
`decision_sources = "any"`): the dispatch delivers the current
finding plus the reason as a `decision_events` field inside the role's ordinary prompt
(all other granted inputs stay present), described factually by a `decision_task` field,
records the admission and the
delivered events under the run directory (sized by the role-effective
`decision_events_limit`, independent of the terminal batch). Each delivered
row includes a role/payload-bound opaque `ack_token`. Only tokens explicitly
selected in `finish.ack_events` become exact per-role receipts after the durable
successful finish (and publication for writers); finish outcome, summary and
check waits never imply decision acknowledgement. Omitted rows replay globally
oldest-first (aware timestamp, event ID), before the prompt limit. There is no
defer rotation or timestamp watermark. Legacy completion cursors are ignored:
eligible history conservatively replays because old finishes prove no handling.
Withdrawn, superseded and revision-stale decisions never emit; history is never
rewritten. A safely read corrupt per-token receipt isolates only that event
while unrelated events still deliver; see [the receipt contract](configuration.md#decision-event-receipts)
for bounds, diagnostics, repair, retry and migration limitations.

In `own` mode a deferral alone still never admits a run: event-triggered and
unchanged-code dispatch exit unless a substantive event or due wait is pending.
A deferral rides along when the role runs for other reasons, pending until
explicitly acknowledged or its topic moves out of eligibility. Admission checks
substantive pending decisions independently of the prompt-size window, so older
deferrals cannot hide a substantive admission trigger. The delivered window is
nevertheless globally oldest-first; role policy defines handling, not mechanism.
A registered structured wait is an additional reconsideration signal, not a gate
on delivery; subscribe to substantive actions only to gate on the wait. The revisit
prose is never parsed as an executable condition. A deferral may instead
register one structured wait (``deadline``, ``code_change``,
``insight_decided``, or ``dependency``) through the decision interface; the wait binds to the
current revision and decision, survives restarts as a recorded file, and is
checked lazily at dispatch, so a satisfied wait admits focused
reconsideration without polling or model calls until explicitly handled.
Structured wait and obligation rows carry a persisted registration identity
(with a stable digest fallback for legacy absent IDs) and a host-issued token
bound to role, source and canonical payload. `finish.ack_events` selects handled
rows, validated before intent alongside decision and terminal tokens. Only after
a durable successful result/publication does the host remove a selected due
wait whose registration still matches, or write a create-only per-role exact
obligation receipt. Empty/omitted selections acknowledge none; partial writes
leave unwritten rows pending, and replay is idempotent. A mid-run re-registration
survives even at the same timestamp. Global oldest-first bounded scans filter
obligation receipts before limiting. Registration reads and token-bearing rows
are capped at 1 MiB, receipt reads at 4 KiB; corrupt/unsafe receipt paths fail
closed without repair. CI wait rows sharing `wait_events` retain their separate
acknowledgement behavior; CI/status and check jobs do not use these tokens.
A ``dependency`` wait names an explicit ``recipient`` role as the visibility route
(not assignment) and binds the assessed requirement as
``requires`` (result insight, exact revision, required substantive decision): the completion
is satisfied only by a revision-current decision matching all three, so a moved result never
satisfies a stale requirement and dispatch alone never counts as fulfillment. Unsatisfied
obligations are visible to the named recipient when it runs for other reasons but never admit
a run on their own, so scheduling the fulfiller stays operator configuration and no new wake
source is implied; the satisfied completion reaches the requester idempotently with current
result evidence. Triggers reach scheduling
without a model poll loop: the writer gate consults decision events and due
waits before suppressing dispatch, and rendered service definitions add
platform file triggers (systemd path units, launchd WatchPaths) that invoke
`mizu run` ahead of the next interval; periodic discovery stays independent
policy and trigger runs with nothing due report unchanged. Retained run
records carry their own receipts: admission reasons, finish-intent timestamps
against command records, read-role writable commands, and verification
self-consistency are checkable from retained files alone. Revision, a new decision, or
withdrawal unbinds the wait; unsupported conditions are refused at
registration, naming the supported kinds instead of implying a schedule.

Insight archival is currently a **storage-only phase**. `inbox/` is the live
working set. On ingest, well-formed current-revision accept/modify/reject/withdraw
records at least `limits.retention_days` old (default 31; 0 disables) move to
`insight-archive.sqlite3`. Pending, deferred, stale and malformed records stay
live. Under `insights.lock`, each `(id, rev)` row stores the complete normalized
body (including evidence and provenance), plus the terminal decision and identity
metadata observed at first archival. A FULL synchronous SQLite transaction and
directory sync complete before inbox unlink. Commit failure keeps the live copy;
unlink failure leaves both copies and a retry converges. Conflicting content at
the same archive key retains the inbox rather than replacing audit. The archive
uses DELETE rollback journals, never WAL; quiescent backup recovers a hot journal
before copying the self-contained database. Archive rows are never deleted.
Decisions remain independently durable in `decisions/` and `decision-history/`.

Current lists, projections, source scans and routine prompts enumerate **only the
live inbox**, not the complete archive. No archive page/cursor API exists yet;
this phase does not claim to bound pending work or all listing costs. Exact ID
reads fall back to the latest archived revision; hex-prefix reads resolve across
both stores and refuse ambiguity (duplicate copies of one ID count once).
Identical submit/revise retries preserve the stored record without reopening it.
A meaningful owner-authorized CAS revision reopens into the inbox, retains the
SQLite row, and writes the prior-revision audit as before. `history(id)` still
returns only superseded revisions from `insight-revisions/`, never archived
current content. Work-item origin checks also resolve archived revisions, and
storage maintenance retains their snapshot references; unreadable archives
refuse maintenance. Backups/restores include the archive.

On an ID read only, the
runtime adds an optional compact `work_item_status` for the original submitter
when the current decision is accept/modify and its seeded item (including a
validated open legacy reconciliation) exists. The projection includes item ID,
status, revision, holder, update time and current-origin staleness, plus only
small relevant status details. It reuses WorkItems decision, sidecar and chain
validation; pending, other decisions, drop-only/unresolved legacy records,
other roles and all list paths keep their existing response shape. This is
observational only: no reconciliation, transition, progress accounting,
scheduling or wakeup, and it adds no insights/work-item grant. A work-item
`done` status is an audited work record only; code completion remains determined
by the writer's published state. The prompt
offer keeps the newest `limits.pending_insights` proposals so recent evidence
is never hidden behind the bound. Worker discretion applies to proposals,
not to the operator's goal or prohibitions.

Deferral evidence may be gathered by a resolver role holding `insights` (to
list pending deferrals) plus the `attach_evidence` capability: it uses
`experiment`/`fetch`/`search` to obtain the missing evidence, then appends an
attributed, append-only entry to the deferred insight itself, bound to the read
rev like `revise` but without changing title, body, or rev. Ownership and the
revise/withdraw rights stay with the submitter only: this is not a revision
and no non-submitter revise grant exists. The attach advances the inbox
generation so the decider wakes once; pending lists carry evidence counts with
full entries behind the read path, and a later decision on the topic reaches
its submitter with the evidence included. Entries are bounded (at most 16 per
insight, each at most 8192 chars; bulk belongs in referenced run evidence),
a full insight refuses further attaches until decided, and a meaningful
revision archives prior entries with the old rev and starts the new rev empty.
No non-submitter revise grant exists: ownership never moves.
A resolver role may opt into cross-source `decision_events` with
`decision_sources = "any"` next to `decision_events = ["defer"]` (unset keeps
own-source delivery): matching deferrals from any source remain pending and
admit focused runs until explicitly acknowledged through `finish.ack_events`, so the
resolver runs on each new deferral instead of on a clock. Own-source deferrals
keep ride-along delivery and never admit alone. The operator configures which
role subscribes, to which actions, and its schedule/model/policy; the submitter
owns its topic and takes it back at its next scheduled run when nothing resolved
it. Keeping scheduled resolution opt-in avoids routine polling when explicit
decision events can admit focused work.

Editor uploads are first atomically claimed into a private ingest directory;
they are not parsed while still mutable in the Editor outbox. Malformed records
and non-regular objects are quarantined. A crash after claim can be recovered.
The captured inbox generation belongs to the beginning of a unit, so proposals
arriving during that unit do not vanish behind an advanced cursor.

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
current snapshot pin, proposals not yet offered, plus an `open_insights`
statement of the still-undecided backlog with ids, total, and explicit
truncation so already-offered insights never read as done), never the full
history again; a new, compacted-away, or rotated session receives
the full prompt, which states the same backlog alongside the full entries.
Both modes carry only `goal_digest` for the charter. Every engine dispatch,
including resume, places the ordered role policy and exact pinned charter in
stable system instructions, independently of compactable unit history.
Writers pin the live operator goal, observers pin the snapshot goal, and
children inherit the parent's goal. Normal session fingerprints include that
goal's digest; pi-durable preserves its live-configured store identity instead,
with both resume-authority and turn grants binding the composed system text
through `policy_text` to refuse stale crash recovery.
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
references, capability-gated pending insights and acceptance commands, the
always-present bounded `open_work_items` page, then trailing volatile `run`,
`now`, and `timezone` fields so repeated stable content forms a longer cacheable
prefix); sealing
is directed by the `finish` tool contract. Each run
records `prompt_projection.json` with the sent counts (`recent_snapshots`,
`pending_insights`, `acceptance_commands`) and `prompt_bytes` as evidence for
reliability review. Its `open_work_items` metadata records only `shown`, `total`,
`overflow_count`, `unseeded_decisions`, and `coverage_complete`, never item text.
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
