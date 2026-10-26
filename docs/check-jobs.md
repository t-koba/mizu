# Asynchronous sandbox checks

Grant `check_job` to a role to opt in. `verify` then submits the configured
acceptance commands against a frozen copy and returns a job ID without waiting
for the commands. Ordinary `experiment` and `exec` calls run directly on the
existing synchronous path even with `check_job`; only an explicit `mode=async`
submits those calls as jobs. `verify` and explicit `check_job` starts are
unchanged. Arbitrary workspace-mutating `exec` remains synchronous; it is not
safe to detach writes to a workspace that the caller can continue modifying.

Ordinary `exec` and `experiment` calls accept an optional `mode` of `sync` or
`async`. Omitting `mode` runs the existing direct synchronous path even with
`check_job`, under the same role grants and workspace restrictions. `mode=sync`
selects the same direct path as omission, without bypassing workspace denial
or changing sandbox permissions. `mode=async` requires the `check_job` grant
and uses only the already-safe queued paths, so writable-workspace `exec` and
`source_receipt` experiments are refused instead of detached, and `verify`
behavior is unchanged (queued and frozen when `check_job` is granted); explicit
`check_job` starts (including named recorded batches) are unchanged. Mode alone
does not change queue identity or sharing.

## Delegated invocation ownership

A delegated child cannot use the `check_job` capability, even when its
configured role has that grant for ordinary top-level runs. The configured
grant remains unchanged; only the ephemeral child role view removes it. As a
result, child `exec`, `experiment` and `verify` routes run synchronously when
the corresponding grants are present, and no role-owned asynchronous job or
standalone check-job wake can outlive the caller-owned delegation. The caller
keeps whatever capabilities it already had, including its own waiting path when
it holds `check_job`.

## Opt-in disk-backed experiment scratch

Queued custom experiments accept `scratch_paths`, an array of unique names from
`sandbox.check_cache_paths`. Both `experiment` (`mode=async`) and
`check_job` (`action=start` with `script`, `commands` or `batch_name`) expose
it under the existing grants. Names are configured
top-level artifact directories, not host paths; duplicate, unconfigured and
malformed names are refused. The tool schema
bounds each name to 255 ASCII characters and the array to 32 entries. Omission or
`[]` keeps ordinary tmpfs-only execution and the existing job identity. A
nonempty selection is canonicalized into the run-local job identity and appears
in job summaries and command evidence. For `experiment`, an empty selection is
always equivalent to omission; a nonempty selection requires `mode=async`, the
`check_job` grant and no source receipt. Omitted or synchronous modes are
refused so a direct call never silently ignores scratch. For `check_job`, supply
the field only
with `action=start`; an empty selection there is equivalent to omission, while a
nonempty selection requires a custom experiment script, `commands` or a named
`batch_name`. `exec` never accepts scratch paths. This grants no new execution
authority.

At execution time the runner takes the project's existing `check-cache.lock`
**before** a compute slot, just as for verification. A blocked scratch job
cannot hold a compute slot or stop unrelated no-cache work. Selected paths must
be excluded from snapshots. The runner moves only those trees from
`check-cache/<execution>` into the job-private frozen `input/<name>`, creating
empty directories when no prior artifact exists. Persistent cache directories
are never container mounts. `/workspace` remains read-only and `/work` remains
the configured size-bounded tmpfs; only selected `input/<name>` directories are
bound read-write at `/work/<name>`. For example, with a configured, excluded
`build` path, an experiment can use `scratch_paths=["build"]` and write to
`build/object` from its ordinary `/work` working directory. Other temporary
files still consume tmpfs. Disk-backed scratch uses the existing harness-owned
cache storage/reserve and cleanup policy, not the tmpfs size allowance.

After commands exit and sandbox cleanup succeeds, selected trees return to the
same persistent cache owner (even when an exited command returns nonzero).
Cancellation, interruption, timeout or launcher failure discards staged scratch
rather than persisting partial work; container cleanup failure retains the
private input for operator recovery under the existing slot quarantine. A
cancelled job may therefore discard artifacts it had staged from a prior run.
There is no automatic retry. Verification continues to move configured cache
trees under the same exclusion.

