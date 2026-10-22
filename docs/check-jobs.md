# Asynchronous sandbox checks

Grant `check_job` to a role to opt in. `verify` then submits the configured
acceptance commands against a frozen copy and returns a job ID without waiting
for the commands. Ordinary `experiment` calls and read-only `exec` calls also submit jobs. Experiments with
a fetched `source_receipt` retain their existing synchronous behavior. Arbitrary
workspace-mutating `exec` remains synchronous; it is not safe to detach writes
to a workspace that the caller can continue modifying.

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
stale; the role may reconsider independent work. A completed job itself admits
the role even when source code has not changed. Result delivery is acknowledged
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

`limits.parallel_checks` (default 2, range 1–64) is a shared compute semaphore,
separate from model admission. `limits.check_pending` (default 32, range 1–1024)
bounds unfinished jobs per project. At most 32 acceptance commands belong to one
job. At 1024 retained records, an acknowledged job is evicted before accepting
another; if none can be evicted the submission is refused. Frozen source obeys
the existing snapshot size/file/exclusion bounds. Private source/build trees are
removed after terminal execution; only bounded evidence is retained. Cleanup
failures retain the tree until operator recovery confirms container removal. Operators may configure `sandbox.check_cache_paths = ["target"]` to preserve
incremental compiler artifacts for acceptance jobs. Entries are unique top-level
directory names and must be excluded from the source snapshot. Only verification jobs using configured cache directories take the project cache exclusion. Other checks can pass a cache-blocked verification without consuming that cache or bypassing the shared compute bound. One project cache
owner moves these directories into the frozen workspace and back after clean
completion; waiting for ownership consumes no compute slot. The live writer's
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
sandbox configuration and observed dependency inputs share one queued/running
execution or a completed passing result. Failures and interruptions are not
cached for new units. `check_job action=start fresh=true` requests a fresh run
when remeasurement is needed (for example time-sensitive or nondeterministic
checks). Retrying the same request within a unit remains idempotent.

The prompt exposes `shared_checks` candidates without expensive dependency
traversal; these are not yet accepted evidence. `check_job action=list` validates
and lists compatible candidates with command previews. `action=join,id=<id>`
creates a role-owned subscription after validating current inputs. A reviewer
can inspect a worker's verification without obtaining the `verify` execution
grant. Importing acceptance still requires that grant and a current source,
command and environment match. Command logs retain their original evidence ID.

Every subscription has its own ID, wait notification, cancellation and durable
acknowledgement. Cancelling one subscription does not cancel another role's
check; execution is cancelled when the last subscriber leaves. Consuming a
result acknowledges equivalent subscriptions only for the consuming role.
Evidence is retained while any subscription references it; the existing 1024
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
positive integers up to 1 TiB. Exceeding a bound disables sharing. Warm matching
uses cached content hashes but still checks filesystem identities. Neither build
artifacts nor command text alone can establish passed validation.

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
