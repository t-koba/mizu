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
`PREVIEW_BYTES` 128 KiB / `MAX_FRAME` 1 MiB / `SCRIPT_MAX` 64 KiB (`fs.py`); include
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
decision records are never candidates, so usage and dashboards keep working. Defaults
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
| `PREVIEW_BYTES`, `MAX_FRAME`, `SCRIPT_MAX`, insight/report/finish/consult/search sizes | 128 KiB / 1 MiB / 64 KiB / 60 KiB / 48 KiB / 12+32 KiB / 8 KiB+8 / 1k+4k | Stays fixed | Framing and memory safety: unbounded reads or messages risk OOM and protocol desync |
| Sandbox termination, `MAX_VERIFY_COMMANDS`, include depth/files/bytes, web redirect/media, restore `--max-bytes` | 1 s+5 s / 32 / 8+32+1 MiB / 3 / 1 GiB | Stays fixed | Termination, fan-out, recursion, and archive-bomb safety floors |
| `MAX_RUNS_SCANNED`, `MAX_RECENT_ENTRIES`, record size caps (`usage.py`) | 5000 / 200 / 1 MiB | Stays fixed | Unbounded run scans stall usage/dashboard; records stay complete on disk |
| `MAX_DECISIONS`, `REASON_TRUNCATE` (`dashboard.py`) | 10 / 500 | Moves to policy | Pure projection: raw decisions stay on disk, only the small-screen slice changes (`[limits] dashboard_decisions`, `dashboard_reason_chars`) |

## ADR-026 — In-process lock guard with a fixed 30 s liveness bound
`fs.lock` serializes lock files within the process with a per-path guard keyed by canonical
path, plus a fixed 30 s blocking-acquire bound shared with Windows `lock_fd`. Contention fails
as `Busy` instead of hanging the daemon; the bound is not an operator knob
(`fs.py` `_LOCAL_BLOCKING_TIMEOUT`, same 30 s as platform `lock_fd`).

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

## ADR-029 — Budget day follows the configured timezone
Day-files name the date in `timezone` (UTC default unchanged when unset) via
`config.resolve_timezone`, so the reset that fired at 09:00 JST now fires at
midnight Tokyo time; each record carries the `zone` that wrote it alongside the
ISO date. Projects sharing one `data_dir` should share one `timezone`: different
zones name different days for the same instant, splitting the shared total across
day-files. Reaping (`retention_days`) uses each caller's zone window and stays best-effort.