Scratch contents are mutable and are selected at execution time, not pinned at
submission. Consequently scratch-enabled jobs never share command results:
no automatic cross-job reuse, discovery or explicit join, even with
`check_reuse=true` or `fresh=false`. Repeating the identical request within the
same run still names one demand. Frozen source, command and environment checks
remain in force, but a completed scratch experiment is **custom evidence only**:
it cannot import `context.verification` or produce an acceptance receipt.

## Ordered custom-command batches

`check_job` (`action=start`) accepts one ad-hoc custom experiment as either
`script` (a single command) or `commands` (an ordered list of 1-32 commands); a
named configured batch is a third selector described below. Each batch entry is
nonblank and must fit the existing 64 KiB UTF-8 command bound. The selectors
are exclusive; omitting all custom selectors submits the configured acceptance
commands. Each custom selection resolves to an ordered command list and reuses
the existing one-job frozen-source lifecycle: one frozen input, sequential
execution in listed order, per-command recorded evidence, and the existing
purpose/`fresh`/identity/sharing/owner/wait/compute-slot/cancellation/cleanup
behavior. Order is part of the identity: changing, adding, removing or
reordering entries selects a different job.

Each command runs in its own separate sandbox context. Ordinary `/work` is a
fresh size-bounded tmpfs per command, not a persistent shell session: exports,
working-directory changes and other shell state do not carry from one command
to the next. Sequence work as explicit ordered commands rather than relying on
carry-over state. Selected disk-backed scratch (`scratch_paths`) follows the
existing job lifecycle for batches exactly as for a single custom script
(staging before execution, commit after clean completion, discard on
cancellation/interruption/timeout/launcher failure, quarantine on cleanup
failure).

A custom batch is always kind `experiment`, never acceptance, even when its
entries equal the configured verification commands. It cannot mint an
acceptance receipt or import `context.verification`. Multiple custom selectors,
empty or oversized lists, non-string/blank/oversized entries, and custom
`script`/`commands`/`batch_name`/`scratch_paths` on non-`start` actions are
refused before execution.

## Named recorded batches

Operator-owned `project.toml` `[recorded_batches]` definitions are selectable
through `check_job` (`action=start`) as `batch_name` (text, at most 63
characters), an alternative to ad-hoc `script` or `commands`. Use
`action=list` to inspect the available names; it does not expose command text.
The three custom forms are mutually exclusive; omitting all three submits the
configured acceptance commands. `batch_name` on any non-`start` action, an
unknown name, a malformed name, or any combination of two custom forms is refused
before any job submission.

The name resolves at submission from the trusted loaded project settings to
its exact ordered command list, then runs through the existing
`check_jobs.submit(commands=...)` custom-experiment path with no new runner:
the complete representable frozen workspace snapshot (configured exclusions
apply) plus the existing execution/environment binding, one frozen input,
sequential execution in listed order, per-command recorded evidence, and the
existing `fresh`/purpose/identity/sharing/ownership/wait/compute-slot/
cancellation/cleanup behavior. Job identity keys on the actual resolved
commands plus the current full frozen source/environment, never on the
definition name alone, so renaming a batch without changing its commands
selects the same job, while editing, adding, removing, or reordering entries
selects a different one. `check_reuse` remains the global cross-job sharing
gate; selecting a name does not enable sharing by itself. `scratch_paths` works
with a named batch exactly as with an ad-hoc batch. A named batch is custom
evidence only even when its resolved commands equal the configured acceptance
commands, and a submitted
job keeps its resolved commands while later configuration edits affect new
submissions only.

## Results and waiting

