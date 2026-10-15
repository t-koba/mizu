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
| `pi.py`, `adapters/pi/` | Version-specific RPC and session transport | Model selection policy |
| `codex.py`, `claude.py` | Trusted CLI argv, isolated per-run config, event parsing | Research quality, host-tool absence |
| `mcp_loop.py` | Shared LF-framed JSON-RPC stdio loop for MCP servers | Tool semantics or protocol extensions |
| `mcp_proxy.py` | stdio MCP-to-bridge proxy with no authority of its own | Tool semantics |
| `protocol.py` | Tool schemas and strict parameter validation | Tool implementation semantics |
| `sandbox.py`, `process.py` | Bounded execution, termination, cleanup | Experiment usefulness |
| `snapshot.py` | Content objects, immutable manifests, published pointer | Semantic correctness |
| `insights.py` | Submission identity, revisable proposals with separate audit, decision history, initial work-item seeds | Acceptance or rejection |
| `work_items.py` | Internal append-audited work-item CAS and completion evidence | Public visibility, prioritization and policy |
| `web.py` | Allowed retrieval, policy-bounded protocol observation, and external source receipts | Scientific truth |
| `materialize.py` | Generic offline-input adapter (spec in, digest-bound content out) | Ecosystem resolution, cache policy |
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
8. A writer captures actual files again. Verification is retained as passing
   only when it applies to exactly the captured code. Write a prepared result,
   publish the immutable snapshot pointer, then finalize the run record.
9. Remove active state and reproducible observer input; dispose processes and
   containers. Evidence and model sessions are retained.

`current.json` is the publication commit point. A crash may leave a prepared
result with or without publication; inspect the pointer rather than treating a
prepared record as completed. The last published state remains readable while a
writer is active. No rollback of uncommitted files happens automatically.

Atomic replace plus fsync and locks provide local filesystem durability under
standard local filesystem semantics. Use a local filesystem, not NFS/object-store/FUSE mounts
with weaker locking/rename guarantees. Disk/host failure recovery is not a
substitute for independent backups. Snapshots have hashes, not digital signatures.

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
at run boundaries, not live-active telemetry, and does not report global
concurrency.

## State and history

A snapshot ties together goal, compact state, code digest, verification evidence,
summary, outcome, run ID and wake conditions. Code objects are content-addressed
and deduplicated. Files are byte-preserving; executable status participates in
the code digest. Symlinks, hardlinks and special files are not materialized as
ordinary source. Plain-directory import refuses unsupported skipped entries;
verification cannot pass if the captured tree contains them.

The history index retains published IDs up to the configured limit (default
128, tunable 8–1000000 via `limits.history_index`); full manifests/objects remain on
disk. Prompts carry a bounded, capability-gated projection: up to
`limits.prompt_snapshots` recent anchored summaries with exact IDs and code
digests, plus only the actionable context the role is granted to use.
`pending_insights` is offered only to roles with `insights`/`decide`, newest
kept up to `limits.pending_insights`;
`acceptance_commands` only to roles with `verify`. Exact snapshot references
(`id`, `code_digest`, `state`, `verification`) are always pinned, never
summarized. `diff` compares the anchored
snapshot with the preceding distinct code digest. Large and binary files are
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
on the operator-selected `ci_branch` (polled red, independent of insight decisions, until green or superseded; empty disables the wake) for roles with `ci_wake` (writable roles by default; an unchanged red re-wakes only on a new failing sha/check, a moved head, or every `ci_rewake_seconds`). `blocked` needs a wake or new
information. `done` is not reopened by news alone; an explicit wake or changed
goal can reopen it. Other scheduled roles are independent until the project is
paused/disarmed.

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
via the runtime, which fixes source to the role name and binds to the read rev. History stays a read path. Decisions carry the same binding:
the decide interface accepts the `rev` from the read, and a decision naming
a superseded revision is rejected without writing anything, so a prior
rationale never authorizes changed content while the current revision stays
pending for reassessment. Prior
revisions are returned only by the explicit history path, never injected into
routine context, and are durable audit like decisions (backed up, never
pruned). Direct ID reuse with different content outside revise is rejected.
Decisions bind to the reviewed `rev`, so a prior approval never authorizes
changed content; deferral still requires a revisit condition and stays
pending. An `accept` or `modify` decision also places an initial durable
`work_item` seed in that same immutable decision-history event (and the
matching current-decision record). Its ID is `wi_<event-uuid-hex>`; it records
the current insight title and `{insight_id, rev}` origin, starts `open` at item
revision 1 with no holder, and uses the decision timestamp for creation and
update. `defer` and `reject` do not create a seed.

