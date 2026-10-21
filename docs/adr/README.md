# Architecture decisions

One short entry per lasting decision: context, decision, consequence.
Interface schema, bounds, trust, retry/cancel, evidence, and failure
stay in the module docstrings and docs/.

## ADR-001 — Local files and operating-system supervision
JSON/Markdown files, atomic publication, flock, and systemd user units
instead of a database, queue, and scheduler. Records stay inspectable and
recovery explicit; multi-host coordination is not supported.

## ADR-002 — Isolated commands, not host tool wrappers
Model commands run in a rootless container with fixed mounts and limits;
the editor is a separate capsule. No host shell fallback and no
permissions that are policy text only.

## ADR-003 — One integrating writer, many independent observers
Models and review processes submit evidence or proposals; one worker owns
integration and records each disposition. Read snapshots anchor to one
published state, avoiding cross-agent edit races.

## ADR-004 — Explicit update promotion
The operator stages checked source and flips a symlink only while
projects are paused and services stopped. No model self-deploys or
changes safety policy; private configuration is preserved.

## ADR-005 — Accountable limits, not invented monetary guarantees
Enforce provider admission counts, runtime, output, and container
resources outside the model, and retain reported usage. Request counts
are never presented as currency limits; billing caps stay explicit.

## ADR-006 — Mechanism/policy separation line
The mechanism owns safety, resource, and evidence invariants plus the fixed commit
vocabularies; policy owns who, when, in what words, and what counts as good. A knob is
added only when an operator would plausibly turn it; otherwise the constant stays in code, named here.
Fixed in code (not knobs): smoke probe 4 requests / 5 tools / 120 s (`smoke.py`); framing
`PREVIEW_BYTES` 128 KiB / `READ_PAGE_CHARS` 8,192 / `MAX_FRAME` 1 MiB / `SCRIPT_MAX` 64 KiB (`fs.py`); include
8 deep / 32 files / 1 MiB total / 32 entries per file (`config.py`); role policy 16 parts /
64 KiB each / 256 KiB total; sandbox verify-command fan-out 32 (`project.py`) and
`--userns=keep-id` floor (`sandbox.py`); lock liveness 30 s (`fs.py`); usage scans
5000 runs / 200 recent entries (`usage.py`); fault taxonomy `INFRA_WAIT=(Busy, InfraExceeded)`
(`runtime.py`/`errors.py`). Tunables live in `[limits]`/`[vcs]` with defaults in
`docs/configuration.md`.

## ADR-007 — Current native engine adapters
Profiles select engine, provider, model, session mode, and native options explicitly,
forwarding native thinking/effort values without a common list. Role capabilities authorize
Mizu operations and `engine_tools` authorizes extra native tools; approvals never expand grants.

## ADR-008 — Upstream sync via trusted argv adapter and `sync` capability
The operator-configured `[vcs]` command speaks bounded JSON over stdin/stdout with a
timeout and no shell; the host fetches upstream refs and injects them read-only. The
`sync` capability lets the writer merge or rebase, resolve conflicts, then verify and publish.

## ADR-009 — VCS publish behind recorded human approval, read/publish split
`vcs_read` cannot publish; `vcs_publish` needs an accepted `GO <branch>` insight carrying
the exact `code_digest`. Stale digests are refused; CI failures become deduplicated insights
keyed by branch, sha, and check.

## ADR-010 — Config `include` of shared TOML fragments
A root-only `include` array merges shared fragments before validation, giving one reviewed
base across configs. Tables deep-merge, arrays never concatenate, and duplicate leaves name
both files; paths stay relative to the includer with symlink, depth, and size bounds.
Bounds (fixed, `config.py` `MAX_INCLUDE_*`): depth 8, files 32, total 1 MiB, 32 entries per
file, entry 4096 chars, `.toml` only. Full schema/bounds/trust/retry/evidence/failure in
`docs/configuration.md` "Shared fragments" and `config.py` docstrings.

## ADR-011 — Non-blocking work via park-and-continue worker policy
No new mechanism: while runnable work remains, the worker parks the stalled item in state
and finishes `continue`; `blocked` is reserved for when every item waits on the operator.
`needs_operator_input` keeps meaning "nothing further without the operator".

