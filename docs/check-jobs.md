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
The work unit ends and returns its model slot and workspace lock. The daemon
will not spend repeated model turns waiting on the same job. New publication,
inbox generation or explicit operator wake makes the prior waiting decision
stale; the role may reconsider independent work. A completed job itself admits
the role even when source code has not changed. Result delivery is acknowledged
only after a consuming work unit has durably completed, not on a possibly lost
tool response. Read-only service triggers watch the persisted queue.

`mizu check-jobs PROJECT` executes at most one pending job without a model. On
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
directory names and must be excluded from the source snapshot. One project cache
owner moves these directories into the frozen workspace and back after clean
completion; waiting for ownership consumes no compute slot. The live writer's
build tree is never accessed. Environment changes select a separate cache, and
interrupted jobs discard their private artifacts. Cache storage requires ordinary
operator disk maintenance and remains subject to the free-space admission reserve.
Artifacts never substitute for execution or passing evidence. Cross-role result
reuse is not inferred: neither arbitrary shell text nor a mutable mount path is
a sufficient immutable input identity.

Job ownership is a host file lock. A crashed runner's unfinished record is
interrupted and its job-specific containers are cleaned before releasing the
compute slot. It is never silently turned into success or replayed as though its
side effects were known. Resubmission in a new unit can retry; same-unit same-input
submission is idempotent. The requester can end without stopping the job; a job's
container label differs from the requesting role so normal role cleanup does not
remove it. Pause/drain prevents new jobs; stopping the runner cancels its current
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