`Project.work_items` provides an **internal-only** `get(item_id)` and
`transition(item_id, *, expected_rev, status, actor, reason=None, wait=None,
snapshot_id=None)` layer. It resolves the seed directly from
`decision-history/<event-hex>.json`, validating the accept/modify event and
nested seed without scanning the inbox or decision history. The event's title
is the immutable title authority, not the subsequently revisable insight.
Reads and mutations share `locks/insights.lock` with insight revisions and
seed creation. A positive non-bool expected revision must match; each change
exclusively atomically appends the full state to
`work-items/<item-id>/<next-rev>.json`. There is no mutable index or pointer.
Reconstruction requires consecutive revisions, with at most 256 revisions
including the seed and 128 KiB per seed/event record. Unsafe links, unknown IDs,
malformed/oversized records, gaps and exhausted bounds fail closed; a stale
CAS writes nothing and must be re-read, not blindly retried. `get()` and
`transition()` recover under the shared lock by removing only recognized
regular `.new-<8-char>` atomic-write leftovers; an uncommitted temporary is
never promoted, and a post-link alias
is removed only when it matches one revision inode. Unknown names and unsafe
links still fail closed. Completion reads revalidate the cited manifest and
require its stored passing verification to match the copied evidence, without
requiring the snapshot to remain in the moving publication history. Lock/I/O
failures propagate; cancellation/process death releases the kernel lock.

Statuses are `open`, `doing`, `blocked`, `done`, `dropped`. Same-status changes
and changes from terminal `done`/`dropped` are refused; other statuses can move
to any different status. Title/origin/creation time stay fixed; every event
records its revision, update time, caller-supplied actor, from/to transition,
and explicit reason/wait/snapshot/verification fields (null when absent).
Actor is trusted internal attribution, not a new authorization grant. `doing`
assigns the actor as holder, `blocked` preserves the holder or assigns the
actor, and other statuses clear it. Blocking requires a wait normalized by
the existing Insights validator, but registers **no scheduler wait or wake**.
Dropping requires a nonempty reason. Completion requires `Snapshots.get()`
validation and membership in retained published history. The stored
verification must have `passed is True` and a code digest matching the
snapshot. The snapshot ID
and verification are copied into the immutable completion event, surviving
later publication-history retention. Backup/restore includes these histories;
storage audit/reclaim protects their cited snapshot manifests. Work-item
retention/expiration is not implemented, so those citations remain durable.

`projection(item_id)` returns one bounded view (`item`, `current_origin_rev`,
`stale`, `redecision`) without changing the immutable seed or CAS record. A newer
origin revision is stale until that revision has an `accept`, `modify`, `defer`
or `reject` decision; `withdraw` does not clear staleness. Re-decisions must
match the action-specific decision schema; an accept/modify must also match its
immutable decision-history event and pass seed validation. Missing current
origin data is reported stale, while unsafe or malformed origin records fail
closed. This projection does not yet gate internal transitions.

