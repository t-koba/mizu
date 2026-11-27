# Migration to the kernel

This document records the plan for moving Mizu from a harness with a built-in
development workflow to the kernel specified in [kernel.md](kernel.md). Stage 2
is complete: the proposal, decision, question, answer, notice and receipt
machinery was deleted from `src/mizu`, and those flows are declared queues and
policy text. A stage is complete only when its dedicated code is deleted.

Stage 5 is **not complete**. Three slices have been delivered: the
`[vcs] default_branch` policy removed the hardcoded `"main"` literals from
kernel behaviour, the `ci_head_state` prompt facts moved to an observation
driver, and the opt-in `ci_summary` aggregates moved to their own driver. Those
deliveries are verified on their own scope, but they do not delete the
dedicated VCS/CI code. `ci_*` modules, the CI wait kinds in
`work_item_waits.py`, the publication-state projection through reviews in
`unit_prompts.py`, `GO <branch>` title parsing and the pull-request model in
`vcs` remain authoritative until the stage-5 deletion slice lands. Green CI on
a narrow slice establishes that slice only, never the whole stage. The
head-state CPU receipt is a generic wake-path control; stage 5 additionally
needs a matched changed-case measurement with a configured CI branch, a role
that receives the fixed key, witnessed rows on both paths and driver-child CPU
accounting (see the stage-5 row below and the retained evidence).

## Why

An inventory of `src/mizu` at 1322c64f (127 modules, 57,460 lines) found that
about 47% of the code exists to implement one development workflow:
proposals and decisions, work items and their waits, completion acceptance,
publication and review, CI handling, delegation and consultation, and
self-hosting. Nine shared modules (runtime, config, protocol, unit_finish,
unit_prompts, project, cli, storage, snapshot; about 13.5k lines) branch on
the same concepts. Every new need of a deployment therefore becomes a product
change, and the product drifts toward one deployment's process.

## Userland

Today's behaviour does not disappear: it becomes configuration and policy
text. The project ships it as `examples/development/`, an example userland
(queue declarations, roles, wakes, outcomes and policy files) that reproduces
the current workflow on the kernel. A deployment uses it by including it in its
own configuration and changes it freely. The kernel never reads it specially.

What a message means, and what a role does on reading it, is written in the
role policy files. For example, "a proposal is decided by the writer, who
writes a decision message referring to it" is a sentence in the writer's
policy and a `decisions` queue in the configuration, not code.

Existing stored records are converted by operator-run procedures that append
equivalent messages to the declared queues; they are not part of the kernel or
of a running role. The example userland declares the `proposals`, `questions`
(threads: questions and the answers that refer to them) and `notices` queues
with their schemas, roles and wakes. There is no
authority marker, dual-write path, or adapter left in the runtime: decisions are
plain-English judgements made by roles under policy, and delivery is a kernel
fact recorded when content is handed to a reader. Old record files appear only as stored data for those conversions
and as historical manifests; see [operations](operations.md) for the procedures.

The first stage-3 slice projects a bounded current work-item inventory through
the operator-run example procedure `examples/development/convert_work.py`. It
reads the inventory with the public CLI and appends declared `item` messages to
the `work` queue with caller ids chosen for idempotent retries; it is userland
and changes no runtime authority. The dedicated work-item store stays
authoritative until the stage's cutover slice: there is no runtime dual-write,
and the example only prepares messages for policies that read the queue.
The public inventory exposes bounded previews, so these messages are a
projection, not a lossless conversion; a later slice must carry full records
before any cutover.

## Stages

Each stage: implement the kernel part, express the affected workflow in the
example userland, switch the code paths to queues, delete the dedicated code,
keep CI green. There is no shadow or dual-write period in product code: a stage
switches authority at once together with the one-time conversion, then deletes
the dedicated code. Measure host CPU of an idle deployment and of one change
before and after the stages that touch the daemon (4 onward). The repeatable local harness is
`python3 scripts/daemon-cpu.py --report private-validation/daemon-cpu.json`; it
runs real daemon subprocesses (Unix-only), waits for an observed steady-state CPU
window, and records measurement validity in the receipt. It also records the
deployment shape and the exact source the daemons loaded (git revision, a content
digest of the daemon source, and a digest of the harness script that computed the
window), so a before/after pair is bound to the code it exercised and to one
measurement method, and only the relative comparison on one machine is
meaningful; a non-zero exit means that window was not steady state. The revision
is read from git only when the measured tree is its own worktree root; otherwise
the explicit label is used, so a git-less exported tree cannot inherit an
enclosing checkout's HEAD. To measure an earlier revision with the same harness,
set `MIZU_CPU_SOURCE_ROOT` to that tree and `MIZU_CPU_SOURCE_REVISION` to its git
label. The stage-4 before/after receipts are retained path-free in
`docs/evidence/stage4-cpu-evidence.json`. Each receipt names the git commit it
measured; a reader with a full clone recomputes that source-tree digest with the
harness recipe (`scripts/verify-stage4-cpu-evidence.py --repo <checkout>`), which is
non-circular because the commit tree is outside the record. Tracked-only mode
verifies record integrity, harness digest, shape and validity and explicitly skips
the tree checks rather than asserting them from fields in the same file; the record's
witness block carries the captured outputs and reader recipes, without worker-private IDs.