The role can continue independent work, inspect `check_job` with `action=list`,
request cancellation with `action=cancel,id=<full digest>`,
and retrieve a completion with `action=result,id=<full digest>`. Results carry
bounded output previews and paths to complete recorded command evidence. Use
`action=logs,id=<job>,evidence_id=<command>,offset=0,limit=8192` for full
recorded output; continue with `content_id` and `next_offset`. Pages are capped
at 8192 characters and changed records are refused. Logs remain untrusted data. A
passing acceptance result is imported only when current source, acceptance
commands and configured execution environment match. A queued, interrupted,
refused, failed or source-mismatched result cannot authorize completion.

When only that job remains, finish with `outcome=wait, wait_for_check=<id>`.
The work unit ends and returns its model slot and workspace lock. The wait
holds the role for as long as the wait the unit sealed with: the configured
`limits.default_wait_seconds`, or the `wait_seconds` the unit chose, capped
by `limits.maximum_wait_seconds`. The hold ends there even when the job
never finishes, so a check that never runs delays a role instead of stopping
it, and the recorded `wake_at` is the truth. Inside one bound the daemon
does not spend repeated model turns waiting on the same job. New publication,
inbox generation or explicit operator wake makes the prior waiting decision
stale; the role may reconsider independent work. A completed job admits
the role only when it is the exact job named by that role’s explicit wait,
even when source code has not changed. Result delivery is acknowledged
only after a consuming work unit has durably completed, not on a possibly lost
tool response. Read-only service triggers watch the persisted queue.

The hold is named, never silent. The waiting unit's record carries
`waiting_for_check` with the job and the second the hold ends, and
`mizu status` lists every role the queue holds under `check_waits` with the
same job, its status and its bound. A wait record that carries no bound
holds nothing, so unreadable state never stops a role.

`mizu check-jobs PROJECT` drains up to `limits.check_pending` terminal jobs per invocation without a model, releasing admission between jobs. It returns immediately on idle, compute contention, cancellation, pause or cleanup failure; it never sleeps between queued jobs. Each result remains in its own durable record. The CLI response adds `processed`, the number of terminal jobs handled. On
Linux, service generation creates a dedicated checks service, path notification
and a 30-second recovery timer when a project enables the capability. Install
and enable those units before enabling model use of the capability. Other
platforms can invoke the same bounded command from an operator-managed runner;
they do not yet have generated check-runner services.

`mizu doctor` reports a `check-job runner` row for this runner. With no
initialized project granting `check_job` the row is `not_run`. On Linux the
row passes when every granted project's check service, notification path and
recovery timer are installed in the standard service directory, and fails
naming the exact missing unit files. On macOS/Windows the row is `not_run`:
the runner is operator-managed there and generated definitions are absent by
design, so there is nothing for the doctor to verify.

`limits.parallel_checks` (default 2, range 1–64) is a shared compute semaphore,
separate from model admission. `limits.check_pending` (default 32, range 1–1024)
bounds unfinished jobs per project. At most 32 acceptance commands belong to one
job. At 1024 retained records, an acknowledged job is evicted before accepting
another; if none can be evicted the submission is refused. Frozen source obeys
the existing snapshot size/file/exclusion bounds. Private source/build trees are
removed after terminal execution; only bounded evidence is retained. Cleanup
failures retain the tree until operator recovery confirms container removal. Operators may configure `sandbox.check_cache_paths = ["target"]` to preserve
incremental compiler artifacts. Entries are unique top-level directory names
and must be excluded from the source snapshot. Verification jobs use all
configured cache directories; a queued custom experiment may explicitly select
a subset through `scratch_paths`. Every job using a cache takes the same project
cache exclusion; unrelated checks can pass a cache-blocked job without consuming
that cache or bypassing the shared compute bound. One project cache owner moves
selected directories into the frozen workspace and back after clean completion;
waiting for ownership consumes no compute slot. The live writer's
build tree is never accessed. Environment changes select a separate cache, and
interrupted jobs discard their private artifacts. Cache storage requires ordinary
operator disk maintenance and remains subject to the free-space admission reserve.
Artifacts never substitute for execution or passing evidence. Cross-role result
reuse requires the explicit sharing configuration and input checks described
below; shell text or a mutable mount path alone is insufficient.