`page_open(cursor=None, limit=25)` is an additional internal read-only page,
not connected to runtime tools, prompts, explain, or status. Under one
`insights.lock` section it scans committed `decision-history/*.json`, validates
and reconstructs every seeded item (including closed states), and returns only
`open`/`doing`/`blocked` rows in deterministic item-ID order. The cursor is an
exclusive item-ID boundary; `total` counts all currently open seeded items,
independent of the cursor. Accept/modify history events without a `work_item`
seed are counted as `unseeded_decisions`; `coverage_complete` is false while
any exist, and their current state is never inferred. Rows include current
origin staleness/redecision, a structured wait for blocked items, and an
optional reason preview of at most 1024 UTF-8 bytes. Limits are 1–100 rows,
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
also recovered under the lock. Paging is a live view across calls, not a transaction snapshot. The exclusive
last-key cursor avoids offset shifts when an earlier row closes, but new IDs
that sort at or before the cursor may be absent from that continuation; an
empty page after a cursor does not mean the collection is empty. `total` and
`coverage_complete` describe only each call's locked scan. A fresh traversal
starts with a null cursor and checks `total`, `truncated` and
`coverage_complete` on each page, but still receives no transaction-consistent
multi-page snapshot. No public visibility or free-text Tasks-retirement claim
follows from this internal method. Public
prompt/tool/CLI/status/explain visibility, policy configuration,
backfill/reconciliation and replacement of free-text Tasks remain unfinished.
**Free-text Tasks in writer state are still needed.**
Author withdrawal (`mizu insight withdraw`, submitter or operator only)
closes a pending proposal with an actor-labelled rev-bound record in
the same decision store; it is refused over a revision-current substantive
decision and over an unseen revision, a repeated request is a no-op, and
`reject` keeps meaning substantive rejection. A role with configured
`decision_events` is additionally admitted by unacknowledged decisions routed
to it (insight source equals role name in `own` mode, any source when
`decision_sources = "any"`): the dispatch delivers the current
finding plus the reason as a `decision_events` field inside the role's ordinary prompt
(all other granted inputs stay present), described factually by a `decision_task` field,
records the admission and the
delivered events under the run directory, and advances a per-role acknowledged
cursor over substantive outcomes only after successful processing, so pause, budget,
concurrency, and restart keep their authority and nothing is lost or repeated.
Withdrawn or superseded decisions never emit, and a substantive decision never lands on a
withdrawn revision. Substantive delivery is one-shot per decision; a deferral rides
along when the role runs for other reasons and rotates across dispatches while still
current, whatever its age against the cursor, and counts as handled only when its
topic moves (revision, withdrawal, or a superseding decision). In `own` mode a deferral alone never
admits a run: event-triggered and unchanged-code dispatch exit unless a substantive
event or due wait is pending, so standing deferrals cost a cheap skip rather than a
model run. Truncation keeps substantive delivery lossless: unacknowledged substantive events
come before the rotating defer window, oldest-first within each class, so every deferral
stays reachable instead of pinning the oldest window. A deferral with a
registered structured wait still rides along while defer is a subscribed trigger; to gate
reconsideration until the condition holds, subscribe to substantive actions only and rely
on the wait. The prompt describes delivered kinds factually (substantive counts, ride-along deferral counts, and what counts as handled), so an unchanged deferral returning in a
later window instead of vanishing is visible without directing behaviour; handling is defined by the role policy. The revisit
prose is never parsed as an executable condition. A deferral may instead
register one structured wait (``deadline``, ``code_change``,
``insight_decided``, or ``dependency``) through the decision interface; the wait binds to the
current revision and decision, survives restarts as a recorded file, and is
checked lazily at dispatch, so a satisfied wait admits one focused
reconsideration without polling or model calls. A ``dependency`` wait names an explicit
``recipient`` role responsible for the external work and binds the assessed requirement as
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
registration, naming the supported kinds instead of implying a schedule. Decided (non-deferred,
revision-current) proposals older than
`limits.retention_days` are reaped on ingest; their decisions persist in
`decisions/` and `decision-history/`, so inbox scans stay bounded. Decided
work stays retrievable within retention: the `insights` tool lists decided
proposals by action and reads by full id or unique prefix, so accepted work
never depends on the model's memory across compaction. Tracking
implementation completion ("done") stays with the writer's published state;
the mechanism records decisions, never per-item done-ness. The prompt
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
own-source delivery): matching deferrals from any source then deliver once and
admit one focused run each, acknowledged after successful processing, so the
resolver runs on each new deferral instead of on a clock. Own-source deferrals
keep ride-along rotation and never admit alone. The operator configures which
role subscribes, to which actions, and its schedule/model/policy; the submitter
owns its topic and takes it back at its next scheduled run when nothing resolved
it. The scheduled-resolver experiment (qpx 15 deferrals waiting on an
already-green fact, most deferrals without structured waits) justified this
opt-in admission over shortening the clock (Less-is-more).

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
by a short, nonblocking `queue.lock` transaction. The pending cap is derived
from `parallel_runs` (one waiter per configured slot), separate from the active
`N.lock` file-lock cap. Only retrying callers enqueue; overflow and fail-fast
contention remain `Busy` infrastructure deferrals. A retrying ticket may skip
older tickets whose role/workspace locks are unavailable, but never a ready
one. Skipped tickets retain their original FIFO position. Fail-fast callers,
including recovery probes, refuse admission while any live ticket is pending.

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
hex `id`, a unique integer `owner` in `[0, parallel_runs)`, and zero to two
`resources`. Resources are canonical `project/locks/run-role.lock` or
`project/locks/workspace.lock` names relative to `data/projects`, with project
and role using the normal identifier schema (at most 63 characters). Absolute
paths, traversal, aliases and other lock names are invalid. Ticket count and
the file read/parser byte bound are derived from `parallel_runs` and the largest
canonical ticket. Queue state is opened nonblocking and without following the
final symlink where the OS supports those flags; `fstat` must identify a regular
file before any read. FIFOs and other special files fail closed with generic
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
pending-cap or total-state-byte policy; memory usage still scales with actual
state bytes. Oversized, unreadable or malformed state fails closed with `Busy`
before liveness or resource probes or queue rewrites. This is local filesystem
coordination, not a remote or network-filesystem scheduler. It does not rank cache affinity, preempt a
running driver, or change caller slot lending or writer workspace ownership.
Only the live `Engine.run` caller owns that lease; unadmitted calls fail with
`Busy` before child inference, and children cannot lend onward. Consultation
fan-out stays bounded by `parallel_consults` and shares the daily budget;
answers fail independently and cannot edit code. Delegation retains the caller's
workspace lock throughout the join, preserving one writer.
A resumed persistent session receives only an append-only delta (goal pin,
current snapshot pin, proposals not yet offered, plus an `open_insights`
statement of the still-undecided backlog with ids, total, and explicit
truncation so already-offered insights never read as done), never the full
goal and history again; a new, compacted-away, or rotated session receives
the full prompt, which states the same backlog alongside the full entries. Rotation is operator policy via `[limits] session_max_tokens`,
`session_max_cost_usd`, and `session_max_age_seconds` (0 disables each, age bounds
conversation lifetime from its stable creation surviving same-id saves):
a due rotation removes `session.json` so the next dispatch mints a fresh
provider session while the published snapshot and composed policy reload
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
references, capability-gated pending insights and acceptance commands, plus the
current wall-clock time and configured zone as trailing `now`/`timezone` fields
so the stable prefix keeps working caches warm); sealing
is directed by the `finish` tool contract. Each run
records `prompt_projection.json` with the sent counts (`recent_snapshots`,
`pending_insights`, `acceptance_commands`) and `prompt_bytes` as evidence for
reliability review. All work
judgment lives in the role policies under `policies/`. Likewise the `report`
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