## ADR-012 — AGENTS.md holds contributor rules only
`AGENTS.md` keeps contributor rules and points at the operator docs for
deployment, credentials, scheduling, and promotion. Procedure lives in one
place, so contributors cannot mistake it for something to encode in code.

## ADR-013 — Unambiguous smoke probe with a bounded 4-request margin
The paid, read-only live probe names the exact file to read and caps spend
at 4 requests, 5 tools, and 120 s, clamped below operator limits. Paid
spend stays bounded while one benign extra step fits. The probe system
prompt is fixed probe policy, independent of operator consult text, so
smoke tests the engine/provider path only.

## ADR-014 — Stable CI dedup body excludes the volatile log URL
CI failure insight bodies cover branch, sha, check, and the trust label
only; the per-run log URL varies, so persisting it broke dedup and aborted
the poll tick. The latest URL stays available through `vcs_read`.

## ADR-015 — Rootless Podman keeps the host uid via `--userns=keep-id`
Rootless Podman runs pass `--userns=keep-id` alongside `--user`, keeping
the host uid inside the container so the workspace stays readable with an
empty user `containers.conf`. Docker and single-id modes are unchanged.

## ADR-016 — Resume rebase plus infrastructure-wait deferral
`mizu resume` resets per-role `consecutive_failures` to 0; `Busy`/`InfraExceeded` waits
defer instead of counting toward the auto-pause brake. Errors are still recorded and raised,
so real faults stay visible while budget, disk-reserve, and lock contention wait.
Gate is `INFRA_WAIT=(Busy, InfraExceeded)` on exception type via `is_deferred(exc, context)`
(`runtime.py`); only daily-budget and disk-reserve guards raise `InfraExceeded`
(a `LimitExceeded` subclass), never by message text.

## ADR-017 — Narrow infra-wait deferral to budget/disk guards
The deferral gate narrows from `(Busy, LimitExceeded)` to `(Busy, InfraExceeded)`: only
daily-budget and disk-reserve guards defer. A host-side budget refusal on the Pi transport
defers by recorded type, never by message parsing. Every other `LimitExceeded` site
(engine deadlines, RPC/tool/request bounds, file-count and snapshot bounds) still counts
toward the brake; see `runtime.py`/`errors.py`.

## ADR-018 — Operator-channel publication approval
`GO <branch>` approvals count only from the operator channel with an
operator-run accept decision; model submits and decides are refused as
forged. Configs combining publish with insight submit/decide fail to load.

## ADR-019 — Digest-bound publication adapter
`vcs_publish` sends the approved `code_digest` and the adapter verifies the
pushed tree before pushing, echoing the digest back. Missing or mismatched
echoes are refused, binding the approval to the exact pushed bits.

## ADR-020 — Reserved role names keep the operator channel unforgeable
Config load refuses the role names `operator`, `vcs`, and `editor`, so a
model role cannot author insight bodies that a later human accept would
record as operator-authored.

## ADR-021 — Bounded opt-in daemon VCS polling
Daemon upstream and CI polling require `vcs.poll_enabled` plus a command, at the configured
cadence. One project polls once per tick under a lock with shared poll state; over-long branch
lists and timeouts become poll events rather than daemon failures. Cadence and caps are policy:
`poll_interval_seconds` 300, `poll_status_timeout_seconds` 15, `poll_fetch_timeout_seconds` 30,
`poll_max_branches` 4 (see `[vcs]` in `docs/configuration.md`); worst case ~90 s per tick at defaults.

## ADR-022 — Per-project request budget with an optional shared total
Day-files keep a shared aggregate plus a per-project map, so `daily_requests` caps each project
and `shared_daily_requests` (0 disables) caps the shared total. Budget reports show both scopes,
so one project's usage no longer stops another under a smaller limit.

## ADR-023 — Digest-gated on_change observers
`on_change` compares `code_digest`, not snapshot id, so state-only
republications do not reschedule observers. A no-action wake publishes the
same digest and observers keep skipping instead of looping model calls.

