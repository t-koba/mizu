# Architecture

## Boundary first

Mizu separates the control plane, the model process, the execution sandbox and
published evidence. Role names are configuration, not a class hierarchy. The
Python runtime executes capability-bearing work units. A Markdown policy
explains which work to choose; it cannot grant capabilities or change limits.

The trusted computing base consists of the Linux kernel, the operator account,
Python, the Mizu release, Node/Pi and their installed dependencies, Podman and
the selected container images. Provider responses, repositories, search results,
experiments and proposals are untrusted inputs. The trusted host adapter never
executes a model-provided host command.

## Components

| Module | Responsibility | Does not decide |
|---|---|---|
| `config.py` | Strict typed TOML, grants, limits, profile resolution | Project priorities |
| `drivers.py` | Engine routing by profile, shared invocation admission | Model/provider selection |
| `runtime.py` | Admission, locks, capability dispatch, publication | Which algorithm is best |
| `pi.py`, `adapters/pi/` | Version-specific RPC and session transport | Model selection policy |
| `codex.py`, `claude.py` | Trusted CLI argv, isolated per-run config, event parsing | Research quality, host-tool absence |
| `mcp_proxy.py` | stdio MCP-to-bridge proxy with no authority of its own | Tool semantics |
| `sandbox.py`, `process.py` | Bounded execution, termination, cleanup | Experiment usefulness |
| `snapshot.py` | Content objects, immutable manifests, published pointer | Semantic correctness |
| `insights.py` | Submission identity, immutable proposals, decision history | Acceptance or rejection |
| `web.py` | Allowed retrieval and external source receipts | Scientific truth |
| `editor.py` | Snapshot export and read/propose MCP | Code editing |
| `report.py` | Escaped static artifact (Markdown + evidence) with atomic latest pointer | Presentation, retention policy |
| `dashboard.py` | Bounded static JSON of already-recorded facts with atomic latest pointer | Presentation, per-project panels |
| `services.py` | Render systemd configuration | Implicitly start or arm |
| `storage.py` | Paused backup, bounded restore, conservative pruning | Evidence retention policy |

There is no daemon listening on a TCP port. A short-lived, private Unix socket
connects each Pi extension to its work unit. Files are the inter-process handoff.
JSON is the machine format; Markdown is the human policy/report format.

## Work-unit transaction

1. Check arm/pause state, shared request budget, free disk and role eligibility.
2. Acquire role lock; writers also acquire the workspace lock. Acquire a bounded
   global execution slot. Import any Editor proposals into private storage.
3. Capture the published snapshot, goal digest, current wake generation and
   inbox generation. Observers get a materialized immutable code view.
4. For command-capable real runs, check rootless Podman prerequisites and clean
   up leftover labelled containers. Record the run and active marker.
5. Launch Pi from an empty control directory. Built-in tools, discovered
   extensions, skills, templates, themes and project context are disabled.
6. Require the trusted extension handshake and the exact configured provider
   and model. Admit provider requests before dispatch. Enforce capability grants
   and limits on every bridge operation.
7. `finish` seals the work unit. Wait for Pi's `agent_settled`, not `agent_end`.
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
normal Linux semantics. Use a local filesystem, not NFS/object-store/FUSE mounts
with weaker locking/rename guarantees. Disk/host failure recovery is not a
substitute for independent backups. Snapshots have hashes, not digital signatures.

## State and history

A snapshot ties together goal, compact state, code digest, verification evidence,
summary, outcome, run ID and wake conditions. Code objects are content-addressed
and deduplicated. Files are byte-preserving; executable status participates in
the code digest. Symlinks, hardlinks and special files are not materialized as
ordinary source. Plain-directory import refuses unsupported skipped entries;
verification cannot pass if the captured tree contains them.

The history index retains 128 published IDs; full manifests/objects remain on
disk. Prompts carry a bounded, capability-gated projection: up to
`limits.prompt_snapshots` recent anchored summaries with exact IDs and code
digests, plus only the actionable context the role is granted to use.
`pending_insights` is offered only to roles with `insights`/`decide`;
`acceptance_commands` only to roles with `verify`. Exact snapshot references
(`id`, `code_digest`, `state`, `verification`) are always pinned, never
summarized. `diff` compares the anchored
snapshot with the preceding distinct code digest. Large and binary files are
listed rather than inserted into a huge diff. This is bounded context, not a
vector database or an automatically complete history search.

`continue` starts another meaningful unit. `wait` sleeps until its deadline,
new information, a changed goal or a human wake. `blocked` needs a wake or new
information. `done` is not reopened by news alone; an explicit wake or changed
goal can reopen it. Other scheduled roles are independent until the project is
paused/disarmed.

## Insight handoff

The submitting route assigns the source identity. A record contains ID, source,
created time, base snapshot, title and Markdown body. Reusing an ID with identical
content is safe; changing content under that ID is rejected. Original proposals
are immutable, decisions are separate, and decision changes append history.
Deferral requires a revisit condition. Decided (non-deferred) proposals older
than 31 days are reaped on ingest; their decisions persist in `decisions/` and
`decision-history/`, so inbox scans stay bounded. Worker discretion applies to proposals,
not to the operator's goal or prohibitions.

Editor uploads are first atomically claimed into a private ingest directory;
they are not parsed while still mutable in the Editor outbox. Malformed records
and non-regular objects are quarantined. A crash after claim can be recovered.
The captured inbox generation belongs to the beginning of a unit, so proposals
arriving during that unit do not vanish behind an advanced cursor.

## Multiple models and recovery

Profiles are exact provider/model/thinking triples. Persistent sessions are
keyed by role, model, goal and policy/grants. A changed key opens fresh context;
old sessions are kept for audit. No silent provider fallback or automatic replay
is implemented. Operators can change profiles at a boundary, and Worker can
request independent consultations against the same snapshot. Consultations
share budget/slot limits and fail independently; they cannot edit code.

Repeated failures pause a project. A restarting worker inspects actual files and
previous failure evidence before further action. It does not resend the last
command blindly, run `git reset --hard`, or manufacture a successful observation.

The runtime prompt carries only protocol framing (goal, anchored snapshot
references, capability-gated pending insights and acceptance commands); sealing
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