A request may name its own demand with an optional `purpose` (1-64
printable characters, no path separators, no whitespace-only value). A later
request with the same purpose from the same role replaces that role's earlier
unfinished job for that purpose: the superseded job leaves the pending queue
and the runner's time, its record names the job that replaced it, and its
frozen input and recorded output are retained. A different purpose, or no
purpose, keeps the earlier job. A purpose is the caller's own statement of
intent, so it never retires another role's job or subscription, and it is
never inferred from age, from a changed source digest or from "newest wins".

Job ownership is a host file lock. A crashed runner's unfinished record is
interrupted and its job-specific containers are cleaned before releasing the
compute slot. It is never silently turned into success or replayed as though its
side effects were known. Resubmission in a new unit can retry; same-unit same-input
submission is idempotent. The requester can end without stopping the job; a job's
container label differs from the requesting role so normal role cleanup does not
remove it. The runner holds the exclusion of the compute slot it occupies while
a job runs in it, so a project-wide sweep refuses instead of removing a running
job's container, and a runner cannot start a job inside a sweep's own window.
That set is bounded by the number of compute slots, so it never grows with the
number of jobs: the runner's own cleanup takes the exclusion of the slot it
occupies, so no exclusion file is left behind for the job. Pause/drain prevents new jobs; stopping the runner cancels its current
bounded command. Changed config or revoked grants refuses queued execution.

This opt-in mechanism changes no role policy, priorities or scheduling decisions.
The tests use fake sandbox commands for lifecycle invariants; they are not proof
of actual rootless container isolation. Runtime isolation must be smoke-tested
separately before production rollout.

Delegated checks are delivered to the parent role and returned in the delegation
result, so an ephemeral implementation child does not become an unwakeable owner.
The producer and receiving parent must both retain the explicit capability.

If container removal fails, the compute slot is quarantined rather than silently
reused while an orphan may still consume resources. `mizu check-jobs PROJECT
--recover` clears that reservation only after successful cleanup; the old job
remains interrupted, never passed.

## Sharing execution and recorded results

`sandbox.check_reuse = true` explicitly enables project-local sharing. It is
false by default. Matching source bytes, exact command list and execution kind,
sandbox configuration, observed dependency inputs and configured verification
cache inputs share one queued/running
execution or a completed passing result. Failures and interruptions are not
cached for new units. `check_job action=start fresh=true` requests a fresh run
when remeasurement is needed (for example time-sensitive or nondeterministic
checks); that request runs its own execution and never joins an existing one.
Its result is still shared under the same policy, so a later request with the
same inputs reuses that measurement instead of repeating it. Retrying the same
request within a unit remains idempotent.

The prompt exposes `shared_checks` candidates without expensive dependency
traversal; these are not yet accepted evidence. `check_job action=list` validates
and lists compatible candidates with command previews. `action=join,id=<id>`
creates a role-owned subscription after validating current inputs. A reviewer
can inspect a worker's verification without obtaining the `verify` execution
grant. Importing acceptance still requires that grant and a current source,
command and environment match. Command logs retain their original evidence ID.

Every subscription has its own ID, wait notification, cancellation and durable
acknowledgement. Cancelling one subscription does not cancel another role's
check; execution is cancelled when the last subscriber leaves. Reusing a result
that is already completed and passing returns its terminal summary synchronously
and records that new subscription as acknowledged, so a cache hit does not
manufacture a later wake. The shared result remains readable through
`action=result`/`action=logs`; queued or running subscriptions remain unread
until their result is consumed. Consuming a result acknowledges equivalent
subscriptions only for the consuming role. Evidence is retained while any
subscription references it; the existing 1024
record bound includes subscriptions and fails closed when nothing can be
reaped. Subscriptions consume no compute slot or private source/build tree.