## ADR-024 — Bounded run-evidence retention for bulky engine logs
Operator-set `[limits]` day counts gzip raw run event logs at N days and drop them at M days
(0 disables a stage); `mizu prune` reports and applies both stages. Result, error, snapshot, and
decision records are never candidates, so usage keeps working. Defaults
`event_log_compress_days` 7 / `event_log_retention_days` 31; age is file mtime, the `.gz`
keeps the raw mtime so the drop clock does not restart, and only top-level
`runs/*/` `*-events.jsonl` and `diagnostics.txt` (plus `.gz`) are candidates.
See `docs/configuration.md` "Limits" and `mizu prune` help.

## ADR-025 — Policy audit moves dashboard projection bounds to config
The audit moves only what carries no safety cost: daemon poll bounds and the dashboard slice
become `[vcs]`/`[limits]` keys with current values as defaults. Spend caps, the fault taxonomy,
framing/memory bounds, sandbox floors, and liveness bounds stay fixed in code.

| Constant (code) | Value | Disposition | Why |
|---|---|---|---|
| `SMOKE_REQUESTS/TOOLS/SECONDS` (`smoke.py`) | 4 / 5 / 120 s | Stays fixed | Paid spend cap with one benign-listing margin; opening it opens spend |
| Infra-wait classes (`Busy`/`InfraExceeded` defer, `LimitExceeded` counts) | type-based | Stays fixed | Fault taxonomy prevents hiding real faults; operator controls brake via `max_failures` plus budget/disk thresholds |
| `POLL_CALL/FETCH_TIMEOUT_CAP`, `MAX_CI_BRANCHES` (`runtime.py`) | 15 s / 30 s / 4 | Moves to policy | Per-tick liveness bound keeps the daemon responsive; now `[vcs] poll_status_timeout_seconds`, `poll_fetch_timeout_seconds`, `poll_max_branches` with the same defaults (worst case ~90 s at defaults) |
| `poll_interval_seconds`, `pending_insights`, `prompt_snapshots`, `history_index`, `retention_days`, event-log days | configured | Already policy | Existing `[limits]`/`[vcs]` knobs with documented defaults |
| `PREVIEW_BYTES`, `READ_PAGE_CHARS`, `MAX_FRAME`, `SCRIPT_MAX`, insight/report/finish/consult/search sizes | 128 KiB / 8,192 chars / 1 MiB / 64 KiB / 60 KiB / 48 KiB / 12+32 KiB / 8 KiB+8 / 1k+4k | Stays fixed | Framing and memory safety: unbounded reads or messages risk OOM and protocol desync |
| `MAX_WEB_WORKER_INPUT_BYTES` (`web_worker.py`) | 3 MiB | Stays fixed | Covers 2x canonical expansion of the 1 MiB TOML budget plus escaped valid probe input; parent and child reject overflow before parsing |
| Sandbox termination, `MAX_VERIFY_COMMANDS`, include depth/files/bytes, web redirect/media, restore `--max-bytes` | 1 s+5 s / 32 / 8+32+1 MiB / 3 / 1 GiB | Stays fixed | Termination, fan-out, recursion, and archive-bomb safety floors |
| `MAX_RUNS_SCANNED`, `MAX_RECENT_ENTRIES`, record size caps (`usage.py`) | 5000 / 200 / 1 MiB | Stays fixed | Unbounded run scans stall usage; records stay complete on disk |
| `MAX_DECISIONS`, `REASON_TRUNCATE` (`dashboard.py`) | 10 / 500 | Moves to policy | Pure projection: raw decisions stay on disk, only the small-screen slice changes (`[limits] dashboard_decisions`, `dashboard_reason_chars`) |

## ADR-026 — In-process lock guard with a fixed 30 s liveness bound
`fs.lock` serializes lock files within the process with a per-path guard keyed by canonical
path, and every blocking acquire is bounded at 30 s: the intra-process guard by
`fs.py` `_LOCAL_BLOCKING_TIMEOUT` and the cross-process (kernel) acquire by `platform.py`
`LOCK_TIMEOUT`, on POSIX and Windows alike. Contention fails as `Busy` instead of hanging the
daemon; the bound is not an operator knob.

