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
| `insights.py` | Submission identity, revisable proposals with separate audit, decision history | Acceptance or rejection |
| `web.py` | Allowed retrieval and external source receipts | Scientific truth |
| `editor.py` | Snapshot export and read/propose MCP | Code editing |
| `report.py` | Escaped static artifact (Markdown + evidence) with atomic latest pointer | Presentation, retention policy |
| `dashboard.py` | Bounded static JSON of recorded facts with atomic latest pointer | Presentation, per-project panels |
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
and the dashboard window edge use the configured `timezone` day boundary so
`mizu usage` groups agree with `mizu budget` for the same day.

`continue` starts another meaningful unit. `wait` sleeps until its deadline,
new information, a changed goal or a human wake. `blocked` needs a wake or new
information. `done` is not reopened by news alone; an explicit wake or changed
goal can reopen it. Other scheduled roles are independent until the project is
paused/disarmed.

## Insight handoff

The submitting route assigns the source identity. A record contains ID, source,
created time, base snapshot, title, Markdown body, revision identity (`rev`)
and update time. Ordinary prompts, lists, reads and the dashboard expose only
the current content, its current decision/evidence gap and `rev`; obsolete
claims are replaced, never appended. The first submission creates `rev` 1.
The original submitter may revise under the stable ID via compare-and-swap on
`expected_rev`; identical retries are no-ops that do not advance the inbox
generation, while a meaningful revision archives the prior record to
`insight-revisions/<id>.r<rev>.json` and becomes pending again. A crashed
revise converges on retry: an orphan archive for the still-current rev is
overwritten, and a same-content retry preserves the stored `run` (revision
provenance recorded only on meaningful change). The current transport
exposes revise/history/withdraw only to the operator (`mizu insight revise/history/withdraw`);
other sources supersede obsolete claims via a new insight until a reviewed
release adds an owner revise transport. Prior
revisions are returned only by the explicit history path, never injected into
routine context, and are durable audit like decisions (backed up, never
pruned). Direct ID reuse with different content outside revise is rejected.
Decisions bind to the reviewed `rev`, so a prior approval never authorizes
changed content; deferral still requires a revisit condition and stays
pending. Author withdrawal (`mizu insight withdraw`, submitter or operator
only) closes a pending proposal with an actor-labelled rev-bound record in
the same decision store; it is refused over a revision-current substantive
decision and over an unseen revision, a repeated request is a no-op, and
`reject` keeps meaning substantive rejection. Decided (non-deferred,
revision-current) proposals older than
`limits.retention_days` are reaped on ingest; their decisions persist in
`decisions/` and `decision-history/`, so inbox scans stay bounded. The prompt
offer keeps the newest `limits.pending_insights` proposals so recent evidence
is never hidden behind the bound. Worker discretion applies to proposals,
not to the operator's goal or prohibitions.

Editor uploads are first atomically claimed into a private ingest directory;
they are not parsed while still mutable in the Editor outbox. Malformed records
and non-regular objects are quarantined. A crash after claim can be recovered.
The captured inbox generation belongs to the beginning of a unit, so proposals
arriving during that unit do not vanish behind an advanced cursor.

## Multiple models and recovery

Profiles explicitly select engine/provider/model/session and native options. Persistent sessions are
keyed by role, model, goal and policy/grants. A changed key opens fresh context;
old sessions are kept for audit. No silent provider fallback or automatic replay
is implemented. Operators can change profiles at a boundary, and Worker can
request independent consultations against the same snapshot. Consultations
share budget/slot limits and fail independently; they cannot edit code.
A resumed persistent session receives only an append-only delta (goal pin,
current snapshot pin, proposals not yet offered), never the full goal and
history again; a new, compacted-away, or rotated session receives the full
prompt. Rotation is operator policy via `[limits] session_max_tokens`,
`session_max_cost_usd`, and `session_max_age_seconds` (0 disables each):
a due rotation removes `session.json` so the next dispatch mints a fresh
provider session while the published snapshot and composed policy reload
unchanged, recording the reason in `rotation.json` and the mode/key in
`prompt_projection.json` (ADR-028).

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