Reuse requires a digest-pinned image and disabled container networking. Read-only
mounts are fingerprinted by content and filesystem identity at submission,
before execution, after execution and before acceptance/reuse. File hashes are
cached against device, inode, mode, size, mtime and ctime on POSIX; other systems
rehash file bytes. Directory identities and internal relative symlink targets
are included. Changed dependencies invalidate the result even if source is
unchanged. This assumes the operator-controlled host and its filesystem metadata
are trusted, as do the existing sandbox and evidence stores. Unsupported special
files, external symlinks, inaccessible or concurrently changing inputs disable
sharing. Custom isolated checks can still execute, but acceptance requested
with sharing enabled is refused when dependency inputs cannot be bound; it never
falls back to accepting unbound evidence. Disable check_reuse to use the original
non-sharing execution semantics for such an environment. Network-dependent or
unpinned-image inputs likewise cannot supply reusable acceptance. Fresh runs
bypass earlier results but still validate their dependency inputs.

Dependency traversal is bounded by `sandbox.check_input_files` (250000 entries)
and `sandbox.check_input_bytes` (8 GiB of regular file contents), configurable
positive integers up to 1 TiB. Exceeding a bound disables sharing. A job with a
bound dependency environment retains the exact traversal bounds in its
`environment_bounds` record (`files` and `bytes`, each an integer in the same
range). Execution and acceptance revalidation use these original bounds even
if reuse is later disabled or the configured traversal limits change. Missing
bounds on older records fall back to the current configured limits; malformed
or out-of-range retained bounds refuse validation without scanning. These
bounds are validation metadata, not part of job identity or the shared key;
sharing candidates still use current sharing settings. Verification-cache
traversal continues to use its current configured limits independently.
Warm matching uses cached content hashes but still checks filesystem identities.
Neither build artifacts nor command text alone can establish passed validation.

Configured verification caches have a separate `cache_input` fingerprint, not
part of the external-dependency `environment`. It binds every configured name,
absence or directory presence, nested names, file bytes, permissions and mtimes
under `check-cache/<execution>`. Inode/ctime observations detect concurrent edits
but are not part of the content key, so staging an unchanged tree out and back
does not invalidate it. Traversal independently uses the same `check_input_files`
and `check_input_bytes` bounds; directories and absent configured names count as
entries. Symlinks (including internal ones), reparse points, hardlinked files,
special files, unreadable/unstable trees and exceeded bounds are **unbound**, not
empty. POSIX traversal pins path components without following links; the portable
fallback checks components and identities before/after access.

Cache fingerprinting and the reuse/enqueue decision hold `check-cache.lock` in
that order before queue exclusion; cache users can wait for the current cache
owner, but hashing never holds `check-jobs.lock`. Dispatch revalidates the queued
fingerprint under the same cache lock before moving any cache tree into frozen
source. A changed or unbound verification cache refuses the queued execution,
even with sharing disabled. Unbound requests never return an earlier same-unit
result. No retry is automatic; after correcting inputs, submit again. Custom
checks without configured verification caches retain their independent behavior.

Checks may legitimately mutate caches and still produce a passing result. Their
proof binds the **pre-execution** cache input, not the staged-back output. Result
import and retained-acceptance lookup compare the current fingerprint with that
input while holding `check-cache.lock` through import or final index/receipt
selection: cache mutation therefore requires a new check before current acceptance
can be imported. Old receipts lacking `cache_input` remain auditable, but cannot
match when verification caches are configured. No-cache scopes retain their
existing form. Experiments, including cache-backed custom scratch, never mint
configured verification acceptance.

Development and CI can use `python scripts/check.py --fail-fast` to report a
Python contract failure immediately instead of spending minutes running the
rest of a failing suite. The option is off by default, preserves sanitized
failure evidence, and never makes a failed or partial run reusable as a passing
gate. With no failure, the entire suite still runs.