## ADR-027 — Composed role policies from ordered Markdown parts
A role's `policy` accepts one path or an ordered array of paths, composed in order as the
system prompt and the policy digest. Operators share principles across roles without an external
concatenation step; any part change re-keys sessions, and missing or oversize parts fail closed.
Bounds (fixed): at most 16 parts, each at most 64 KiB, 256 KiB total; see "Roles" in
`docs/configuration.md`.

## ADR-028 — Persistent-session delta prompt and rotation as policy
A resumed persistent session receives only what changed since its last unit; new, compacted,
or rotated sessions get the full prompt with the exact published state and composed policy text.
Rotation past token, cost, or age `[limits]` keys records the new session key and reason.
Rotation only freshens context: the generation bump lands before the old
session record is removed, so a failed transition never silently resumes
the prior conversation, and an abandoned generation store is retired only
after it verifies as terminal (every run settled, no started/unknown
turn) — active, unknown-completion, corrupt, or uninspectable stores are
retained for the operator. Time-based reaping stays per-store and likewise
requires known terminal run/turn states and a recorded, valid result; otherwise
the store remains available as recovery evidence.

## ADR-029 — Budget day follows the configured timezone
Day-files name the date in `timezone` (UTC default unchanged when unset) via
`config.resolve_timezone`, so the reset that fired at 09:00 JST now fires at
midnight Tokyo time; each record carries the `zone` that wrote it alongside the
ISO date. Projects sharing one `data_dir` should share one `timezone`: different
zones name different days for the same instant, splitting the shared total across
day-files. Reaping (`retention_days`) uses each caller's zone window and stays best-effort.

## ADR-030 — Immutable publication receipts anchor exact-SHA waits
Publication identity is a per-publication receipt, not a mutable branch head: it
binds the source Git commit and approved `code_digest` to the exact destination
branch and published commit SHA, plus the approval/run reference. The trusted
adapter must return that published SHA under a stable publication ID; Mizu does
not infer it from a later branch observation or CI result. Store the receipt as
durable project evidence and let waits address it directly rather than scanning
run history. Record intent before the external side effect; retry/recovery must
use the same publication ID, and an uncertain outcome without adapter
reconciliation stays unresolved rather than creating a second publication.
`code_digest` remains the exact-content approval binding, distinct from Git
commit identity.

The first structured CI wait targets the receipt's published SHA, never merely
the branch. Its declared check predicate is satisfied only by terminal evidence
for that exact SHA (green or red); missing or pending check evidence leaves it
unresolved, and failure wakes the item but does not mark the work done. Branch
movement cannot satisfy or erase an older wait; if exact-SHA evidence is
unavailable, the wait remains visibly unresolved rather than inferring a result
from the new head. Review, promotion, and installation waits remain later
slices.

## ADR-031 — An operator-run publisher may record its publication post hoc
The operator's own harvest publishes approved commits to the development branch
outside the product, so those publications leave no durable source-to-published
identity and no wait can be pinned to their published SHA. A publication by an
operator-run publisher may therefore be recorded post hoc as a durable receipt
anchored by code digest, branch, published SHA and approval reference. The
record is create-only and idempotent: an identical request replays the stored
receipt, a conflicting one is refused, and the write never overwrites. It is
readable through the same validation path as the product's own receipt, so an
exact-SHA CI wait can be pinned to it.

Consequence: the product never infers a published SHA from a branch head. A
recorded receipt is operator-asserted evidence; CI evidence must still confirm
the exact SHA it names, and the product's own publish path keeps its
intent/dispatch/receipt trio unchanged.

## ADR-032 — JSON nesting depth is a product bound, not an interpreter limit
`loads_finite` bounded a JSON document by size and by the runtime's recursion
limit, so the accepted nesting depth depended on the Python version: a deeply
nested frame was refused on 3.11-3.13 and parsed on 3.14, where it reached a
tool handler. The product supports all four versions, so the same hostile input
must be refused everywhere. `MAX_JSON_DEPTH` (64) is therefore a fixed
mechanism bound beside `MAX_FRAME`, and a document deeper than it is refused
before it is parsed, so a hostile frame costs no allocation and no version
sees a different outcome. It is not a knob: no legitimate frame, tool
argument or recorded document in the product nests that deep. The bridge, the
MCP loop and the MCP proxy keep their existing invalid-frame refusals, so the
depth refusal is reported the same way as any other unparseable frame, and the
`RecursionError` mapping stays as a backstop.