The resident daemon no longer stats its fixed watched set on a one-second cadence: it blocks on
the platform change notification (inotify, kqueue or FindFirstChangeNotification) for the same
paths and on the exact next deadline, and does nothing in between. The project root is watched
only for entry events, with event names filtered to the fingerprint set where the platform
reports them, so writes to unwatched root children cannot wake the daemon. A stop bridge is
owned once per daemon lifetime and released when the stop event is collected. This wake
coverage is established for local filesystems: a successful registration on a network mount
does not show that changes originating on another host are delivered, and no filesystem-name
blacklist can prove support. Every arming therefore emits a `native_wake_coverage` fact
naming the watched paths whose watch was accepted: local-origin changes are covered, while
remote-origin delivery is reported as unverified rather than assumed. A path whose
registration fails is reported separately as a `native_wake_unwatchable` fact; neither
starts a polling fallback. The matched
before/after CPU receipts for this change are retained path-free in
`docs/evidence/native-wake-cpu-evidence.json`; verify them with
`scripts/verify-stage4-cpu-evidence.py --record docs/evidence/native-wake-cpu-evidence.json --repo <checkout>`.

The stage-5 cutover that moves fixed `ci_head_state` prompt facts to an
operator observation driver was measured on the same settled shape and
harness digest before and after; both receipts report `proc-per-pid`
`steady_state_observed=true` idle medians of 0.0 s and one-change medians of
0.08 s and 0.07 s. They are retained path-free in
`docs/evidence/ci-head-state-cpu-evidence.json`; verify them with
`scripts/verify-stage4-cpu-evidence.py --record docs/evidence/ci-head-state-cpu-evidence.json --repo <checkout>`.

The same stage-5 slice also moves the opt-in prompt `ci_summary` aggregates to
a declared observation driver: the host reads one coherent poll cache and sends
only bounded failure subject/revision pairs, validated check summaries and an incompleteness
flag, the driver owns the aggregation policy, and the kernel validates the
returned facts before they reach the prompt. Wake and admission logic keeps its
own kernel-side projection; an undeclared, refusing or invalid driver leaves the
aggregate prompt key empty without inventing facts. Like the head-state driver, this
runs only while building a unit prompt and appends to a queue of readable prompt
facts that does not wake the role (kernel-side `ci_due` remains the CI
admission) and retains only the newest observation owned by the driver, so it
adds no daemon periodic work; the daemon code
and wake paths are untouched and no new CPU receipt is required for this slice.

The three stage-5 behaviour-code branch literals are removed: the CI live-ref
fallback, the empty-workspace poll target and the sync receipt's `upstream` SHA
come from `[vcs] default_branch` (empty in the kernel, `"main"` in the shipped
example config). With no default configured the kernel adds no branch, polls no
branch and reports a null `upstream`; none of these three paths hardcodes a
forge branch name. The separate legacy schema-1 lineage validator still pins
`refs/heads/main` as a declared wire-format constant; schema 2 binds to the
configured `[vcs.lineage_target]`, and that legacy format is outside this
cutover.

| Stage | Kernel work | Moves to userland | Deleted when done |
|---|---|---|---|
| 1. Queues | `mizu.kernel`: queue declarations, authenticated append, per-reader delivery facts, delayed delivery, reference lookup, retention, wake files; `queue_*` and `message_*` tools; `[queues]` configuration | — | The object store with states, transitions and requirements (`kernel/kinds.py`, `kernel/objects.py`, `kernel/events.py` as they are), `kernel_shadow.py`, `kernel_shadow_audit.py`, the `kernel-shadow` commands, `[kinds]` and role `subscribe` configuration, `objects_*` and `events_*` tools |
| 2. Proposals, decisions, questions, answers, notices | Nothing new | Queues for each, and the policy text for who writes and reads them and what a reader does | insights, insight_index, insight_tools, decision_delivery, decision_reconciliation, decision_idle, record_migration, operator_records, operator_record_index, host_records and the insight branches in shared modules |
| 3. Work items, waits, completion | Nothing new | Work as messages in a queue, waits as delayed messages or as reading for a referring message, completion as policy (the holder's judgement and an independent check) | work_items and work_item_* modules, completion_evidence and completion_* modules, ready-work projection |
| 4. Wake and scheduling | Daemon sleeps on wakes and the earliest `deliver_at`; outcome effects from configuration | Outcome names and effects per role; which queues wake each role | idle_seconds re-evaluation loop, readiness recheck cadence, event-recovery polls, CI re-wake cadence, fixed wake order in runtime/scheduling, progress outcome lists |
| 5. Drivers | Driver observations appended to configured queues; generic driver operations | Version control, CI, pull requests, publication and review as driver operations plus queues and policy; the default branch as `[vcs] default_branch` | ci_* modules, the pull-request model in vcs, reviews, publication-state projection, `GO <branch>` title parsing, CI wait kinds |
| 6. Child units and the writer | Child units started asynchronously, their result appended to a queue; workspace write is a resource with one holder | Delegation and consultation as role configuration; work order, continuation and parking rules as policy | delegation/consult special cases, work_order, continuations, `workspace == "write"` behavioural branches |
| 7. Text and periphery | Unit openings and tool descriptions state facts only | Behavioural guidance in role policy files | instructional prompt text, installer source testing, smoke retention and `smoke_refs` in record schemas |

## Invariants kept throughout

Authentication of authors, append-only history, fail-closed validation,
bounded reads, sandbox isolation, grants, budgets and the single-holder
property of a writable workspace are kernel integrity and stay. States, waits
and completion are userland policy over queue messages and work-item records;
the kernel enforces only integrity and bounds.

## Done

- An idle deployment's daemons use near-zero CPU regardless of how many
  messages are retained; they wake only on messages in queues that wake them
  and on due delayed messages (plus one bounded driver observation where the
  external system cannot notify the host).
- Adding a queue or a role, or removing review or audit, needs configuration
  and policy text only.
- `src/mizu` contains no role type, record state or lifecycle, decision or
  answer vocabulary, forge or CI concept, or behavioural prompt text.
- A deployment using `examples/development/` behaves as today.