When a unit finishes while waiting for a check, already-read terminal results
are acknowledged even if another check remains outstanding. The next prompt
carries `check_continuation`: the prior role's summary/state, source identity,
`same_code`, and the awaited check's current status. This is bounded prior-role
report data (4000 characters per text field), not validation evidence. A changed
source is explicit and never inherits acceptance. A normal finish clears this
continuation; bounded or failed units do not acknowledge unfinished delivery.
Acknowledged historical results remain available through `check_job list` but
are omitted from the default prompt's outstanding-check projection.

`finish.release_checks` accepts at most 32 owned check IDs. All ownership and
wait conflicts are validated before sealing. A successful, unbounded finish
releases those subscriptions using normal cancellation: other subscribers keep
the execution alive, and only an execution with no remaining consumer is
cancelled. Those explicitly released subscriptions are acknowledged in the same
finish so their cancellation does not wake another model unit. It never selects
obsolete work by source age or guesses its relevance.
For a timed external wait, writable roles can use `finish` with `outcome=wait`
and `wait_seconds`; the existing persisted wake deadline releases the unit and
workspace rather than holding a command open.

For `check_job` action `logs`, omitting `evidence_id` returns an index of up to 32 available recorded command IDs and ready-to-use `logs` requests for that same owned subscription. It lists only regular, nonsymlink UUID command records in the physical job's evidence directory, including when the subscription shares execution. No completion, acknowledgement or passing acceptance is inferred. Supplying an ID retains the existing paged, content-pinned evidence read and ownership checks.

Queued/running job responses include a `wait` continuation recipe: the `finish`
operation and its `outcome=wait, wait_for_check=<owned id>` arguments, plus the
summary/state fields the caller must supply. It neither seals a unit nor claims
success. Continue independent work when available; invoking that finish returns
the model slot and workspace lock and resumes on completion or new input. The
recipe disappears on terminal results. Normal ownership and finish validation
still apply.

## Retained acceptance evidence — IMPLEMENTED SLICE

Passing input-bound acceptance now has a durable owner separate from the job
cache and the live workspace: `evidence/acceptance/`. Both synchronous `verify`
and the **original** detached verification producer retain normalized receipts.
Subscribers and result collection do not mint receipts. The detached producer
publishes its terminal state only after execution and cache cleanup; cancellation
at that boundary prevents retention. No execution, experiment, failed,
interrupted, cancelled, cleanup-failed, source-changing, dependency-changing or
unrepresentable check can mint a passing receipt.

The existing `sandbox.check_reuse` input-binding mechanism supplies the scope:
full configured acceptance commands (not command previews), execution-key digest,
observed dependency fingerprint, configured verification cache-input fingerprint
(when present), and pinned image. An unbound environment (`None`),
network-enabled execution or mutable image is not reusable evidence. No new
policy switch is introduced. Scope is bounded to 512 KiB of canonical JSON;
scopes that cannot fit are nonreusable. Each normalized receipt is at most 1 MiB,
contains only source/scope, producer identity and successful command outcomes,
and retains **no stdout, stderr or command-log bodies**. Existing producer
records and ordinary command logs keep their own existing bounds and lifecycle.
Producer records above the configured `file_bytes` read bound cannot be
recovered into receipts. Detached finalization checks the exact serialized
result against that bound before marking it for retention. Recovery of an
already marked oversized result removes the pending scope and records
`acceptance_retention_refusal: "result_file_bytes"` in `job.json`, as does
finalization of an oversized result. This bounded, non-authoritative disposition
mints no proof, leaves the result bytes and execution verdict intact, and
permits normal acknowledged cache eviction without repeated retention retries.
It cannot later promote raw result bytes, even if the read bound increases.
Malformed or integrity-failing producer content cannot mint proof; unsafe
paths and shared/durable store failures still refuse the operation.

### Invalid producer diagnostics and operator repair