## ADR-033 — A capture limit names its cause and reaches the writer
The snapshot capture failed closed on a workspace file above the per-file byte
limit, but the error carried no path: the run ended interrupted, its finish
was never published, health counted a failure, and three such runs paused the
project without telling the writer that owned the workspace what to remove.
Every capture bound now names what tripped it, the path with its size against
the per-file limit, the running total against the snapshot byte bound, or the
file count against the count bound. A writable role's `finish` checks the same
bounds before it seals, so an unrepresentable workspace is refused with the
offending path while the writer can still remove or ignore the file and finish
again in the same unit. The capture stays authoritative at publication time;
the finish check only reads file sizes, and a failure there names its cause in
the error itself, so the publication path reaches health as well. The recorded
cause is the one that still applies: it is set when a capture fails, kept
while it applies, and cleared when a capture succeeds. It is recorded in the
run record, the health record and the pause reason, and the next unit of that
role reads it from its context, so the same stray file is not rediscovered
from scratch and a resolved cause is never read as a live blocker. Nothing is skipped or truncated: a file the snapshot cannot hold stays
a visible refusal, and the limits keep their current defaults.

## ADR-034 — Both Pi engines resolve the pinned model from the same source
The two Pi engines resolved a profile's exact `provider/model` differently: the
`pi` engine built its model runtime from the engine directory's `models.json`
plus Pi's bundled catalog, while `pi-durable` resolved built-in providers only
and refused an operator-defined model with "Configured exact model is
unavailable on pi-durable". The persistent worker therefore could not run a
model that every other role could, and Pi's bundled catalog trails provider
releases by days. Both engines now resolve the pinned model through Pi's own
`ModelRuntime` over the same two sources, so one `models.json` defines a model
for every role. The durable adapter reads the shared login store through Pi's
own file-backed auth store, which removes the in-adapter credential store and
its lock handling. Everything that held before still holds: the exact pin
fails closed for an unknown model, a provider without configured auth never
reaches a model (the durable engine refuses before the submission, while the
pi engine's session fails at the provider), per-request admission and usage are
unchanged, and credentials stay out of evidence.

## ADR-035 — CI runs are requested at an exact published commit
CI starts from a push, so every publication spends a run whether or not the
work needs one, and a writer cannot ask for evidence about the exact commit it
published. `vcs_request` asks the provider for one run per workflow at one
exact published commit: the target is a completed publication receipt, never a
branch head, and the product itself verifies that every returned run names
that commit, so a provider that cannot prove the binding refuses instead of
guessing. The request and the returned run identities are recorded durably; a
dispatch marker is written before the provider is called, and an unresolved
dispatch is never retried blindly, because a second request would spend a
second run for the same commit. The role capability and the operator's
`request_grant` are both required and both off by default, so the mechanism
exists without any role able to trigger it until the operator grants them.
Ref handling stays provider policy: the product never creates or deletes a
ref to target a commit.

## ADR-036 — A drain is recorded as a drain, and a drained unit keeps its finish
A drain that reached a running `pi` unit was recorded as if the unit had run out
of requests: the run ended interrupted, its finish was never published, and the
record said "Per-run provider request budget exhausted", which points an
operator at the wrong bound. The shared admission handshake now carries the
bound kind, so a drain is named a drain wherever it surfaces (run record,
health, events), the durable launcher reports the kind it was given instead of
always the request budget, and a drained unit on the `pi` engine takes the
same wrap-up as on `pi-durable`: it records its finish state and yields instead
of being interrupted as an admission failure. The request budget keeps its own
cause, and the per-request budget bound is unchanged.

## ADR-037 — A CI minute is runner wall time, and the per-day view is a local sum
The cost of a CI run is not a bill. A provider charges for minutes only where
it charges at all, and what it charges applies per-job rounding and per-OS
multipliers the product never sees, so the product derives its own measure: the
sum of the `job_seconds` it recomputes from each check's reported instants.
That measure is runner wall time, and it is labelled as such, never as a
saving. The adapter contract this product drives exposes
no provider usage endpoint, so nothing about a provider's billing enters the
measure.

The per-day view that groups those seconds by day is decided here and is not
built yet, because the provider's status rows carry no check instants; the
cost-visibility work item owns it. The requests a unit spent are already
visible per day, through the local listing of the recorded requests, and a
run's own record names the requests that run made, with their identity, their
day, the work items they covered and whether each one is still unresolved. A
request is dated by its dispatch, the day the provider was asked and a CI run
may have been spent; a later answer never moves it. A dispatch whose outcome
is unknown is named rather than hidden, because a CI run may have been spent,
and an outcome record that cannot be read leaves the request named as
unresolved rather than hiding it. The seconds view will count a re-run again,
because a re-run spends
runner time, and it is never presented as a saving. Until then no document may
describe such a view as existing behaviour.

The seven-day bound on a reported job is a bound on an untrusted duration:
the product refuses a value it cannot sanity-check, and the refusal covers
the whole status response rather than silently dropping one row. A provider
can report a longer span (a self-hosted agent whose job timeout is
unlimited), so the bound refuses values a provider may produce; it is a
product rule, not a claim about every platform.

## ADR-038 — A closure names the tree that carries its delivery
A work item was closed against any retained published snapshot with a passing
verification, so the evidence could speak for a tree the delivery never
reached: the closure named one tree and the reason named another. The ledger
now derives the tree the workspace presents and binds the closure to the
published snapshot that carries it, so the record states the delivery tree
instead of asserting it in prose. The caller supplies nothing new: the
delivery tree is derived, the writer's operation keeps its parameters, and a
closure whose tree cannot be derived fails closed.

An older published snapshot that carries the same tree is still a valid
binding. An ancestor with a different tree is not, and the reason is what the
ledger can establish rather than what the tree happens to be: the delivery
happened at an unknown point in the workspace's history, so the only tree the
ledger can show contains the delivery is the one the workspace presents.
Binding an older tree would be a guess about where the delivery happened, so
the closure names the tree it can prove and the evidence is that tree's own
verification.

The consequence is a closure window, and it belongs to the rule rather than to
an operator's convenience: the tree a closure names is the tree the workspace
presents when the closure is made, so that tree must be published and verified
first. A delivery made in an earlier unit is therefore closed against the
current tree, which contains it, and a verification that has not passed blocks
the closure until it has. The writer waits a unit for the publication rather
than binding a tree that does not carry the delivery.

The claim is the binding, not a copy of it: the record's copied verification
carries the bound snapshot's code digest, and that digest is the tree the
closure names. The record repeats it in `delivered_tree`, which also marks the
closure as one made under this guard; a record written before the claim
existed states no tree and stays readable history. The product's own write and
restore paths never rewrite a record's fields, so the claim cannot be lost by
them; an operator's repair that rewrites records must preserve the field,
because the ledger keeps no era marker that could tell a rewritten record from
a legacy one.

The guard is a property of the ledger that accepts the closure, not of the
published tree: a deployment that does not carry this tree accepts closures
without the claim, and the records it writes state no tree. The published tree
carries the mechanism; the operator's deployment puts it in force.

## ADR-039 — The container sweep runs under the role's admission exclusion
A role invocation holds its role's exclusion for its whole run, so the
kernel releases that lock exactly when the invocation ends, whether it
finished, failed or was killed. The service cleanup that removes leftover
labelled containers took no such lock, so a sibling service's stop cleanup
could remove the containers of an invocation that was still running. The
sweep now runs under that exclusion: the invocation that owns the role
sweeps its own leftovers, and any other sweep is refused while the role is
live and reports the refusal instead of removing anything. Once the owner
exits, the next sweep removes the orphans. A delegated child is an
invocation of its own role and takes that role's exclusion for its whole
life, so a role runs once at a time in a project, delegated children
included, and a delegation to a role that is active is refused. A host-side
workaround that holds the same lock around the cleanup command must be
removed when this ships: two holders of one inode would keep the sweep from
ever removing an orphan.

The exclusion has one definition, in the module that admits invocations, and
every caller takes that path: the invocation, the sweep, the delegated
child, the storage maintenance and the recovery probe. A sweep that
protected a different file than the invocation holds would leave the defect
in place with a green suite, so the paths are shared rather than written
out, and a regression runs a live invocation and shows that the sweep finds
its exclusion.

A project-wide sweep excludes every configured role together with every
exclusion that exists in the project, and both are needed. The existence
of a lock file is not a liveness fact: the file appears when a role takes
its exclusion and is never unlinked, so a role that has not run yet has
none. The configured set is what closes the window: the set is read before
the container listing, so a role that starts inside that window would
otherwise be unprotected — but a delegation only names a configured role,
so holding every configured role's exclusion keeps any role from starting
inside the sweep's own window, including a delegated child whose role the
project does not enable. The storage maintenance takes the same set, so a
delegated child is excluded from maintenance as well.

## ADR-040 — A delegated child stops when the project drains
A drain stops new runs and moves a persistent session to a reduced-grant
wrap-up, but an ephemeral delegated child had no such move: it kept running
until its own deadline while its parent waited for it, so the draining
parent could not finish either. A delegated child now stops at the same
safe boundary the drain wrap-up uses. No request is admitted after the
drain, no in-flight work is discarded, the work the child already did
stays in the caller's workspace, and the child reports the interruption
to its caller, which then seals under the drain bound. The outcome is
truthful rather than silent, and a draining parent is no longer held by a
child that cannot finish.

One rule holds for every engine. The admission paths differ — the Pi
handshake, the shared admission the Claude and Codex adapters use, and the
durable launcher's reported bound — so the refusal the drain bound requires
at an admission boundary lives in one place, and each path asks for it. A
delegated child is stopped by all of them; a unit with a session to preserve
wraps up under the reduced grant as before.

The Pi engine crosses the drain boundary from two places, not one: its
request admission and the transport poll it makes between provider
requests. A child that meets the drain between two requests is stopped at
that poll too, by the same refusal, so the child's outcome does not depend
on which crossing noticed the drain.

The child's refusal is the shared handshake's own text, the one the durable
launcher classifies as the drain bound. A refusal it does not recognise is a
provider failure, so a child's stop would be recorded as a launcher failure
instead of the bound the drain is; the host therefore classifies the child's
stop itself, before the adapter's error mapping can turn it into a wrap-up.
One vocabulary, one rule, every engine.

## ADR-041 — A long check runs as a job, and the unit does other work while it runs
A verification, an experiment or a read-only command blocked the worker for
its whole duration, so the unit could not read the ledger, look at
evidence or research while the check ran, and a check that outlived the
unit's budget was lost with it. With the `check_job` capability, those
calls return a job instead of a result: the job runs against a frozen copy
of the source in a bounded runner that takes compute slots independently of
model admission, and the unit continues with work that does not depend on
the check. A result is imported only while it still matches the current
source digest, the configured commands and the environment, so a source
change cannot inherit a passing result. The unit ends with `outcome=wait`
and `wait_for_check`, and the job's completion resumes it.

The boundary is explicit. Writable execution stays synchronous, because it
writes the live tree its caller owns. A job's result is evidence for that
job's own inputs only: it is not a general cross-role discovery of
matching results, not a coalescing of equivalent in-flight checks, and a
preserved build artifact is not validation evidence. The capability is
opt-in per role, so a role without it keeps the synchronous behaviour.

## ADR-042 — Adapter startup is bounded by host-relative evidence

A single fixed timeout can refuse a managed adapter that is still loading its
SDK. Use a per-engine host record and derive the bound as twelve times the
largest recorded interval, with a 60 s floor and 150 s ceiling. If no probe
measurement completes, the floor remains the fallback. Completed refusals are
measurements too; a timeout with no answer is not.

The measurement endpoints are deliberately distinct. `doctor` and
`check-cli` run a one-shot probe and measure from process spawn through process
exit, then run the probe under the derived bound. Managed runtime startup
records the interval from child spawn to its first protocol response: Pi's
first `get_state` response, Claude's first `ready` event, and Codex's matching
`initialize` response. A refusal response is recorded before it is classified;
a child that never responds contributes no runtime sample. The shared record
keeps the largest interval for that engine, so probe and runtime timing are
not represented as the same measurement.

The runtime bound applies only to the initial startup/initialize exchange.
Codex app-server RPCs after `initialize` keep their existing 30 s response cap.
`pi-durable` runtime startup remains governed by its existing transport deadline
and does not consume this managed-handshake record. Its one-shot doctor/check
probe is still bounded as a probe, not as a runtime handshake.

A timed-out probe names the applied bound and is not reported as a missing
capability. An external signal kill is identified as such; if the product
selected a stop reason before terminating its child, that reason stays primary
and the signal is secondary detail. `doctor` and `check-cli` share the same
probe outcome classification and engine label.

## ADR-043 — A check job's container is protected by the runner's slot exclusion
A check job's container carries the project's label like any other container,
so a project-wide sweep enumerated it and removed it while the job was still
running: the command was killed under the job, the work was lost, and the
sweep reported a removal. A role's containers are protected because the
invocation holds that role's exclusion while it runs, and a check job had no
such exclusion: it belongs to the runner, not to a role.

The runner now holds the exclusion of the compute slot it occupies while a job
runs in it, and the sweep holds every compute slot exclusion. The sweep
therefore refuses while a check job runs, exactly as it refuses while a live
role runs, and a runner cannot start a job inside the sweep's own window
because the slot it would occupy is already held. The exclusion set is
bounded by the number of compute slots, so it never grows with the number of
jobs a project has run: a project that kept one file per job would make the
sweep and the storage maintenance open an unbounded number of locks and fail
outright.

The exclusion has one definition, in the module that admits invocations, and
the sweep takes the same paths the runner holds. The runner's own cleanup of
a job's container takes that same exclusion, which it already holds, so it
contends with nothing and leaves no exclusion of the job's own behind: the
file per job that every sweep would otherwise have to open stays absent.

A cleanup that fails keeps the compute slot reserved for operator recovery
instead of reporting a job as interrupted with its container still running: the
job is recorded as `cleanup_failed` and the slot's fault file is left for the
operator's recovery. A cleanup that is refused -- a live invocation holds the
slot's exclusion -- is read as a refusal rather than a removal, and the
operator's recovery reports the jobs it could not clean in `refused` and leaves
their reservation in place. The runner holds the exclusion of the slot it
occupies, so its own cleanup is never refused: it either removes the container
or fails, and a failure is the reservation.

## ADR-044 — A check wait holds a role only until its bound, and the operator sees it
A unit that sealed `outcome=wait, wait_for_check=<id>` deferred its role
until the job finished, and nothing else ended that deferral: the job's
completion, a new publication, an inbox generation, an operator wake. A
check that never ran, because the runner was not installed or the job sat
queued behind a paused project, held the role out of every later unit
indefinitely, and nothing named the hold. The unit's own wait bound was
recorded and ignored, so the operator read a `wake_at` that the product
never honoured.

The wait is now a bounded deferral. The unit's own wait bound — the
`wait_seconds` it sealed with, itself the configured default wait capped by
the configured maximum — ends the deferral, so a check that never finishes
delays a role instead of stopping it, and the recorded `wake_at` is the
truth. The bound is the unit's own, not a new knob: the wait a role already
declares is the wait a check wait may hold it for.

The deferral is named rather than silent. The deferral itself is recorded
once, in the check-jobs wait store, which is the store the engine consults
before it admits a role; the waiting unit's record carries `waiting_for_check`
with the job and the second the hold ends, the refusal that keeps the role
out names the job and that second, and `mizu status` lists every role the
queue holds under `check_waits` with its job, its status and its bound. The
last two are views of that one record, not records of their own. A wait
record that carries no bound holds nothing, so unreadable state never stops a
role, and a hold ends as soon as its job reaches a terminal status.