Runner recovery isolates invalid individual job-cache content only after a safe,
bounded regular-file read: malformed JSON, invalid job schema/scope, and failed
producer/result integrity or normalization. Healthy finalized producers still
recover, and unrelated queued jobs still run in the existing order. No switch
or automatic retry is added. The invalid producer is excluded **before** scheduler
sorting/reads, not treated as successful, acknowledged, quarantined or evictable;
its bytes and any pending-proof eviction pin are left unchanged. Existing durable
proof remains usable independently of corrupt cache bytes. The existing size-only
retention refusal above is separate and unchanged.

Read-only discovery uses one `check-jobs.lock` snapshot for job listing, shared
candidates, pending completions and continuation summaries. Full and delta
prompts reuse that snapshot across all three check views. Discovery validates
fields consumed by sorting, filtering, summaries and the **one-hop** subscription
overlay, plus marked acceptance producer content using the same pure inspector
as retention. It never invokes mutating recovery/retention, creates proof,
acknowledges a result, removes a pending scope, or rewrites producer bytes.
An invalid/missing original excludes its attached subscription from delivery;
detached subscriptions keep their existing cancelled disposition.

An ordinary `check-jobs/<non-producer-name>/job.json` is classified only after
safe reading its regular, non-hardlinked leaf (including path checks before a
size-only refusal). It is left untouched and does not veto valid jobs. The
public diagnostic contains `entry_digest`, **not** a job ID or literal path:
SHA-256 of `mizu.check-job-entry.v1` + NUL + the filesystem-encoded entry name.
This domain-separated digest is a diagnostic locator, never a usable job ID.
`InvalidProducer` still accepts only valid 64-hex producer identities. Symlinks,
hardlinks or unsafe shared paths are not ordinary non-producer entries.

Every `run_one`/`run_pending` return with detected corruption, and check-job tool
`list`, includes
`invalid_producers: {entries: [{job_id, code} | {entry_digest, code}], total,
omitted, coverage_complete: false}`. One deterministic view is limited to 32
entries; counts cover all distinct invalid producers and non-producer entries,
and batch repeats are deduplicated. All exclusions, including omitted entries,
remain internal. Codes are fixed, not exception text: `job_json`, `job_schema`,
`job_scope`, `job_integrity`, `result_json`, `result_integrity`, plus discovery's
`job_file_bytes`, `result_file_bytes`, `job_subscription` and
`non_producer_entry`. The recovery-only size disposition above is unchanged.
Diagnostics can accompany `idle`, `busy`, `deferred` or a completed healthy job;
none of those statuses means the suspect evidence was resolved.

For roles with `check_job`, both prompt modes include `check_discovery` with
`coverage_complete`, `summaries_omitted`, `continuation_omitted`, and the same bounded
`invalid_producers` envelope when exclusions exist. `coverage_complete` means
no suspect producer/entry was excluded, not that every valid summary fits.
Each owned/shared list remains capped at 32. Their combined JSON projection,
including continuation and diagnostics, is capped at 64 KiB (UTF-8, including
JSON escaping); an oversized continuation is withheld with `continuation_omitted`
set true. `summaries_omitted` counts additional byte-budget omissions
from those count-capped lists. Command previews remain 128 bytes each (first
four), summary string fields are at most 512 bytes, and continuation report
text is at most 4,000 bytes per field with 128-byte reference previews.
Roles without the grant neither scan this cache nor receive its diagnostics.
Existing role, source, execution, environment and workspace visibility filters
remain in force; prompt shared candidates still require dependency validation
on tool use. Pending completion discovery returns only genuine eligible jobs:
diagnostics alone never become a completion wake. A later discovery rechecks
files; it does not poll, acknowledge, quarantine or repair them.

This boundary does not make mutation scans tolerant: submission's reuse scan,
queue-capacity accounting (`_pending_jobs`), eviction (`_make_room`), and
same-purpose replacement (`_superseded`/interest checks) retain their existing
direct-read behavior and can still fail on corrupt cache content. These paths,
and direct-ID result, cancellation and acknowledgement operations, are not
covered by the read-discovery isolation contract.

Unsafe shared directories, links/hardlinks, unstable reads, systemic I/O errors,
durable receipt/index corruption and write failures remain fatal, not isolated.
Durable evidence audit is separate and strict. Backup retries healthy producer
retention first, then visibly refuses if any invalid producer remains: job caches
are excluded from archives, so a successful backup must not silently omit the
only suspect bytes. Historical run recovery during backup also remains strict.

Repair is an explicit operator action, outside autonomous recovery: pause the
project and obtain the maintenance exclusions, preserve the suspect producer
files and diagnostics outside the disposable cache, and investigate storage and
source evidence. Restore only from an independently trusted intact copy; do not
invent passing results, receipts, acknowledgements or remove a scope to unpin a
suspect producer. If evidence cannot be restored, retain it for investigation
rather than silently resetting it. Re-run recovery/backup after repair and check
that the diagnostic is gone and strict durable audit succeeds. This mechanism
provides no automatic delete, rewrite, quarantine or repair command.

`objects/<receipt-id>.json` is immutable, keyed by SHA-256 of the canonical
schema-1 receipt. `index/<code-digest>/<scope-digest>.json` is an immutable
first-writer selection of an exact receipt ID. The object is synced before the
index is published; retries are idempotent, and a later success does not replace
that selection. Evidence consumers carry the selected immutable ID and can
resolve it exactly; they never substitute a newer scope lookup for a recorded
ID. Reads validate schema, digests, bindings and regular non-hardlinked paths;
unsafe directories, malformed records and dangling indexes fail closed.

`Project.status` and both full and delta prompt `published_snapshot` headers
carry **separate** fields:

- `verification`: unchanged raw verification embedded at snapshot publication;
- `effective_verification`: `null`, or `{passed: true, code_digest,
  source: "retained_acceptance", receipt_id}` for a matching representable tree
  and the current exact configured scope.

The latter is a read projection, not a manifest rewrite or a current-workspace
import. Selector work inputs likewise keep their publication-time `verified`
verdict separate from `retained_verification` (the exact projected reference).
Immutable report artifact evidence continues to describe its publication-time
snapshot verification; it is not retroactively rewritten by late results.
An A check completing after the workspace moves to B can therefore become
visible for retained A without republishing A, collecting the result or setting
B's current verification. Current-source result import still applies its source,
execution, environment and grant checks independently.

Synchronous verification now also fails when otherwise successful, source-stable
commands change their bound mounted dependencies or configured execution inputs
(including acceptance commands) during execution. This intentionally tightens
the prior source-only verdict to match detached input-bound authority:
`dependencies_unchanged` must be true for synchronous success too. Unbound
execution remains nonreusable; retained evidence does not relax any current-source
import, capability or completion guard.

Runner recovery retries validated finalized originals in the bounded job
cache; backup maintenance also retries synchronous run records and re-indexes
durable normalized objects after an object/index interruption. Old
results without a bound scope marker are never upgraded. Check-job locks are
released **before** acceptance retention takes its lock; finalized producers
awaiting receipt acknowledgement cannot be evicted. After receipt retention,
eviction is harmless. The existing backed-up `evidence` root includes all
receipts/indexes; backup holds the acceptance lock while archiving, restore
validates the whole store before exposing a project, and storage audit counts
`acceptance_receipts` and refuses corrupt/unsafe/dangling proof. Receipts are
retained audit evidence, not reclaimable job cache.

**Item completion:** retained acceptance alone is not delivery. WorkItems now
requires an earlier explicit delivery revision bound to an exact retained
published snapshot and accepted scope (ADR-050). Ordinary done may use embedded
proof or this late projection; it pins the exact immutable receipt ID and
acceptance-scope digest for replay independent of current configuration/index.
Forward dropped-to-done correction requires that prior delivery plus retained
acceptance, not embedded proof alone. No unit-finish verification or external
VCS publication witness rule changes. Tests in `test_acceptance.py` use bounded
fake runners; they are not a rootless-runtime or deployment smoke test.
