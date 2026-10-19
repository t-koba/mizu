# Configuration reference

The complete, commented canonical example is `config/config.example.toml`.
Run `mizu configure` once; edit the private copy, not the distributed template.
Unknown keys, invalid types, unknown grants and
missing policy files fail validation. Configuration paths are resolved relative
to the config directory; `~` and `${ENVIRONMENT_VARIABLE}` are supported.
Environment substitution reads the controller process environment, not
`credentials.env`. `${VAR}` is fail-closed (unset or empty refuses load with
`ConfigError`); there are no implicit defaults so a missing export cannot
silently change grants or paths. Prefer literal model aliases for systemd services.

## Decision-to-work policy

`mizu_decide` (the `decide` capability) accepts optional
`work_item_intent: "create" | "none"` on `accept`/`modify` only. The operator
CLI uses `mizu insight decide <project> ... --work-item-intent create|none`.
Other intent values or action combinations are refused before any event is
written. Omission resolves from the stored, trusted insight `source`, never
its title, body, opaque `origin`, or the deciding role. Policy is configured as:

```toml
[insight_decisions]
work_item_intent_default = "create"
[insight_decisions.work_item_intent_by_source]
operator = "none"
vcs = "none"
```

Without this table the global default is `create` and the per-source map is
empty; unknown sources use the global default. Both settings accept only
`create`/`none`; map keys must be source identifiers. The example configuration
suggests `none` for operator/VCS dispositions, not a mechanism restriction:
every accept/modify may explicitly override either default. Editing the example
does not update an existing private operator configuration.

Every new accept/modify event stores the resolved `work_item_intent`. `create`
keeps the canonical `wi_<event-id>` seed; `none` is a durable explicit no-work
disposition without a seed. Both resolve work-item coverage. Old unseeded
accept/modify events without the marker remain unresolved until the existing
hash-bound open/drop reconciliation; neither reads nor new policy infer or
rewrite their intent. Explicit no-work events cannot be reconciled. The legacy
audit page includes them as `work_item_intent: "none"`, `openable: false`, and
`resolution: {outcome: "none"}`; no work-item status is attached to them.

A substantive `accept`/`modify`/`reject` at the current insight revision prevents
conflicting further decisions on that revision. An exact keyed replay returns
the original event without appending history or changing its recorded run,
whether the current pointer already matches or needs repair after an uncertain
write. For recovery, omitted intent reuses the stored resolution despite later
policy changes; conflicting action/reason/revisit or explicit intent is refused.
Revise the insight meaningfully to reopen; an earlier defer alone may still be
superseded. New decisions use a bounded keyed content lookup; only a legacy
defer pointer without `decision_key` may require scanning old random-ID event
contents. The shared safety scanner checks decision-history directory metadata
and atomic-write temporaries before all new event writes (see architecture.md).
Seed retries also validate storage and the transition chain before success or
pointer repair. Revision CAS, append-only history, and source binding are
unchanged. Read current state after uncertainty; refusals write no event or
seed. Existing decision/seed byte bounds and lock/cancellation behavior apply;
the decision-to-work policy adds no grant or scheduling. Independent task
creation uses the separate grant below. New `create` decision seeds store the
exact decision reason as immutable `description` (1–4000 nonblank characters),
and replay verifies that binding. Existing decision seeds without descriptions
remain valid and are never upgraded or rewritten; their retries keep the old
shape and reason bounds.

## Direct work-item creation grant

`work_items_create` is a separate mutating capability, denied without an explicit
role grant. The example grants it only to `worker`; it is explicitly forbidden
for consultation and delegation children, even when their configured role has
it. `work_items_read` does not imply create. The runtime binds actor to the role;
there is no model-supplied actor or implicit assignment.

Supply a stable `idempotency_key` (1–128 characters) and `title` (1–200), optionally
`description` (1–4000 characters with at least one non-whitespace character)
and/or `origin` with exactly one `insight_id` or `work_item_id` plus a positive
integer `rev`. Description text is immutable and preserved exactly, including
whitespace; explicit null is invalid. Omitting description preserves the v1
seed shape; supplying it uses the v2 field set. Both remain readable and keep
their original shape through transitions. The origin's current revision must
match at first commit. Keys are scoped to the runtime role: retain the exact
key/title/origin and description presence/text after an uncertain response.
Exact retries return the original creation record with `created: false`, even
after valid transitions or origin revision changes; they validate stored history
without rechecking the live origin. Changed requests with the same key are denied,
and retries do not count as tool progress. First commits share decision seeding's
preflight and refuse nonempty or unsafe item slots, including orphan temporaries;
no existing record is overwritten.
Null and parent origins never acquire an inferred submitter for terminal routing.
See [the runtime and persistence contract](architecture.md) for validation,
projection, history, event delivery, cancellation and backup behavior. The
work-item ledger is the single record of accepted and planned work, including
blocked conditions; the finish outcome and structured wake source record why a
unit stopped, while `Now`/`Next` are short working notes, not a parallel backlog.

## Root keys

| Key | Meaning |
|---|---|
| `data_dir` | Private local filesystem state; restart before moving it |
| `engines` | Named Pi/Codex/Claude environments: trusted `command` argv and private `directory` |
| `timezone` | IANA zone for Linux calendar timers and the request-budget day; default UTC. macOS uses explicit local; Windows supports local and UTC (see completion-contracts.md) |
| `consult_role` | Default answering role for consultation when the caller names none; unset fails closed (explicit roles required) |
| `exclude` | Snapshot/import exclusion patterns; not a secret detection system |
| `include` | Shared TOML fragments merged before validation; duplicate keys are errors (see below) |

The daemon reloads configuration at work-unit boundaries. Changing schedules
requires regenerating units and `systemctl --user daemon-reload`. Changing an
active grant or model does not mutate an in-flight call. Pause first for urgent
changes. Operator policies live outside model-writable trees.

## Shared fragments (`include`)

`include = ["shared/base.toml"]` at the top level merges shared TOML
fragments before validation, so a fleet of configs can share one reviewed
base (limits, engines, sandbox floors) while each file keeps its own roles.
Fragments use the same schema as the main file and may themselves include
further fragments. Merge first, validate after: the merged document must
pass every existing rule (unknown keys fail, required tables still
required). Operator tooling that concatenates configs
(`mizu-env/ops/render-config.py`) can retire in favor of this knob.

- Schema: root-only `include` array of strings. Only the top level and
  fragments may carry it; it is consumed at load and never becomes a
  runtime setting. Tables deep-merge; any duplicate leaf value (same
  dotted path in two files, e.g. `limits.idle_seconds` or
  `roles.worker.profile`) is a `ConfigError` naming both files. Arrays
  never concatenate: defining the same array twice is a duplicate.
- Bounds (fixed mechanism per ADR-006, not knobs): include depth at most
  8, at most 32 files, at most 32 entries per file, 1 MiB total bytes
  across the graph. Each entry is at most 4096 chars and must end in
  `.toml`.
- Trust: include entries are literal relative paths resolved against the
  directory of the file that declares them. Absolute paths, `~`,
  `${VAR}`, NUL/newlines are refused, as is any symlink in the written
  path or the target itself. Relative values *inside* fragments (policy
  paths, engine directories, `data_dir`) resolve against the top-level
  config directory, exactly as if inlined. `${VAR}` expansion is
  unchanged: fail-closed at the same call sites, never applied to
  include entries.
- Retry/cancellation: load-time single pass with no retries; a missing
  fragment, cycle, or oversize graph fails the load immediately.
- Evidence: duplicate errors name the dotted key and both files
  (`Duplicate configuration key 'limits.idle_seconds' defined in
  <first> and <second>`); cycle errors name the chain.
- Failure: `ConfigError` on bad entries, missing fragments, symlink
  escapes, cycles, bound breaches, duplicates, or unknown keys in any
  fragment. Existing configs without `include` load unchanged.

## Limits

| Key | Scope and description |
|---|---|
| `daily_requests` | Per-project provider admissions per day in the configured `timezone` (UTC default; 0 disables, max 100,000). Day-files older than `retention_days` are reaped. Pi counts logical model requests, Codex counts turns, Claude counts queries |
| `shared_daily_requests` | Optional shared total across all projects sharing `data_dir` per day in the configured `timezone` (UTC default; 0 disables the shared cap, max 100,000). Per-project admissions also count toward this total; the tighter bound refuses first |
| `requests_per_run`, `tools_per_run` | Per-work-unit request and tool bounds (project default; a role may override with `roles.<name>.requests_per_run` / `tools_per_run`, same range, unset keeps the project value); child consultations consume shared daily budget. Hitting a per-run bound grants the configured total wrap-up allowance (`wrap_up_requests` requests, `wrap_up_tools` wrap-up tool calls limited to `finish`, `research`, `submit_insight`, `attach_evidence`, `decide`) so the unit can seal a clean `bounded` end that runs again like `continue` without counting toward `max_failures`; without a finish inside it the unit fails as before |
| `wrap_up_requests`, `wrap_up_tools` | Total wrap-up allowance granted when a per-run spend bound is hit (defaults 3 requests and 5 wrap-up tool calls; range 1–1,073,741,824). A role may override with `roles.<name>.wrap_up_requests` / `wrap_up_tools`, same range, unset keeps the project value. The reserve wall window (`wrap_up_at`) divides the wall by this request allowance plus one |
| `delegate_wrap_up_requests`, `delegate_wrap_up_tools` | Aggregate share of that same wrap-up allowance available to delegated children across one caller unit (defaults 2 requests and 4 tools; ranges 0–2 and 0–4). Any unused child share remains available to the caller; at least one request and one wrap-up tool always remain for the top-level seal. Child normal request/tool counts and daily admission remain shared with the caller; consultation units are unaffected |
| `run_seconds` | Whole-unit monotonic wall deadline, default `1200`, integer 1–1,073,741,824 seconds. `roles.<name>.run_seconds` inherits when unset; integer 0 disables only that role's whole-unit wall, and positive integers up to the same maximum override it. Resolved once per Context, never renewed by selector fallback |
| `model_request_seconds` | Positive finite logical-request deadline, default `1200`, integer 1–1,073,741,824 seconds; no role override. Host-enforced independently of the whole-unit wall. Pi/pi-durable: each admitted `model_request`; Claude: enclosing query; Codex: enclosing turn, not every opaque provider HTTP subcall |
| `command_seconds` | Deadline for each individual container command (unchanged by role wall overrides) |
| `work_items_prompt` | Maximum open work-item rows included in each full and delta prompt (1–100; default 20). Overflow and legacy-unseeded coverage are reported explicitly; the tool page limit is separately available through `work_items_read` |
| `idle_seconds`, `cooldown_seconds` | Daemon sleep between poll iterations and pause between work units |
| `default_wait_seconds`, `maximum_wait_seconds` | Default and maximum model-selected wait times (1–86,400s) |
| `progress_signals` | Signals qualifying a writable `continue`/`bounded` unit as progress. Default `tool`, `code`, `decision`, `insight`, `input`; any selected true signal qualifies. `state` is supported but opt-in because a free-text rewording alone is not substantive progress. Names must be unique supported signals; `[]` means no unit can qualify. `tool` is a successfully completed non-finish Mizu operation except pure reads of existing state (`files`, `read`, `diff`, `insights`, `research_read`, `vcs_read`, `work_items_read`, `work_items_legacy_read`, retained-evidence reads, exact `work_items_create`/`work_items_reconcile` replays (`created: false`), and replay of an existing fetch/probe/search receipt), or a new native engine-tool authorization (not an idempotent replay), `decision` a successful `decide`, `insight` a successful submit/revise/withdraw/evidence attachment, `code` a changed final code digest, `state` an explicitly selected changed finish state (including paraphrases), and `input` a changed inbox/operator wake generation, consumed decision/terminal-work-item/wait events, CI failures due for this snapshot, or an expired prior wait. Persistent obligations and non-due cached red CI are not repeated input progress; a bounded identity digest distinguishes newly offered obligations from unchanged pending visibility. |
| `no_progress_continuations` | Number of consecutive no-progress `continue`/`bounded` snapshots the same role may self-admit before parking (0–100; default 0 parks after its first no-progress finish). Selected progress, role change, or signal-policy change resets the streak. |
| `no_progress_recovery_after` | Consecutive no-progress `continue`/`bounded` units before a persistent session is rotated to a fresh conversation on its next already-admitted dispatch (1–100; default 2). It never schedules a run; `mizu status` separates a due no-progress threshold (`pending_work: true`) from actionable rotation (`due` only for persistent profiles, `not_applicable` for ephemeral ones, and `conditional` for mixed selector choices). |
| `no_progress_backoff_seconds` | Timer wake delay for a parked no-progress `continue`/`bounded` snapshot (0–86,400; default 0 means no timer wake). A role may override with `roles.<name>.no_progress_backoff_seconds`, same range, unset keeps the project value. Explicit goal/operator/input/CI/wait wakes remain available. |
| `max_failures` | Consecutive role failures before auto-pause (0 disables auto-pause; `Busy`/`InfraExceeded` infra waits (daily budget, disk reserve) defer without counting, plain `LimitExceeded` bound faults still count, `mizu resume` resets to 0 — ADR-016, ADR-017; matched selector error rules do not count unless their `counts_as_failure` is explicitly true) |
| `parallel_runs`, `parallel_consults` | Independently bounded global caller execution slots and per-consultation role fan-out; admitted callers lend their slot to children, so `parallel_runs = 1` permits consultation/delegation |
| `busy_retry_seconds` | How long retrying one-shot `mizu run` callers wait for admission (0–86,400s; default 300; 0 disables waiting). A shared local FIFO queue admits at work-unit boundaries, skipping resource-blocked tickets without changing their place. Pending capacity is derived from `parallel_runs`: one waiter per configured slot; overflow defers immediately with `Busy`. Fail-fast callers (including daemon dispatch and recovery probes) never bypass older pending tickets. Retries poll at `idle_seconds` cadence, holding no role, workspace or execution-slot locks while waiting. Authority, budget and disk guards are rechecked before attempts and at dispatch; drain, pause, unarmed state and stop end the wait. The daemon retains its defer-and-repoll loop |
| `drain_yield` | Strict boolean, default `true`. Explicit project drain ends persistent-session, non-ephemeral units at the next safe logical-request boundary using the existing reduced-grant wrap-up (at most 3 requests and 5 wrap-up tools; daily admission still applies). Active requests, tool dispatches and joined children are never preempted. A seal keeps its normal outcome (normally `continue`), session and workspace; an unsealed normal return or exhausted wrap-up allowance durably records non-failure `status="bounded", bound="drain"` without publication or health changes. Failure to store that result remains a failure. Non-persistent/ephemeral units finish undisturbed; `false` preserves drain-as-admission-only behavior. Ordinary queued waiters never trigger yielding; FIFO admission and wall/request deadlines are unchanged |
| `missing_finish_followups` | Extra bounded turns after a managed engine normally returns without `mizu_finish` (0–3; default 1; 0 keeps fail-fast behavior). If the return made no Mizu or engine tool calls, the continuation retains the role's full grant and tells it to begin the requested work; after any tool call, the continuation remains finish-only to prevent repeating completed work. Every new query/turn/request uses existing admission, per-run and daily budgets, and the same absolute deadlines. The active session continues only within this work unit; ephemeral sessions stay run-local. Provider/transport/protocol errors and explicit cancellation are never relabeled or retried |
| `missing_finish_counts_as_failure` | Whether exhausting the missing-finish follow-ups increments the existing failure brake (boolean; default `true`, preserving today's behavior). Scoped only to this terminal missing-finish outcome; it does not change selector `counts_as_failure` rules |
| `missing_finish_recovery_after` | Consecutive missing-finish failures (unit ends without a sealed finish after exhausting `missing_finish_followups`) before a persistent session is rotated to a fresh conversation on its next already-admitted dispatch with unblocked open work (1–100; default 2, strictly below the default `max_failures` = 3 brake). It never schedules a run and never bypasses admission; a fully blocked/empty open-work queue never rotates. A sealed unit, a non-missing-finish failure, a bounded drain/deadline/authority outcome, or an infra-wait defer resets the streak to 0 (persisted as `consecutive_missing_finish` in the role health record). Rotation reuses the session-generation mechanism (bumps generation so the old conversation is retired and the next dispatch starts fresh); `rotation.json` and `prompt_projection.json: rotation` record the `missing-finish units N >= threshold` reason. If the rotated session also fails, the ordinary `max_failures` brake still pauses the project. |
| `output_bytes` | Maximum captured stdout/stderr per command |
| `file_bytes` | Maximum complete-file byte size admitted by `read` and retained-evidence records; default 8 MiB. The `started.json` authorization record is additionally capped at 64 KiB. Admitted files are paged in 8,192-character responses, with `content_id` verification across pages |
| `snapshot_bytes`, `snapshot_files` | Total visible snapshot size and file-count bounds |
| `history_index` | Maximum published snapshot IDs retained in the immutable history generation (8–1,000,000; manifests stay on disk) |
| `prompt_snapshots` | Number of recent anchored snapshot summaries offered to prompts (1–64) |
| `role_state_bytes` | Maximum canonical JSON bytes of one role-owned current research state record (1,024–65,536; default 8,192) |
| `role_state_prompt_bytes` | Maximum bytes of a role's own research state injected into its prompt (1–65,536; default 4,096); over-bound records inject status/generation/sizes only |
| `retention_days` | Daily budget-file retention and decided insight live-inbox window (default 31 days; 0 disables; 0–3,650). Current-revision terminal insights move permanently to `insight-archive.sqlite3` before inbox removal; bodies/evidence are not deleted. Ingest still reports removed live copies as `reaped`. `insight list` remains live-only; explicit `insight page --scope archive` enumerates bodies and validated legacy identity-only tombstones (`body_unavailable: true`); ID reads include archived bodies and explicitly deny missing bodies. Archive retention is permanent, with no expiry/deletion knob (see operations for one-time migration and retry behavior). This setting never deletes snapshots, whole run directories, decisions, or archived insight bodies; bounded per-run dispatch measurements use the separate `event_log_*` retention below |
| `event_log_compress_days` | Compress per-run engine logs and bounded dispatch measurements (`*-events.jsonl`, `diagnostics.txt`, `dispatch-attempts.json`) at or beyond N days old to `.gz` (0 disables; 0–3,650; default 7) |
| `event_log_retention_days` | Drop those raw/compressed logs and dispatch measurements at or beyond M days old (0 disables; 0–3,650; default 31). Result/error/consultation/started/selection/runtime-usage records, snapshots, objects, sessions, decisions, and proposals are never candidates |
| `event_stream_bytes` | Per-run engine event-stream retention in bytes (1 MiB–256 MiB; default 4 MiB). Bytes past the budget are dropped from `*-events.jsonl` with an explicit `*-events-truncated.json` marker while parsing and the run continue, so diagnostic volume can never discard sealed work. The per-record transport bound stays fixed mechanism |
| `insight_cursor_ttl_seconds` | Absolute lifetime of `insight page` continuation cursors (60–604,800 seconds; default 86,400). Continuations never extend expiry. Expired cursors fail explicitly; restart enumeration. This is not archive retention |
| `pending_insights` | Newest pending proposals offered in prompts (1–1,000; default 30). `mizu insight list` uses 1,000 |
| `decision_events_limit` | Decision events delivered per dispatch per role (1–100; default 10; 100-event cap stays fixed mechanism). With rotation, a role with many deferrals sees each one every N/value runs |
| `work_item_events_limit` | Terminal work-item events delivered per dispatch per role (1–100; default 10; 100-event cap stays fixed mechanism). Independent of the decision batch: each source uses only its own limit |
| `session_max_tokens` | Cumulative session tokens at which a resumed persistent session restarts with a fresh session key (0 disables; 0–1,073,741,824; default 0). The published snapshot and composed policy reload on the next unit; Pi accumulates per-run usage into the session record, Codex/Claude reuse their saved cumulative totals, unknown shapes count 0 (age still rotates) |
| `session_max_cost_usd` | Estimated session cost (USD) at which a resumed persistent session restarts (0 disables; 0–1,000,000; default 0). Cost is the native provider estimate where reported (ADR-005); sessions without cost evidence never rotate on this bound |
| `session_max_age_seconds` | Wall-clock conversation lifetime (stable creation time surviving same-id saves; legacy files fall back to mtime) at which a resumed persistent session restarts (0 disables; 0–31,536,000s; default 0) |
| `free_disk_mb` | Minimum free disk space required to start a work unit |

Successful work-unit artifacts record `started.json.admission_elapsed_seconds`: elapsed monotonic time from FIFO admission entry through post-acquisition revalidation, including queue/lock and guard overhead but excluding selection and model execution. It can be joined by run ID with `prompt_projection.json`'s hashed `session_key` and `result.json` usage for shadow cache-affinity analysis; the key is a grouping proxy, not proof of provider cache identity or residency. Only runs that produce `started.json` are represented, so rejected/cancelled admission attempts and pre-run skips are absent.

A role wall of `0` is not an infinite model request: all four managed engines
remain host-bounded per logical request. Pi adapters report admission and
completion even without usage; progress, usage, heartbeat, and duplicate
admission/completion never renew a request timer. An engine stalled before or
between logical requests is also bounded by `model_request_seconds` (startup
handshakes retain their separate 30-second bounds). An admitted Pi tool keeps
its own command/adapter bounds, not the between-request stall timer.
Pi-durable's process is polled against these host deadlines; no launcher-wide global timer truncates an
admitted join. Timer values remain positive finite numbers on the bridge;
disabling a role wall does not serialize infinity or a zero timeout.

Finite units reserve `min(model_request_seconds, run_seconds / (wrap_up_requests + 1))`
seconds at the end of the existing wall for wrap-up (`wrap_up_requests` is the
configured total, project default with per-role override). At that boundary ordinary
work is refused; the configured wrap-up allowance and wrap-up-only grants
apply, and `finish` records `bounded`. This is an opportunity to seal, not a
guarantee that all maximum-duration requests/tools fit: neither the wall nor the
allowances are extended. Without a finish by wall exhaustion, `result.json`
records `status="bounded"`, `bound="deadline"`, and
`publication="not_published"`; no snapshot changes or `max_failures` increment.
Logical-request timeouts and pre-seal provider errors retain their existing
failure behavior. A valid finish seal remains separate from adapter settlement
and all final integrity/verification/publication checks still apply.


All limits are positive integers except `daily_requests`, `shared_daily_requests`, `max_failures`, `busy_retry_seconds`, `missing_finish_followups`, `free_disk_mb`, `retention_days`, `event_log_compress_days`, `event_log_retention_days`, `session_max_tokens`, `session_max_age_seconds`, and adapter `diagnostic_bytes`, which may be zero. `missing_finish_counts_as_failure` and `drain_yield` are booleans. `session_max_cost_usd` is a number (integer or float) which may be zero.

Run-evidence retention keeps growth bounded: `mizu prune` (paused project, dry-run by default, `--apply` writes a `maintenance/prune-*.json` audit) gzips raw engine logs older than `event_log_compress_days`, drops raw/`.gz` logs older than `event_log_retention_days`, and drops web-cache entries expired past `[web] cache_seconds` (`retrieved_epoch` age; entries without a usable epoch fall back to mtime). Age is file mtime versus now unless noted; the `.gz` keeps the raw mtime so the drop clock does not restart. Only top-level `runs/*/` `*-events.jsonl`, `diagnostics.txt`, bounded `dispatch-attempts.json`, and `*-events-truncated.json` markers (plus their `.gz`) and expired `data_dir/web-cache/*.json` entries are candidates — `result.json`, `error.json`, `consultation.json`, `started.json`, `selection.json`, `runtime-usage.json`, admission records, snapshots, objects, sessions, decisions, and proposals are always kept, so `mizu usage` and restores keep working. Sessions are durable audit evidence: `mizu storage` reports their counts/bytes and `mizu storage --apply` never reclaims them. `mizu storage --apply` removes only unreferenced snapshot manifests and orphan objects under quiescence; it refuses when the preview reports any skipped manifest (`snapshots.skipped`, including unreadable or over-1-MiB manifests), any skipped reference record (`reference_records_skipped`, covering present-but-unreadable, malformed, over-1-MiB, wrong-shape, or failed history/run/artifact/inbox/current-pointer scans), or any skipped work-item history (`work_item_events_skipped`), because reachability is then unknown. `reference_records_skipped` counts unique record/scan paths, not failed checks (the active generation is counted at most once). JSON `null` records, invalid non-null snapshot IDs, and malformed history names, entries, digests, or active endpoints are incomplete. Pointer and history reads share the 1 MiB bound and do not follow symlinks. Missing optional run records and an absent `artifacts/latest.json` pointer are normal and not counted; an artifact directory without `evidence.json` is incomplete. Usable valid IDs are still reported, but an incomplete scan never proves references are absent. The web cache is shared under `data_dir`; the invoking project's `cache_seconds` decides expiry and a concurrent fetch recreates a removed entry, so `prune --apply` is safe to retry. Snapshots reference workspace objects only, never run logs, so compressed/dropped logs are disposable projections absent from later backups by construction.
`daily_requests` counts logical admissions, not dollar amounts; enable a hard provider spend cap/pause by default (alert-only alarms are not sufficient for unattended runs). Use `mizu status` and `mizu budget` for per-project and shared day counts in the configured `timezone`, and `mizu usage` for token statistics.
Day-files (`data/budget/YYYY-MM-DD.json`) keep the date in the configured `timezone` (UTC when unset) plus the shared aggregate, a per-project map, and the `zone` that wrote them, so two configs sharing `data_dir` with different `daily_requests` admit independently unless the optional shared total is reached. `mizu budget <project>` and `mizu status` report both numbers with the day and zone. Projects sharing one `data_dir` should share one `timezone`: different zones name different days for the same instant, splitting the shared total across day-files.

## Fixed mechanism bounds

Safety-critical bounds are fixed in code rather than exposed as configuration knobs:

| Category | Bound | Value | Purpose |
|---|---|---|---|
| **Framing & Preview** | `PREVIEW_BYTES` (`fs.py`) | 128 KiB | Diff/insight/editor preview and canonical list bounds; CLI stdin framing |
| | `READ_PAGE_CHARS` (`fs.py`) | 8,192 characters | Per-response workspace/evidence read text bound, separate from the whole-file `file_bytes` cap |
| | `MAX_STARTED_EVIDENCE_BYTES` (`runtime.py`) | 64 KiB | Hard ceiling for run metadata used only to authorize retained-evidence reads; `file_bytes` may lower it |
| | `MAX_FRAME` (`fs.py`) | 1 MiB | Maximum JSON-RPC message size across bridge and MCP servers |
| **Tool inputs** | `exec` / `experiment` script | 64 KiB | Sandbox command size (`SCRIPT_MAX`) |
| **Adapter diagnostics** | `ADAPTER_DIAGNOSTIC_BYTES_MAX` (`process.py`) | 8 KiB | Hard ceiling for any configured model-visible stderr excerpt; per-adapter `diagnostic_bytes` may lower it or disable it |
| | `submit_insight` body | 60 KiB | Proposal prose size |
| | `report` body | 48 KiB | Markdown artifact size |
| | `finish` summary / state | 12 KiB / 32 KiB | Work-unit completion summary and carried state |
| | `consult` question / roles | 8 KiB / max 8 | Consultation query and bounded role fan-out |
| | `search` query / path | 1,000 / 4,096 chars | Retrieval query and file path limits |
| **Sandbox** | Process termination | 1s TERM, 5s KILL | Clean process-group termination without orphan leaks |
| | `MAX_VERIFY_COMMANDS` | 32 | Prevents unbounded verify fan-out |
| **Web broker** | URL / redirects / media / transport | 4,096 chars / max 3 / text, JSON, XML / HTTPS-only, port 443, no URL credentials, DNS pinning | Strict URL length, redirect limit, safe media types, and fixed safe transport floor |
| **Restore** | `--max-bytes` default | 1 GiB | Archive bomb defense; override explicitly if needed |
| **Smoke probe** | `SMOKE_REQUESTS_PER_RUN` / `TOOLS` / `SECONDS` (`smoke.py`) | 4 / 5 / 120 s | Paid `smoke --live` spend cap with margin for one benign listing plus one auxiliary request; applied as `min(fixed, operator limits)` |
| **Config include** | `MAX_INCLUDE_DEPTH` / `MAX_INCLUDE_FILES` / `MAX_INCLUDE_BYTES` (`config.py`) | 8 / 32 / 1 MiB | Shared-fragment recursion, file-count, and total-byte bounds; per-file entries max 32, entry length max 4096 |
| **Locks** | `WINDOWS_LOCK_TIMEOUT` (`platform.py`) / `_LOCAL_BLOCKING_TIMEOUT` + per-path guard (`fs.py`) | 30 s | Liveness bound: contended blocking acquires fail as Busy instead of hanging the daemon thread; intra-process guard has no plausible operator tuning (ADR-026) |
| **Decisions** | `defer` requires a revisit value (`insights.py`) | always | Accountability floor: a deferral without a revisit condition parks work with no observable trigger; worker policy defers only with a revisit trigger |
| **Completion** | `done` requires passing acceptance commands on the exact code digest (`runtime.py`) | always | Publication integrity floor: a goal cannot be declared done without binding proof to the final unchanged tree; never weakened to pass |
| **Publication** | `vcs_publish` and merge in `vcs_dispose` require a recorded human `GO <branch>` approval | always | No autonomous external publication: pushes, PRs, and merges never run without recorded human approval behind the exact digest; standing grants never substitute |

`on_change` is orthogonal to scheduling: it skips execution when the published
`code_digest` is unchanged, so state-only republications do not reschedule it.
An unacknowledged decision event routed to the role (`decision_events` in
`own` mode, or any-source matches with `decision_sources = "any"` delivering
once and admitting even deferrals), a terminal work-item event routed by
`work_item_sources`, a due structured wait, a red on the configured CI wake set
for a role with `ci_wake`, or ready work is a distinct admission reason that
unchanged-code suppression must not swallow. Ready work is project-wide and
write-only: any seeded `open`/`doing` item without a structured wait qualifies,
and its admission still observes the writer's configured cooldown. See the
[scheduling contract](scheduling.md) for precedence and the bounded prompt
projection. Event delivery reaches scheduling two ways: the writer daemon
re-checks `should_run` against event, wait, CI and ready-work state every idle
tick, and `mizu service` additionally renders file triggers for any non-daemon
role with `decision_events` or `work_item_events`, with or without a schedule
(systemd path units on Linux, `WatchPaths` on macOS), that invoke the same
`mizu run` before the next interval; the timers keep their own schedules. An
event-triggered dispatch with nothing due returns `skipped: no-event` without
consuming anything; a non-event `on_change` dispatch with an unchanged
`code_digest` reports `unchanged`. A role with neither a schedule nor event
subscriptions stays explicit-run only. Linux event triggers keep systemd
defaults with no trigger limit (`Type=oneshot` with `StartLimitIntervalSec=0`
on the event service only): a burst may dispatch more than once, which is
cheap because admission is state-based, while a trigger limit fails the path
unit and stops dispatching until reset by hand. A role with `on_change` alone
and no `daemon`/`interval_seconds`/`calendar` runs only via explicit `mizu run`.

## Sandbox

The container interior is always Linux. The runtime must support standard flags (`--user`, `--network`, `--read-only`, `--cap-drop`, resource limits). Rootless Podman runs additionally pass `--userns=keep-id` so `--user <host uid>` keeps the host identity inside the container and the bind-mounted workspace stays readable; Docker has no `keep-id` mode and runs without the flag.

| Key | Default | Description |
|---|---|---|
| `executable` | `"podman"` | Container runtime binary name or path (string validated, not an allowlist; typically `"podman"` or `"docker"`) |
| `image` | `""` | Local `sha256:<hex>` or digest-pinned image reference (runtime pulls are forbidden) |
| `network` | `"none"` | Container network name; `"none"` ensures networkless isolation |
| `entrypoint` | `"/bin/sh"` | Container entrypoint; the image must provide it |
| `memory_mb`, `cpus`, `pids` | `2048`, `2`, `256` | Container resource limits |
| `temporary_mb`, `file_mb` | `256`, `128` | Temporary storage size and per-file write limit (`RLIMIT_FSIZE`) |
| `selinux_label` | `true` | Linux-only `:z` volume relabeling |
| `mounts` | `[]` | Extra read-only host mounts (`{source, target}`); system/harness paths rejected |
| `env` | `{}` | Extra environment variables; runtime vars (`HOME`, `TMPDIR`, `PATH`, `PYTHONDONTWRITEBYTECODE`, `PYTHONPYCACHEPREFIX`), process-startup hijack vars (`NODE_OPTIONS`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `BASH_ENV`) and tokens matching `(^|_)(KEY|SECRET|TOKEN)(_|$)` rejected (`HF_TOKEN`, `PUBLIC_KEY_PATH` rejected; `MONKEY_PATH`, `TOKENIZERS_*` allowed) |
| `mode` | `"rootless"` | `"rootless"` (default) or `"single"` (Linux hosts lacking subordinate ID delegation) |

Read-only root, dropped capabilities, `no-new-privileges`, user mapping, and the seccomp floor stay strictly enforced regardless of settings. For total disk storage capping, use a dedicated volume quota.

`mode` is `rootless` by default on every OS. On Linux hosts without
subordinate-ID delegation only, set `mode = "single"` and additionally configure `namespace_helper` (absolute
operator-owned wrapper argv, executed before every runtime invocation),
`podman_root`, `podman_runroot`, `podman_tmpdir`, `cgroup_parent` (a delegated
path whose final component must not end in `.slice`), and
`cgroup_manager = "cgroupfs"`. Optional `storage_driver` (`overlay` or `vfs`)
and `storage_options` (`name=value` entries passed as `--storage-opt`) tune
the store. In single mode containers run with `--uidmap 0:0:1 --gidmap 0:0:1
--user 0:0`; any stale single-mode key under `rootless` mode, or a missing
single-mode key under `single` mode, is a configuration error.
`doctor --sandbox` proves the isolation behavior (readonly source, non-root
user, no inherited secrets, the configured network posture, container cleanup) on every
OS before any project runs. With the default `network = "none"` the probe asserts
a networkless namespace; with an operator-enabled network it records the choice
instead, since exfiltration risk was explicitly accepted.

## Profiles and roles

How these settings behave today is mapped in [scheduling](scheduling.md):
schedule modes, wake sources versus delivered inputs, platform triggers,
and workspace/lock isolation.

Optional [dynamic model selection](model-selection.md) uses named TOML selectors,
explicit task attributes, rule/inference classification and operator observations.
Only roles with `selector` enable it; fixed profiles keep their existing behavior.


A profile explicitly selects `engine`, `provider`, `model`, and `session`
(`ephemeral` or `persistent`). IDs remain exact. Native `options` belong to
that engine: Pi `thinkingLevel`, `cacheRetention` (`none`|`short`|`long`; unset keeps the Pi default `short`; `long` via the Pi env channel, `none` degrades to the Pi default on pi), `settings`, `codemode`, `toolSearch`,
`excludeTools`, `scopedModels`; pi-durable `thinkingLevel` plus `cacheRetention` (`none`|`short`|`long`; unset keeps the engine/provider default; passed natively as `settings.stream.cacheRetention`, retuning keeps the store)
`durable_backend` (`sqlite`), `durable_resume` (`compatible`|`fresh`),
`durable_retention_days` (0-3650), and the per-profile
live-context bound `durable_compaction_reserve_tokens`,
`durable_compaction_keep_recent_tokens`, `durable_compaction_background_tokens`
(each 0-1073741824; omit to keep the engine/provider default, 0 disables
background). `requests_per_run` is the single per-unit spend bound on
pi-durable too: every provider request inside the submission passes `_budget`
admission, and a per-run refusal aborts the submission (`request_bound`
maps to a bound error, never a silent stop) instead of failing the unit. A set threshold rides the
Harness `settings.compaction` policy as a partial override (enabled stays true;
disabling is not exposed): generation blocks to compact above `contextWindow -
reserveTokens`, background starts `backgroundTokens` below that, and the summary keeps
`keepRecentTokens` verbatim. Unset keeps the engine default with no Mizu value
injected, so a `plan` profile that sets nothing keeps the provider default while a
`main` profile may set a half-window bound (operator policy, never a fixed
mechanism value). Larger `reserveTokens` (or `backgroundTokens`)
bounds live context tighter without rotating the session away: retuning only these
three keys (or `thinkingLevel` or `cacheRetention`) continues the same durable store with the new
thresholds on the next run. The per-run grant still binds `cacheRetention` and the compaction
thresholds, so retrying the same run under those changed values is refused; an effort-only
`thinkingLevel` retune is excluded from the grant and resumes the same run. Retuning `cacheRetention` likewise keeps the store on both pi (continuing the conversation like `thinkingLevel`) and pi-durable (the Harness reads `settings.stream` live). More broadly, a pi-durable session covers only
what makes a conversation incompatible (engine, backend, the adapter-declared store
format): policy text, capabilities and tools, goal, model, and settings ride the live
conversation (applied per submission, keeping prompt caches warm), and adapter upgrades
keep it unless they declare a store-incompatible change. Other engines keep rotation on
any effective change outside their live keys. The explicit fresh start is
`mizu session reset PROJECT --role ROLE`, which deletes that role's persistent sessions
and durable stores (refused while the role has an active run); usage and age rotation
bounds still apply to every engine.

For a durable work unit, the persisted grant must exactly equal the current
configured authority (minus effort-only live keys, which ride the live conversation)
even when the host narrows a continuation's turn role. A mid-run authority
change does not fail the unit: the next turn's resume refusal ends it as a
clean bounded result with bound `authority` (no further turns, no publication,
no failure-brake increment; see the run-walls section of
`docs/completion-contracts.md`). Daemon poll paths are unchanged and keep
their own timeouts; no configuration schema changed.
Only the effective turn's tools are exposed and authorized: a missing-finish
follow-up exposes only `finish`. A normal unsealed return is stored as terminal
`completed` with `sealed: false`; this is not a host finish seal. If the configured
follow-up allowance is exhausted, no active run row is stranded. An explicit,
one-dispatch, in-process host authorization may continue that same work unit
(and the existing reduced-grant budget/deadline wrap-up paths); it does not change
which failures qualify. The work-unit key and grant stay fixed. The first
turn's Harness `requestId` is the work-unit key; subsequent authorized turns
have distinct request IDs using `<key>-t<sequence>`. Selector candidate
ordinals (`-a<ordinal>`) still advance
only on selector failover, not on missing-finish follow-ups.

`durable_resume` defaults to `compatible`: interrupted-turn recovery reuses its
persisted request id only when the prompt digest and effective turn grant match,
in addition to exact work-unit grant equality. Changed or legacy incomplete
turn metadata fails closed. Interrupted unsafe tool effects are never replayed;
the Harness reports unknown/interrupted completion instead. `fresh` refuses
persisted-run recovery (including after a crash), but permits the explicit
in-process host-authorized continuation above. Provider, transport, protocol,
storage, cancellation and finalization failures are not missing-finish returns.

Pi-durable model accounting prefers the host's current per-request
`_model_usage` reports. If those are absent, the launcher's `reported.usage`
is a **per-dispatch delta**, never the Harness's cumulative session total,
on both normal settlement and `request_bound`. The launcher snapshots usage
before recovery is unpaused and subtracts it after settlement for the selected
provider/model. Only known nonnegative monotone counters are returned (token
counts are safe integers; costs are finite numbers, at most JavaScript's
`MAX_SAFE_INTEGER`). An absent model in a valid initial map is a known-zero
baseline; missing/malformed snapshots or counters and counter regressions stay
unknown and are omitted (or `null` when none are known), not fabricated as zero.
No accounting read admits a provider request.

Pi SDK knobs
are refused on pi-durable profiles and durable policy is refused on pi
profiles instead of silently ignored. pi-durable resolves the exact model
over built-in providers only; custom provider setups stay on pi. Pi operators bound
its live context through `options.settings.compaction` (`reserveTokens`,
`keepRecentTokens`); pi-durable operators use the three `durable_compaction_*`
keys above. Codex native configuration keys plus `options.turn` for native turn fields
(such as `outputSchema`, `effort`, `serviceTier`); Claude
`ClaudeAgentOptions` fields. Effort values are validated by the selected engine,
not a Mizu enum. Models/providers have no fixed allowlist.

`resources` are reviewed local files or directory trees with `{kind, path, sha256}` (maximum 64).
Tree hashes cover names, content and executable bits; no symlinks, at most 4096
files and 64 MiB per resource. Claude supports local skill/plugin resources,
Codex local skill resources, Pi extension/skill/prompt/theme resources.
Each run verifies content before launch. `mcp_servers` uses engine-native
connection configuration. The runtime owns the `mizu` server. Stdio commands
run inside the existing readonly OCI floor and require executables already
present in the selected image; environment belongs in `sandbox.env`. Prefer
stdio `command` servers: `url` HTTP servers stay explicit operator endpoints
outside the OCI floor, and a loopback bind alone is not a browser boundary.
An HTTP `url` must carry its upstream DNS-rebinding guard
(`enableDnsRebindingProtection` / `allowedHosts` plus `allowedOrigins` /
`hostHeaderValidation`, mcp-go >=0.56.0 or equivalent) plus auth; never serve
one as unauthenticated loopback alone. When the `url` server uses OAuth,
pin the expected issuer in the client instead of trusting server-advertised
authorization metadata: a hostile or compromised server can otherwise redirect
client credentials to its own token endpoint. Clear stored OAuth state
registered without an issuer and rotate secrets/tokens after any
untrusted-server contact. No
installation, login, package acquisition or deployment runs during work.

### Terminal work-item subscriptions

`work_item_events = []` is disabled by default. Opt in to `["done"]`,
`["dropped"]`, or both (unique values only); no other transitions are events.
`work_item_sources` (`own`/`any`) routes terminal events independently of
`decision_sources`, which routes decisions only. For terminal events, `own` routes by
**the origin insight's submitter**, not the terminal transition actor. Routing
reads the supported inbox/archive body, including revised and archived origins;
item/origin references remain those of the immutable seed. Missing source data
is explicitly `origin_source: null`, `routing: "unknown"`; only `any` opt-in
receives those events, with no actor fallback. A malformed/unsafe origin fails
closed rather than becoming a guessed route.

With neither route set, both default to `own`. For migration, omitting
`work_item_sources` preserves the legacy shared `decision_sources` route for
terminal subscriptions. In particular, a terminal-only subscription with only
`decision_sources = "any"` resolves to effective `work_item_sources = "any"`
and `decision_sources = "own"`. Legacy `any` without either subscription still
fails load. Once `work_item_sources` is explicit, the routes are independent:
each `any` requires its **own** nonempty subscription (`decision_events` or
`work_item_events` respectively). Set both fields explicitly to stop inheriting
the shared route; for example, `decision_sources = "any"` with
`work_item_sources = "own"` delivers decisions from anyone but terminal events
only for this role's findings. `mizu explain` reports the effective routes.

Terminal events are validated through the existing WorkItems immutable seed
and complete current chain, oldest terminal instant first (aware timestamps
normalized to UTC, then item ID/revision). Dispatch batches are independent per
source: the role-effective `work_item_events_limit` (role override when set,
else the project default) bounds terminal rows, 1–100 per dispatch, while the
role-effective `decision_events_limit` bounds only decisions. Either role
override is 1–100 with `None` meaning inherit; the fixed 100-event mechanism
cap still applies to both. Choose each batch to fit the role's
`requests_per_run`: a whole selected batch is acknowledged only after a
completed run, otherwise it replays. Each terminal row is
at most 8192 canonical bytes, the page at most 1 MiB; title retains the seed's
200-character bound and reason is a 1024-byte preview with a truncation flag.
Rows include stable `event: "work_item:<item-id>:<terminal-rev>"`, `source`,
`type`, item/origin references, transition, terminal time, actor and routing.
`done` additionally includes `snapshot_id` and a bounded `verification`
reference (SHA-256 of the canonical copied verification, passing flag and code
digest); the full evidence remains in the immutable work-item history.

These events admit `--event` and unchanged-code runs, appear in a separate
`work_item_events` field in both full and delta prompts, and are frozen in
`runs/<run>/work-item-events.json` before selection/execution. `started.json`
and `prompt_projection.json` record their count. Completion acknowledges only
that selected batch, after `result.json` is durably `completed`. Failures,
selector waits, busy/budget exits, bounded and unsealed runs do not acknowledge
terminal events. An event arriving during execution is not in that batch.

Acknowledgements are source-scoped exact receipts at
`work-item-receipts/<role>/<item-id>.<terminal-rev>.json`, separate from
`decision-cursors`. There is **no terminal timestamp watermark**: a later
decision or clock rollback cannot hide an unseen terminal event. A matching
retained terminal event with no receipt for the role is backfilled even if it
predates subscription; receipts remain effective across unsubscribe/re-subscribe.
Receipts are excluded before applying the delivery limit. Each receipt is written atomically;
a failed/partial batch replays only identities without committed receipts.
Atomic-write leftovers are not receipts. Receipts have no automatic expiry,
are included in backup/restore, and are not deleted by read-only views. An
acknowledgement failure does not turn completed processing into an event loss;
the next run may replay it. Decision acknowledgement/defer rotation is unchanged.

Daemon admission checks pending terminal identities each idle tick. Non-daemon
Linux/macOS service definitions also watch the work-items parent. After a
terminal append or successful/partial receipt write the parent metadata is
touched as a non-authoritative hint (nested directory writes alone are not
assumed recursive platform watch events); this permits bounded-batch draining
without requiring a receipt directory to pre-exist at service installation. No record schema or
grant changes, assignments or automatic execution are implied. These are wake
hints, not a transactional queue: a crash between append and metadata touch, a
failed hint, or a platform-coalesced/busy dispatch can delay processing until
another explicit/daemon/scheduled check. Windows keeps the existing scheduled
checks (no file trigger in its current service schema). Re-render/reinstall
service definitions after enabling a subscription; rendering does not install
or start services itself.

A role has exactly one of `profile` or `selector`, plus `policy`, `workspace` (`write`, `read`, `none`),
`capabilities`, `engine_tools` (maximum 128 explicit names), `on_change`,
`decision_events` (at most 8 unique lowercase decision actions, never
`withdraw`), `work_item_events` (unique `done`/`dropped` values, default `[]`) plus `ci_wake` (whether a red on the configured CI wake set wakes
this role: default true for writable roles, false otherwise) plus `decision_sources` (`own` default routing only this role's findings,
`any` routing matching actions from any source so a resolver runs on each new
deferral) and `work_item_sources` (terminal routing by origin submitter;
`own`/`any`, default/migration rules in
[terminal subscriptions](#terminal-work-item-subscriptions)),
optional per-role no-progress backoff override `no_progress_backoff_seconds`
(0–86,400; unset inherits the project `[limits]` value; 0 disables the timer
wake), optional per-role spend overrides `requests_per_run` / `tools_per_run` /
`wrap_up_requests` / `wrap_up_tools`
(1–1,073,741,824; unset keeps the project `[limits]` value, so an auditor may run
200/400 while other roles stay at the project default; storage, transport, budgets,
slots and rotation stay project/host policy), optional per-role event batch
overrides `decision_events_limit` / `work_item_events_limit` (1–100 each; unset
keeps the matching project `[limits]` value, so one role may take larger
decision windows while another takes smaller terminal batches), and at most one
of `daemon`, `interval_seconds`, `calendar`. `policy` is a path
string or an array of path strings (e.g. shared principles plus the role file);
parts are read as UTF-8 in order and joined with one `\n` (trailing newline
kept), so the composed text is the prompt and the policy digest. Missing,
unreadable, non-UTF-8, or oversize parts fail load; unknown keys still fail.
Bounds (fixed mechanism): at most 16 parts, each at most 64 KiB, 256 KiB total. Capabilities grant
Mizu operations; `engine_tools` grants native or external MCP tools such as
`codemode`, `web_search`, or `mcp__docs__search`. Exposure and automatic approval
never grant execution authority. Native host command/file tools cannot bypass
the bridge and publication contract. Codex currently refuses any nonempty
`engine_tools` role before launching app-server: its current adapter has no
pre-send admission hook. Native/extra tools (including extra MCP tools) remain
unavailable there until such an adapter exists; this does not alter operator
grants. Pi and Claude require a trusted SDK invocation identity at their pre-call
boundary; see [admission bounds and trust](completion-contracts.md#extra-tool-pre-send-admission).

| Engine | Managed interface | Completion | Admission unit |
|---|---|---|---|
| Pi | `createAgentSession`, `ModelRuntime`, `runRpcMode` | `agent_settled` and seal | Logical model request (includes auxiliary inference) |
| Pi-durable | Pinned pi-durable Harness over file-backed SQLite; one admitted submission per engine attempt, with per-turn Harness request IDs bound to the durable work-unit grant key | Settled submission and seal | Logical model request (includes auxiliary inference) |
| Codex | `app-server --listen stdio://` | `turn/completed` with completed status and seal | Turn |
| Claude | Official Python Agent SDK in separate interpreter | `terminal_reason=completed`, successful subtype, no error and seal | Query |

For pi-durable, the durable work-unit key is bound to the exact persisted
work-unit grant; it is not itself the per-turn Harness request ID in every
submission. The first turn uses the work-unit key as its Harness `requestId`,
and later authorized turns use `<key>-t<sequence>`. Replaying a compatible
completed sealed candidate restores its persisted host finish without new
admission; an unsealed completed row requires its one-dispatch in-process
continuation and cannot be replayed as a fresh terminal. Selector failover gets
a new work-unit key only after a known terminal `pi-durable` `unanswered` failure
and an eligible fallback has actually been selected; the bounded attempt suffix
leaves the session/store and all durable grant and replay checks intact. Other
or uncertain failures retain the work-unit key and therefore refuse a changed
grant rather than forking around unresolved work.

Settings that collide with runtime-owned prompt/model/bridge/session fields
fail before input; they are never silently overwritten. Native engine errors
remain errors. On every engine but pi-durable, session identifiers include model,
policy, goal, capabilities, native grants, effective configuration minus live
reasoning-effort keys (`thinkingLevel` and `cacheRetention` on pi, `model_reasoning_effort` on Codex,
`effort` on Claude), command and resource content digests. An effort-only (or `cacheRetention`-only on pi)
profile switch therefore continues the role's conversation with the current
effort; any other change still starts a fresh bounded session whose first
prompt carries the full goal, snapshots, pending insights, and research state.
Pi-durable sessions instead cover only engine, backend, and the declared store
format (see the profile section above), so compatible changes ride the live
conversation there.
Resume errors never start a new conversation. Persistent sessions belong to
that fingerprint; ephemeral state belongs to the run. Authentication is an
explicit operator operation. Mizu does not alter operator settings files.

Token totals are observed evidence, separate from admissions and native cost
estimates. Codex/Claude do not expose a supported Pi-equivalent hook for each
inner provider request. Claude native turn/cost limits can supplement the
query admission. Model fallback is explicit native configuration and actual
models are recorded. Model usage excludes unreported auxiliary billing.

| Capability | Authority |
|---|---|
| `files`, `read`, `diff` | Bounded file view and anchored published changes |
| `exec` | Container command; workspace mode determines source writability |
| `experiment` | Container command with readonly source and ephemeral work area |
| `command_evidence_read` | Read an exact retained command record from this project's same configured role's run (current or prior); no listing or path input; ungranted by default |
| `experiment_evidence_read` | Read an exact retained experiment wrapper from this project's same configured role's run (current or prior); independent of the command grant; ungranted by default |
| `verify` | Execute operator-owned command strings and bind proof to code digest |
| `fetch`, `search` | Allowlisted retrieval / trusted discovery adapter |
| `probe` | One policy-bounded HTTPS request to `probe_hosts` with an allowlisted method and bounded headers/body; observes status, headers and bounded body with a receipt (no redirects, no credentials) |
| `insights` | Read proposals |
| `work_items_read` | Read an open-item page (optional cursor up to 128 characters; limit 1–100, default 25; rows at most 8192 canonical bytes) or one exact seeded item by ID in any status, including terminal states, with current origin staleness and paged immutable history (`after_rev` exclusive 0–256, default 0; limit 1–100, default 25; chain at most 256 revisions; records at most 128 KiB). Either response is capped at 1 MiB and is a fresh live view, not a frozen snapshot; unsafe or malformed store data fails closed. This read grant does not imply transition authority |
| `work_items_transition` | Append one CAS-bound status revision; actor is fixed to the role, stale revisions require a fresh read, `done` requires retained passing digest-bound snapshot evidence, `dropped` requires a reason, and `blocked` may carry a structured wait. Granted only to the example worker role |
| `work_items_legacy_read` | Read a bounded live event inventory of unresolved/reconciled legacy accept/modify history and explicit no-work dispositions, with origin validity, bounded decision/reconciliation reasons and coverage. Observational; no proposal bodies. Granted only to the example worker; does not imply reconciliation authority |
| `work_items_reconcile` | Exclusively record one immutable hash-bound open/drop resolution without rewriting decisions. Expected action and optional revision must match; runtime actor and nonblank reason required; open needs title and valid source origin/timestamp, drop has no seed. Exact retries return `created: false` and do not count as progress; conflicts fail closed. Non-observational on first write, forbidden to consult/delegation children, no wrap-up exception. Granted only to the example worker |
| `decide` | Record a revision-bound accept/modify/defer/reject and rationale, with optional accept/modify work-item intent; revise after any substantive decision |
| `submit_insight` | Submit proposal with route-derived identity |
| `revise_insight` | Revise one of the role's own proposals under its stable ID (source fixed to the role, bound to the read rev; prior content archived, meaningful change reopens the topic) |
| `withdraw_insight` | Withdraw one of the role's own proposals as author retraction, not rejection (source fixed to the role, bound to the read rev); a current accept/modify blocks withdrawal by default |
| `withdraw_decided` | Supplemental opt-in allowing a role already granted `withdraw_insight` to withdraw over a revision-current accept/modify; requires `withdraw_insight`, has no default grants, and does not affect the direct operator CLI |
| `consult` | Ask named read-only roles through their own profile or selector (default from `consult_role`, fail-closed when unset), no nested consultation |
| `attach_evidence` | Append an attributed evidence entry to any open insight (bound to the read rev, title/body/rev unchanged; at most 16 entries of 8192 chars each; no role holds it by default) |
| `report` | Stage a Markdown document for static artifact publication |
| `sync` | Refresh upstream refs via the trusted VCS adapter (writable workspace only); merges stay as workspace edits followed by verification |
| `vcs_read` | Read CI status, logs, PR comments, external proposals, exact-revision proposal content, and CI-recorded artifact files via the trusted VCS adapter; never publishes (read roles may hold it) |
| `vcs_publish` | Push a branch or open a PR via the trusted VCS adapter behind a recorded human `GO <branch>` approval (writable workspace only; consultation roles cannot hold it) |
| `vcs_retire` | Retire an owned temporary integration branch at an exact expected sha behind the configured `retire_grant` (read workspace suffices: integration authority is the capability plus the grant, never source-write access; consultation roles cannot hold it; never combines with `decide` (ordinary `submit_insight` is allowed but can never mint an operator-channel approval)) |
| `vcs_dispose` | Close or merge an external proposal at exact assessed endpoints behind per-action grants (`close_grant`; `merge_grant` plus human `GO <branch>` for merge); also permits an explicit `close_if_adopted` action with exact proposal/target pins, `close_grant`, and the separate host-adapter `[vcs] close_adopted` gate. Role capability plus both gates are required; a valid `closed: false` adapter receipt is retained without retry/refetch/merge. Read workspace suffices; consultation roles cannot hold it; never combines with `decide`; observation never closes automatically |
| `research_read` | Read this role's own current research state record with its generation (read roles and consultation roles may hold it) |
| `research` | Replace this role's own current research state record under the read generation; stale generations refused, audit in `research-state.json` (consultation roles cannot hold it) |
| `finish` | Seal result; cannot acquire new permissions |

For post-decision withdrawal, add `withdraw_decided` to the same role's TOML `capabilities` array only when intentional; it is supplemental to
`withdraw_insight`, never granted by default, and the operator CLI stays deny-by-default.
Neither `read`, `exec` nor `experiment` implies either retained-evidence grant.
The two grants are independent and expose only their own record kind. Their
scope is fixed to the current project and same configured role (current or prior
runs); there is no cross-role/project setting. A current grant permits reading
that role's retained records regardless of the original run's capability set;
the original command is not reauthorized. See [retained-evidence reads](operations.md#retained-evidence-reads)
for locators, paging, byte bounds, trust and failure behavior.

Consultation and delegation require a live caller execution-slot lease from `Engine.run` (unadmitted calls fail with `Busy` before child inference). They use that held slot without acquiring another; consultation fan-out is bounded by `parallel_consults`, and child contexts cannot lend onward.

A consultation names its answering roles explicitly (`roles` array of at most 8, or single `role`, default from the configured `consult_role`, fail-closed when unset). Each child unit runs the named role as configured — its own fixed `profile` or a bounded selector preview with no routing consumption and no classifier inference — so retry, `on_error`, and fallback apply with no new mechanism and the model is stated once on the role. The `delegate` capability names one-shot implementation children: a role holding it hands a self-contained `task` to a `role` (default from the configured `delegate_role`, fail-closed when unset) that works in the caller's live workspace while the caller waits holding its workspace lock, so exactly one writer touches the tree. The child takes grants, profile or selector, and policy from the delegate role's own configuration, never `decide`, insight-write, vcs-mutating, or `delegate` grants (refused at delegation time); the mechanism ensures a terminal `finish` seal (idempotent when the configured role already carries `finish` for its own top-level runs) that returns its summary to the caller, and verification and publication stay with the caller. The child's normal requests, tools, and daily budget count against the caller's unit; its wall and logical-request deadlines belong to the child's own role/Context, never `min(child,parent)`. Delegated children share only the configured aggregate portion of the wrap-up allowance (`delegate_wrap_up_requests` and `delegate_wrap_up_tools`, each clamped to the spend role's effective `wrap_up_requests`/`wrap_up_tools` total minus one); unused child share is available to the root, while the root always retains at least one request and wrap-up tool for its seal. These child shares aggregate across repeated delegations and do not enlarge the total allowance. Consultation units are independent and unaffected. Nested delegation and consultation stay disabled so no spend escapes. Consultations likewise use their answering roles' own deadlines. An already admitted synchronous child/join drains even if the parent's wall expires: parent transport cleanup waits rather than cancelling the child. Explicit operator stop, pause/unarm/authority revocation, and child-local timeout still cancel their intended scope. After the join, an expired finite parent receives no further inference allowance; neither its wall nor any enclosing query/turn request deadline is renewed. Each delegation records its task, child run, and result beside the run like consultations do. The `consult` capability names the operation (bounded child units on the same snapshot, answers returned unranked); role and profile names are operator configuration — avoid naming a role or profile `consult` when that overload confuses review, since the capability, role, profile, and `consult_role` key are distinct.
Any configured role qualifies as long as it stays read-only (`workspace = "read"`)
without write/execute grants (`exec`, `experiment`, `verify`, `decide`,
`submit_insight`, `revise_insight`, `withdraw_insight`, `withdraw_decided`, `consult`, `report`, `sync`); read-only grants (`files`, `read`,
`diff`, `insights`, `fetch`, `search`) stay operator choice plus the required
`finish`. Anything broader is refused before any model call by the single
shared gate (`runtime.check_consult_role`, also used by `smoke --live`).
How answers are combined is worker judgment under policy, never a mechanism
vote: the runtime records answers without ranking them.

Writable roles require the `verify` capability so `done` can be bound to
acceptance commands; a project with a writable role but no `verify` commands
can still run `continue`/`wait`, while `done` fails loudly at finish time.

Avoid multiple writer roles on one project even though a shared workspace lock
serializes them. The supplied Maintainer belongs to its own candidate project.
Editor is intentionally not an autonomous role table: it is a separate,
human-triggered stock harness connected through a readonly capsule.

## Pi extensions

Select reviewed extension resources in `[[profiles.NAME.resources]]` with
`kind = "extension"`, a local path and SHA-256. Discovery is disabled. Extensions
run as trusted operator code in the adapter process; tool permissions do not
isolate arbitrary extension code. Model-driven Mizu operations always use the
bridge, including codemode nested calls. `finish` has model-only exposure and
structured results; seal rejects later calls regardless of exposure.

## Web

Configures outbound retrieval for `fetch` and `search`:

| Key | Default | Description |
|---|---|---|
| `hosts` | `[]` | HTTPS hostname allowlist; re-checked on every redirect. Plain entries match exactly; a leading-dot entry (e.g. `.python.org`) also covers the base domain and its subdomains on a label boundary; a single `*` entry allows any public HTTPS host (other transport protections still apply). Empty stays closed. Prefer the narrowest entry: a suffix trusts every subdomain beneath it, and `*` trusts the open web |
| `probe_hosts` | `[]` | HTTPS hostname allowlist for `probe` observation with the same exact/leading-dot/`*` entries; empty disables `probe` (method/header/body rules still apply) |
| `probe_methods` | `[]` | Allowlisted `probe` methods (uppercase tokens, e.g. `GET`, `POST`); empty disables `probe` |
| `feeds` | `[]` | Allowed RSS/Atom feed URLs for discovery |
| `search_command` | `[]` | Trusted argv command receiving/returning bounded JSON |
| `diagnostic_bytes` | `2048` | Maximum stderr tail shown on a configured program's nonzero exit (0–8192; `0` disables). The excerpt is labelled `external-untrusted` and is model-visible; it may contain host paths or other private adapter output |
| `destination_command` | `[]` | Trusted argv owning the destination decision per request (`url`, `host`, `role`, `capability` fetch/probe, redirect `hop`, `method`); stdin bounded JSON, stdout must be `{"allow": bool}`. Unset keeps the `hosts`/`probe_hosts` lists; when set the decider decides authoritatively per hop and anything but explicit `true` denies (fail closed). While set, `fetch` bypasses the response cache so every hop is decided live; `probe` never caches. HTTPS-only, port 443, DNS pinning, bounds and receipts stay in mechanism |
| `intranet` | `false` | When true, permits any private unicast IP (`ip.is_private`); multicast, link-local, loopback, unspecified, and transition addresses stay refused |
| `cache_seconds` | `1800` | Response cache TTL: reuse window and expired-entry reclamation age (`retrieved_epoch` vs now; epoch-less entries use mtime for reclamation only). Static-cache hits require the current fetch-policy digest (canonical sorted `hosts`, `intranet`, `max_bytes`, and receipt-schema version); changes to these settings or the receipt schema invalidate reuse and re-run live redirect checks. Legacy, prior-schema or incomplete receipts without the current digest miss. Unused for `fetch` while `destination_command` is set (cache bypassed); still ages already-stored entries for reclamation |
| `timeout_seconds`| `20` | Request timeout |
| `max_bytes` | `524288` | Maximum raw response payload (512 KiB; 1–16,777,216); worker JSON expansion does not enlarge the raw response cap |

The `fetch`/`probe` worker IPC ceiling accounts for JSON expansion and metadata
separately from the unchanged raw response cap: combined stdout/stderr is limited
to `max(2 MiB, 6 * max_bytes + metadata_reserve)`. The fixed metadata reserve is
64 KiB for `fetch`; `probe` adds `6 * 64 * (256 + 8192)` bytes for its bounded
response headers, which coexist with the body. Six covers worst-case JSON control
escaping and UTF-8 replacement decoding. Generated `fetch` Content-Type and
`probe` media metadata are limited to 256 characters without changing fetch media
decisions. Oversized hand-edited cache objects still fail closed at the finite
worker ceiling. `search` retains its existing 2 MiB combined worker output cap;
this is not a universal search aggregate envelope.

Web worker stdin has a separate fixed 3 MiB framing limit. Canonical JSON can
double literal TOML tabs/newlines in the 1 MiB configuration/include budget;
another MiB covers worst-case escaping of a schema-valid 32 KiB probe body, 16
bounded header pairs, and envelope/path overhead. The parent rejects oversized
canonical requests before spawning; the child reads at most the limit plus one
byte and rejects overflow before JSON parsing. This is separate from
`PREVIEW_BYTES`, the raw body/response limits, and the output cap.

A normally exited nonzero `search_command` or `destination_command` may include up to the final `diagnostic_bytes` bytes of stderr in its error, labelled `external-untrusted`; setting the value to `0` disables the excerpt. Timeout, output-limit, spawn, and malformed-protocol failures keep their generic handling. A valid destination response with `allow = false` remains a normal fail-closed denial and does not expose stderr. These diagnostics are model-visible, so inspect the operator adapter's output and use `0` if it can emit private data.

URL fragments are stripped client-side and never sent. Redirects re-validate hostname, port, and IP. Text/HTML/XML/JSON formats are supported; binary/PDF/image formats are rejected.

Transport floor (fixed mechanism, not policy): every `fetch`/`probe`/feed retrieval is HTTPS-only on port 443 with no URL credentials and DNS pinning, even when `destination_command` decides the destination or `hosts` widens to `*`. Which hosts, feeds, adapters, deciders, intranet reach, timeouts, cache windows and byte caps apply is operator policy above; how bytes move (encrypted TLS, standard port, no creds in URLs, pinned DNS, bounded receipts) stays in mechanism so cleartext or non-standard-port retrieval cannot be enabled by configuration. Credentials in URLs stay refused.

`probe` reuses `[web] timeout_seconds`/`max_bytes`/`intranet` with its own `probe_hosts`/`probe_methods` allowlists. It sends one request (caller headers at most 16, names 128, values 4096; credential/framing headers refused) with a bounded body (at most `max_bytes`, 32 KiB tool-input cap), follows no redirects (3xx returns as observation), never caches, and records a `sources/<id>/receipt.json` receipt (`status`, bounded headers, `sha256`, `trust: external-untrusted`). Non-2xx statuses return as observation, never as failure.

### Immutable Web continuation and experiment input

A new `fetch` takes `url`; a new `probe` takes `url` and `method` (and optional
`headers`/`body`); a new `search` takes `query`. Each invokes the existing bounded
worker once, with the existing transport, redirects/feed behavior, media checks,
request/response limits and failure classifications. There is no automatic retry.
Fetch/probe return a 64-lowercase-hex `receipt_id` (also `id`); search returns a
64-lowercase-hex `result_id`. To continue **in the same run**, omit the original
request fields and pass only the ID and optional `offset`/`limit`, for example:

```json
{"receipt_id": "<receipt_id from fetch or probe>", "offset": 8192, "limit": 8192}
{"result_id": "<result_id from search>", "offset": 100, "limit": 100}
```

Continuation reads only immutable run evidence: it does not call the worker,
recheck destinations, consult a cache, send another POST, fetch again or rerun
search. Mixed ID/request fields, missing/malformed IDs, wrong-kind, corrupt and
other-run records fail explicitly, never falling back to a request. Supplying
URL/method again is an explicit **new** probe, not continuation. Text offsets
remain character offsets (limit 1–8192, default 8192); search uses its existing
row paging (limit 1–1000, default 1000) and bounded previews. Results remain
`external-untrusted`. Normal tool cancellation/deadlines still apply; local
continuation uses bounded safe reads with no retry or subprocess.

Fetch/probe records live at `sources/<receipt_id>/receipt.json`; search records
at `sources/search-<result_id>.json`. IDs bind canonical record content, kind and
run scope; replay verifies these with bounded, non-link-following reads. A live
fetch retains the exact response bytes in the receipt's body-only `payload`
directory as `body` with representation `raw-response-body`, under
`[web] max_bytes`, binding byte length and SHA-256. It does not also store normalized
text in the run receipt: continuation decodes UTF-8 with replacement and applies
the same HTML text extraction as the initial response. The existing shared cache
remains text-only; a cache-hit receipt is pageable but has `raw_available: false`
and cannot supply experiment bytes. No raw bytes are reconstructed from text and
there is no global raw-body cache. Probe/search replay their first stored result.

`experiment` accepts optional `source_receipt` naming a same-run **fetch** receipt
with retained raw bytes. The harness verifies receipt identity and the actual
body's length and SHA-256 immediately before launch, then bind-mounts the
body-only payload directory read-only at `/work/input`; the bytes are
`/work/input/body`. Example input:

```json
{"script": "wc -c /work/input/body", "source_receipt": "<receipt_id from live fetch>"}
```

No source path or arbitrary mount is accepted. Missing, corrupt, other-run,
probe/search and raw-unavailable receipts are refused without refetch or
substitution. There is no implicit source execution, archive extraction or network
operation. The experiment's script is still an explicit sandbox command; source
content stays `external-untrusted`. Command evidence records only source receipt
ID, representation, byte length and digest (plus the trust label), not raw
content or host source paths. `/workspace` remains read-only and `/work` disposable; `[sandbox] network`
is unchanged (default `none`). Offline fake-transport and argv tests establish
replay/integrity and constructed mount flags, not live-provider behavior or actual
OCI isolation.

## Offline materialization

Configures the operator-owned `[materialize]` adapter that warms already-mounted read-only caches or returns digest-bound text. It is a trusted argv program on the host; model input supplies only a bounded JSON spec, never argv or mount paths. The adapter's returned content remains `external-untrusted` until applied as a workspace edit and verified.

| Key | Default | Description |
|---|---|---|
| `command` | `[]` | Trusted argv executable; JSON stdin/stdout, never a shell string. Empty disables |
| `timeout_seconds` | `20` | Per-invocation deadline |
| `max_bytes` | `524288` | Maximum request/response payload (512 KiB; 1–16,777,216) |
| `diagnostic_bytes` | `2048` | Maximum stderr tail shown on a configured program's nonzero exit (0–8192; `0` disables); labelled `external-untrusted` and model-visible |

Only a normally exited nonzero adapter includes the bounded stderr excerpt; timeouts, output-limit and malformed protocol responses keep their existing generic errors. Stderr may include host-local paths or private data; set `diagnostic_bytes = 0` if it is not safe to send to the model.

## VCS (upstream sync, M1 step 5: grant-gated `sync` refresh plus daemon periodic fetch)

Configures the operator-owned upstream-sync adapter. `src/mizu/vcs.py`
(`invoke`/`fetch_refs`, plus host-side `inject_refs`/`list_refs`/`read_ref`)
implements the single-invocation contract and read-only ref injection.
`src/mizu/runtime.py` (`Context._op_sync`, `refresh_upstream`,
`upstream_fetch_due`/`poll_upstream`) implements the grant-gated `sync`
refresh and the best-effort daemon periodic fetch.

| Key | Default | Description |
|---|---|---|
| `command` | `[]` | Trusted argv executable; JSON stdin/stdout, never a shell string. Empty disables |
| `timeout_seconds` | `20` | Per-invocation deadline for on-demand tools (1–16,777,216) |
| `max_bytes` | `524288` | Maximum adapter request/response payload, 512 KiB (1–16,777,216) |
| `diagnostic_bytes` | `2048` | Maximum stderr tail shown on a normally exited nonzero adapter call (0–8192; `0` disables); labelled `external-untrusted` and model-visible |
| `ci_branch` | `""` | CI/reporting branch exposed only to `vcs_read` roles and included in the default CI wake set when nonempty |
| `ci_wake_extra_branches` | `[]` | Add up to 64 valid branch names to the CI wake set; deduplicated with `ci_branch`; empty preserves existing default behavior |
| `poll_enabled` | `false` | Explicit opt-in for daemon upstream/CI polling; `false` disables all daemon adapter calls |
| `poll_proposals_enabled` | `false` | Additional opt-in for daemon external-proposal observations; requires `poll_enabled = true` and a configured adapter; does not change CI polling |
| `poll_interval_seconds` | `300` | Daemon poll cadence in seconds (15–86,400) |
| `poll_max_branches` | `4` | CI branches polled per daemon tick (1–64) |
| `poll_fetch_timeout_seconds` | `30` | Daemon upstream-fetch adapter cap in seconds (1–300); each tick uses `min(timeout_seconds, this)` |
| `poll_status_timeout_seconds` | `15` | Daemon per-branch CI status adapter cap in seconds (1–300); each tick uses `min(timeout_seconds, this)` |
| `ci_rewake_seconds` | `1800` | Unchanged-red re-wake interval in seconds (60–86,400); an unchanged red still re-wakes eventually so it can never park a writer forever |

Adapter contract: stdin is one JSON object `{"op": ...}` bounded by
`max_bytes`; stdout must be one JSON object. `fetch` returns
`{"refs": {name: sha}}` (at most 4096 refs; names 1–512 chars without
newlines/NUL, no leading `/` or `..`; shas 40/64 lowercase hex) with
`trust: external-untrusted`. Invocation uses `process.run` with timeout, no
shell, `maximum=max_bytes`. Trust: operator-owned host program only; never
model-provided, never run in the sandbox. Retry/cancellation: single
invocation per call under the configured timeout; no shell retry.
Evidence: `sync` writes `sync.json` (full refs) and returns a bounded
summary (`injected`, `prefix`, `trust`, `upstream` for `main`, sorted `refs`
names); completion still binds to `code_digest` via `verify`. Failure: `ConfigError` on unknown keys, bad argv, or
out-of-range bounds; `Denied` on unconfigured adapter, oversize request,
timeout, oversize response, nonzero exit, or malformed/non-object JSON. A normally exited nonzero command may include the configured final stderr tail in its error; timeouts, output-limit and protocol failures stay generic. The stderr is model-visible and may contain private host data, so set `[vcs].diagnostic_bytes = 0` to suppress it.
Defaults apply when `[vcs]` is absent so existing configs keep loading. `retire_grant` (default `false`) is the explicit operator grant for branch retirement; `owned_prefixes` (default `[]`, e.g. `["mizu/"]`) names the temporary integration namespaces; `protected_refs` (default `[]`, e.g. `["main"]`) names exact long-lived refs that always win over owned prefixes. Mizu `close_grant` (default `false`) is required for routine proposal close and conditional close; the separate operator-owned host adapter `[vcs] close_adopted` gate must also allow conditional close. Mizu does not configure or verify that adapter setting. `merge_grant` (default `false`) is the separate explicit grant for proposal merge; merges additionally require a recorded human `GO <branch>` approval for the integrated tree. No project, bot, or provider names are hardcoded: an empty policy classifies nothing as owned, so nothing retires.

`ci_branch` (default `""`) names the operator-selected CI/reporting branch. It is exposed as `ci_branch` in full and delta prompts, but only to roles holding the `vcs_read` engine tool; every other role sees `null`, and an empty value reads as `null` everywhere. `ci_wake_extra_branches` (default `[]`) adds up to 64 valid branch names to the wake set; the effective set is the deduplicated nonempty `ci_branch` plus these extras. Thus absent/empty extras preserve `[ci_branch]`, while an empty `ci_branch` and no extras still fails closed. Extra names use the VCS branch-name validation rules; malformed entries and arrays over 64 fail configuration load. The same gated prompts carry the unfiltered `ci_failures` projection (`null` without `vcs_read`, otherwise a capped list, empty when green): the last polled red per branch, independent of insight decisions. A parked role with `ci_wake` (writable roles by default) wakes on a selected red only on change -- a new branch/SHA/check/run fact in the whole selected set, a moved published head, or every `ci_rewake_seconds` (default `1800`, range 60-86,400) for an otherwise unchanged red -- so a red belonging to a previous head cannot re-admit a waiting writer every tick while its fix is in flight. The wake record stores a bounded digest of the selected branch-specific facts. Adapters MUST keep run identity stable for the same run across polls (churn would re-wake every poll). Reporters must select CI evidence explicitly and never guess a branch. Unknown keys still fail load; `ci_branch` must be a string no longer than 256 chars and without whitespace.

Host-side `inject_refs` writes validated refs as read-only (`0o444`) files
under the project-local harness store `upstream-refs/*` (a sibling of
`workspace/`, at most 4096 refs; stale entries pruned on refresh; symlink
escapes refused), so harness state never appears in the product working
tree and product tooling (e.g. Git) never sees it. The `files`/`read`
tools serve the store read-only under the virtual prefix
`refs/remotes/upstream/*` (read roles included; `none` sees none);
snapshots capture the workspace only, so refs never affect `code_digest`
by construction. `Snapshots.excluded` still excludes the legacy workspace
subtree of the same virtual path, so not-yet-refreshed workspaces keep
their digest across the migration. The reservation is reported, never
silent: captures record reserved paths in the snapshot record (`reserved`;
`code_digest` still covers files only, so existing digests do not move),
plain-directory import refuses when reserved paths are present, and `read`
of a virtual-ref path that also exists as a workspace file is refused as
a collision. Each refresh also removes the legacy
`workspace/refs/remotes/upstream` subtree when present. Do not keep
project source under that prefix; relocate genuine files out of it.
Content stays `external-untrusted` until merged and verified.

The `sync` tool refreshes that view on demand: it calls `fetch` on the
adapter, then `inject_refs` into the project harness store, and returns the
bounded summary above. It requires the `sync` capability (writable workspace
only; `sync` on a read/`none` role is refused at load for configured roles
and at call time for replaced roles, and consultation roles cannot hold it).
Without the grant the call is refused before any adapter spawn. Merging or
rebasing `upstream/main` stays worker policy: ordinary workspace edits,
then `verify` and publish. No silent auto-merge runs in the mechanism.

Daemon polling is an explicit opt-in (`poll_enabled = true` plus a
configured `command`); by default the daemon never spawns the adapter.
Cadence uses `poll_interval_seconds` (default 300 s). Polling is best-effort
host-side work with no capability check (operator-scheduled, not model
authority): each daemon iteration takes the per-project `vcs-poll.lock`
(non-blocking) and shares `vcs-poll.json` timestamps, so one project polls
once even with a daemon per role; a daemon that misses the lock skips the
tick. Bounds keep one tick short: one fetch capped at
`min(timeout_seconds, poll_fetch_timeout_seconds)`, CI at most
`poll_max_branches` branches capped at
`min(timeout_seconds, poll_status_timeout_seconds)` each (worst case ~90 s
at defaults without proposal polling; one enabled proposal page adds at most
one more status-timeout cap). Proposal polling uses that
same status timeout cap and shared cadence, but is separately opted in with
`poll_proposals_enabled = true`; the global `poll_enabled` and configured
adapter are also required. It fetches at most one normalized page (up to 256
rows) per proposal tick, persists the opaque cursor in `vcs-poll.json`, and
resumes it next tick until `complete = true`; a non-paginated response without
a cursor completes the enumeration. Adapter, invalid-page, recording, and
poll-state persistence failures emit a bounded `proposal_poll` event with
`ok: false`, advance the proposal timestamp where state storage permits, and
keep the prior cursor so a page can be retried.
Successful rows go through `record_proposal_state` (stable IDs, no-op on
identical facts, revise on changed facts). A complete enumeration or a
non-paginated response with no cursor clears the cursor; missing proposal rows
never revise or close a recorded proposal. Proposal observation performs no
automatic close, merge, or branch retirement; existing grants, human GO,
provider atomic checks and writer boundaries remain unchanged. CI polling
continues independently whenever `poll_enabled` is true, regardless of the
proposal flag. Failures become visible `upstream_fetch` / `ci_poll` events
(`ok: false`) and never publish a snapshot (injected refs stay
digest-excluded). Their existing timestamps advance on failure too, so one
bad adapter cannot busy-loop.

## VCS publish (M2 step 1: `vcs_read`/`vcs_publish` behind `GO <branch>` approval)

Same `[vcs]` adapter, with no publish-specific configuration keys.
`vcs_read` serves only `status`/`log`/`comments`/`proposals`/`acquire`/`artifacts`/`artifact`;
`vcs_publish` serves only `push`/`pr`. Cross-path calls are `Denied`
(`vcs_read cannot publish`). Calls are host-side only, bounded by
`timeout_seconds` and `max_bytes`, and adapter observations remain
`external-untrusted`. Run-local `vcs-read/` and `vcs-publish/` evidence paths
are named in tool responses.

The `vcs_publish` tool input remains `{"op", "branch"}`. After checking the
recorded human approval against the captured `code_digest`, Mizu reads the
source commit from the local workspace's Git `HEAD` (not a branch-status
observation). Missing Git, an unborn/unavailable HEAD, or a non-full SHA fails
closed. The source commit is distinct from the approved content digest:
uncommitted content still needs the exact digest approval, and the adapter
must verify the tree it actually publishes.

The adapter request is
`{"op", "branch", "digest", "source_commit", "publication_id"}`. The source
commit is 40 or 64 lowercase hex; `digest` and `publication_id` are 64 lowercase
hex. `publication_id` is the SHA-256 of Mizu's canonical JSON bytes (sorted keys,
compact UTF-8, trailing LF) for
`{"source_commit", "code_digest", "branch", "op"}`. It excludes run and approval
identity, so retries of the same request reuse the same ID. The adapter must
verify the pushed tree matches `digest` before any external mutation, echo
both `digest` and `publication_id`, and return `published_sha`: the exact
published commit, 40 or 64 lowercase hex. Missing, mismatched, abbreviated or
malformed values are refused. A later branch head or CI result never supplies
a missing publication SHA.

Durable project evidence lives outside the workspace at
`evidence/publications/<publication_id>.{intent,dispatch,receipt}.json`.
Records are create-only under a per-ID lock and bounded no-follow reads
(8 KiB each). The schema-1 intent binds `publication_id`, `source_commit`,
`code_digest`, `branch`, `op`, `approval` (human approval insight ID), and `run`
(the originating run ID). Intent persistence must succeed before dispatch.
Immediately before starting the adapter, Mizu persists the usual run-local
unknown-outcome mutation evidence and a create-only dispatch marker containing
`publication_id`. A validated adapter response permits a create-only receipt:
the complete intent plus `published_sha`. This receipt is durable before the
run-local final observation. Successful output exposes these receipt fields,
`published: true`, the project-relative `receipt` path, run-local `evidence`
identity, and `result` (the adapter response on first dispatch; validated
identity/digest/SHA fields on replay).

A receipt replay returns the original receipt identity without invoking the
adapter, while still requiring current human approval/digest/workspace gates
and recording a fresh run-local observation. An existing intent without a
dispatch marker can safely resume with the same ID. Once a dispatch marker
exists without a receipt, all retries refuse with the unresolved publication
ID visible, including timeout, cancellation, invalid response, or receipt-write
failure. This conservative boundary may leave an intent unresolved even if a
crash occurred before the adapter started. There is no automatic retry or
reconciliation operation in this slice; do not delete a marker to retry.
A `vcs_read` `status` call may bind directly to a retained publication with a 64-lowercase-hex `publication_id` instead of `branch`/`sha`. Mizu validates the durable intent, dispatch marker, and receipt, then asks the adapter for that receipt's branch and exact `published_sha`; branch/sha overrides are refused. When `sha` is requested, rows for other commits are filtered out, so an empty result stays missing evidence rather than borrowing a later head. The returned publication identity and adapter response are retained in the invoking run as external-untrusted evidence. This read does not infer a complete check predicate or terminal outcome; structured exact-SHA CI waits remain a later ADR-030 slice.

`status` responses are normalized to `{op, branch, checks: [{check, state, sha, url?, run?}], trust}` (at most 1024 checks; `check` 1-256 chars, `state` 1-64 chars, `sha` 40/64 hex, `url` max 4096 chars, optional `run` 1-128 chars without newlines/NUL as the opaque adapter run identity, stable per CI run across polls; omit `run` when the adapter cannot report it, in which case wake stays sha-keyed); malformed shapes are `Denied`. `log`/`comments` pass through with `trust: external-untrusted`. `artifacts` responses are normalized to `{op, branch, sha?, artifacts: [{artifact, size?, digest?}], trust}` (at most 256 rows; `artifact` a relative Unix path 1-512 chars with no empty/`.`/`..` components, no leading slash, no backslashes; `size` an int >= 0 when present; `digest` 64 hex when present); `artifact` responses are normalized to `{op, branch, sha?, artifact, digest, content, trust}` (text at most 524288 chars, `digest` recomputed locally as sha256 over `fs.canonical(content)` like `acquire`; a name or digest mismatch is `Denied`). Results stay `external-untrusted`; an unconfigured adapter fails closed as today, and any role holding `vcs_read` with a visible workspace may use both ops. `proposals` responses are normalized to `{op, branch?, proposals: [{id, state, head, base, draft, mergeable?, checks?, reviews?, url?}], complete?, cursor?, trust}` (at most 256 proposals; `id` 1-128 chars, `state` `open`/`closed`/`merged`, endpoints live `{repo, ref, sha}` or explicit tombstones `{deleted: true, repo, ref}` for removed forks, `draft` bool, `mergeable` bool or unknown, CI check rows reused with each row keeping its own tested sha (rollup checks may run on a merge commit rather than the head sha, so the adapter omits rather than misattaches them), `reviews` at most 64 `{reviewer, verdict, sha}` rows (`approved`/`changes_requested`/`dismissed`, each bound to the exact revision reviewed; absent means unknown, never none), `url` max 4096 chars). The envelope may carry `complete` (bool) and an opaque `cursor` (max 256 chars) for truncated enumerations: callers resume with the cursor, observation flows on partial evidence, and safe mutation stays blocked while required evidence is unavailable. A missing or truncated row never implies terminal disposition; only a reported `state` does. Proposal observations are recorded as one deduplicated insight per proposal (`proposal-<32 hex>` from the proposal id): identical facts are a read-only no-op with no rev bump, while changed head/base/check/state facts revise the record (`record_proposal_state` returns `changed`). The body carries facts only, never per-run log URLs. Check details use a fixed 16-KiB UTF-8 budget; oversized sets retain a failure-first bounded preview plus a deterministic SHA-256 over all normalized check name/state/SHA facts, so omitted rows still trigger revisions and can be queried through `vcs_read proposals`. Missing rows in even a complete enumeration never imply closed, merged, or any other terminal state; only an explicit validated row can revise an existing record.

The daemon polls CI only when `poll_enabled` is true, on the same `poll_interval_seconds` cadence under the same per-project lock and shared timestamps: each tick derives branches from injected refs (all when they fit `poll_max_branches`, else a rotating window tracked in poll state so every branch is covered; `main` when no refs exist), calls `status` once per branch with the `poll_status_timeout_seconds` per-call cap, and records only `failure` states as deduplicated insights (successes create no insight). Each successful tick refreshes the shared `vcs-poll.json` `failures` map and bounded `successes` evidence map (each capped at 128 branches x 16 checks): polled failure branches are replaced wholesale, polled success evidence is retained for cross-poll comparison, unpolled branches are kept, and refs that vanished are pruned. Green/superseding failure facts therefore update independently of insight decisions. A projected failure is annotated `nondeterministic: true` with one validated `passed_on` branch only if the retained success cache contains the same SHA/check; this is informational, does not clear the failure or change wake/gate/retry behavior, and absence of an annotation is not proof of determinism because evidence is bounded. `should_run` and dispatch admission consult cached failures (best-effort local read, never an adapter call), wake a role with `ci_wake` while a red remains on the configured wake set (`ci_branch` plus extras), and expose the unfiltered per-branch failure projection as `ci_failures` to `vcs_read` roles with admission `ci` in `started.json`. Adapter errors leave cached facts untouched. Explicit branch lists longer than `poll_max_branches` are refused. Failures become visible `ci_poll` events (`ok: false`) and never publish a snapshot; `last_poll` advances on failure too so one bad adapter cannot busy-loop.

External publication requires recorded human approval (AGENTS.md: "no external publication without recorded human approval"): an insight titled exactly `GO <branch>` with source `operator` (`mizu insight submit`) whose body carries a `digest: <code_digest>` line for the exact workspace `code_digest` at call time, with an `accept` decision recorded via `mizu insight decide` (run `operator`) on that insight ID. Model-submitted insights and model `decide` records are refused as forged (`operator channel`). Missing, stale, undecided, or non-accept records are `Denied` (stale digests name the staleness). `vcs_publish` requires a writable workspace, is forbidden on consultation roles, and must not share a role with `decide` (config load refuses the combination; use a dedicated publisher role). A publisher may hold `submit_insight` for ordinary reports: only operator-channel `GO` records satisfy the approval gate. Role names `operator`, `vcs`, and `editor` are refused at load so model roles cannot mint those sources. CI failures become deduplicated insights via stable IDs (`ci-<32 hex>` from branch+sha+check); repeats return the existing record (the log `url` is validated but not stored, so varying per-run URLs still dedupe; latest URL via `vcs_read`). Passes create no insights; retained bounded success evidence can annotate a matching red check as nondeterministic.

## VCS acquire (exact-revision external content)

`vcs_read` `acquire` materializes proposal content for assessment at an explicit scope: it sends `{"op": "acquire", "id", "sha", "scope"}` where `sha` is the exact head sha from the proposal record and `scope` is `"head"` (the head commit only) or `"full"` (the base revision through the head: every proposal commit), plus an optional `base_sha` pin with `"full"` taken from the observed base endpoint. The adapter returns `{"id", "sha", "scope", "digest", "content"}` plus `"base_sha"` exactly for `"full"` scope. `head` content is immutable for the sha but omits earlier commits, so it must never be treated as the whole proposal; review and integration acquire `"full"` so the receipt binds both revisions. The product checks the `id`/`sha`/`scope` echo against the request (a pinned `base_sha` must echo back unchanged; an unpinned `"full"` binds whatever base the adapter served), and recomputes `digest` locally as sha256 over `fs.canonical(content)` instead of trusting the echo. A moved head, a moved base, a scope mismatch, or a digest mismatch is `Denied`: the caller re-observes `proposals` and retries with the fresh revision pair, so a retargeted base with an unchanged head acquires fresh content under a fresh digest rather than passing as old evidence. No branch is needed: the endpoint travels in the proposal record, so fork heads resolve by id and sha alone. Provider fetching lives in the operator adapter, which labels the immutable scope it actually serves; the generic request, validation, receipt, and retry discipline live in the product. Results stay `external-untrusted` with per-invocation `vcs-read/` evidence files named in the response `evidence` path. `acquire` mutates nothing and needs no grant; final disposition and retirement stay separate operations.

## VCS retire (branch lifecycle: classify, then retire owned only)

`classify_branch` sorts every ref into `owned` (temporary integration branches under configured `owned_prefixes`), `protected` (exact `protected_refs`, always wins), `external` (pull/fork namespaces outside local heads), `tracking` (`refs/remotes/*`), or `other` (active work of unknown ownership). Prefixes match on component boundaries (`mizu` owns `mizu/x` but not `mizu-x`). `vcs_retire` serves only `{"branch", "expected_sha"}`: it refuses without `retire_grant = true`, refuses every non-owned class (protected/external/tracking/other are preserved, and a failed call implies nothing about the remote ref), refuses early while a recorded proposal observation still openly references the branch at head or base (naming the blocking proposal ids), then sends `{"op": "retire", "branch", "expected_sha"}`. The adapter must delete only when its current head still equals `expected_sha` AND no live (non-terminal) proposal still references the branch — one atomic provider step, never a separate observe-then-delete round trip — and echo `branch`/`sha` with `deleted: true` plus `live_refs` naming the blocking proposal ids it checked (empty on success), else the call is `Denied`. Recorded observations may be stale, so the provider affirmation is the fresh authoritative check; a provider that cannot enforce the atomic check must answer `deleted: false` instead of pretending, and the limitation surfaces as a refusal, never as an unchecked delete. Tombstoned (deleted) endpoints reference nothing live, and CI records never block. The lifecycle order is integrate, then `GO`-approve, then `vcs_dispose`, then re-observe `proposals` until the terminal state is recorded, and only then `vcs_retire`; a closed-unmerged proposal is not proof its commits are disposable. Evidence lands in a per-invocation `vcs-retire/<uuid>.json` file named in the response `evidence` path, with the classification.

## VCS dispose (final disposition after approved integration)

`vcs_dispose` serves `close`/`merge` (`{"op", "id", "sha", "base", "target", "branch"}`): `branch` names the integration branch holding the approved tree, `id` names the proposal, `sha` is the exact assessed head, `base` the exact assessed base, and `target` the assessed target branch. Close is routine terminal reconciliation behind `close_grant = true` alone. Merge promotes the tree behind `merge_grant = true` plus a recorded human `GO <branch>` approval for the current `code_digest` (checked before the adapter spawns), so main/release promotion without a matching approval is refused. The merge approval must additionally bind the assessed destination and proposal endpoints with body lines `target: <branch>`, `proposal: <id>`, `sha: <head>`, and `base: <base>` naming exactly the merge arguments: a digest-only approval, or one bound to another target or other proposal content, never authorizes the merge (a moved head, moved base, or retarget refuses until the operator re-observes and re-approves). Either op then sends `{"op", "id", "sha", "base", "target"}`. The adapter must act only while the proposal is still open at all three assessed endpoints (compare-and-dispose immediately before action: a moved head, a moved base, a retargeted proposal, or an externally superseded proposal refuses) and echo `id`/`sha`/`base`/`target` with the terminal state (`close` ends `closed`, `merge` ends `merged`), else the call is `Denied`. Tree-content approval stays local to the `GO` gate: the adapter contract binds endpoint identity only, never pretended content atomicity. Disposition is explicit per action: no automatic merges, no bot rules, and the main promotion flow is untouched. A failed call implies nothing about the remote proposal; reconcile by re-observing `proposals`, which revises the recorded observation on state change. Evidence lands in a per-invocation `vcs-dispose/<uuid>.json` file named in the response `evidence` path (with the approval for merges). Safe mutation stays blocked when required evidence is unavailable: tombstoned heads carry no sha to dispose, and an incomplete enumeration never implies terminal disposition.

`close_if_adopted` is an additional explicit `vcs_dispose` action, not polling and not an automatic response to observing a proposal or insight. The role needs the `vcs_dispose` capability and both independent configuration gates must allow it: Mizu `[vcs].close_grant = true` (default false) and the operator-owned adapter's `[vcs] close_adopted = true`. The adapter gate and its behavior are operator-reported; Mizu neither edits nor independently verifies the adapter configuration. The request carries exactly `{id, head_sha, base_sha, target_repo, target_ref, target_sha}` from the operator-reported observation. A usable response echoes every pin unchanged, names predicate `changed-path-tree-entries/v1`, and has strict boolean `closed` plus a nonblank `reason` of at most 4,096 characters. No other keys are allowed except the optional paired `mismatched_paths`/`mismatched_count`: the count must be an integer (not a boolean) from 0 through 2,000, and the list may contain 0 through `min(mismatched_count, 50)` distinct nonempty paths, each at most 4,096 characters. The list can therefore be partial or empty, and a count larger than 50 permits up to 50 paths. Reason and paths must not contain terminal controls. A `closed: true` response cannot report mismatches. The full adapter response is retained in a per-invocation `vcs-dispose/<uuid>.json` file named in the response `evidence` path, and returned. Mizu permits at most one validated conditional-close adapter dispatch per work unit; later conditional-close attempts are refused before dispatch, so they cannot replace the first receipt or retry an uncertain result. Invalid local pins/grants do not consume this one-shot dispatch. A well-formed `closed: false` is a refusal receipt, not success and not a trigger for refetch, retry, or merge. Malformed or stale echoes fail closed. The predicate and adoption decision are adapter claims: Mizu validates their shape and pin binding, not whether the provider's changed-path tree comparison is correct or whether the tree is a sufficient review. This is distinct from routine `close` and merge/GO behavior, which remain unchanged.

## VCS proposal lifecycle (observe, integrate, dispose, retire)

The stages share one contract shape. Observation (`vcs_read` `proposals`/`acquire`) is read-only and revision-bound: enumerate with cursor resume, assess exact heads by `(id, sha, scope)` with digest-bound content bound to the base revision, and record observations that revise on change. Integration is workspace edits plus verification under the untouched main promotion flow. Disposition (`vcs_dispose`) resolves one proposal behind the matching per-action grant: `close` uses `close_grant`; `merge` uses `merge_grant` plus a `GO <branch>` approval; `close_if_adopted` uses `close_grant` plus the adapter's independent `[vcs] close_adopted` gate and exact proposal/target pins, returning even a valid refusal receipt. Routine close/merge act only while the proposal remains at assessed endpoints. No observation automatically closes a proposal. Retirement (`vcs_retire`) deletes one owned branch behind `retire_grant` at an exact sha, and refuses while a recorded proposal still openly references it.

Evidence is scoped per mutation: `vcs-publish/<uuid>.json`,
`vcs-dispose/<uuid>.json`, and `vcs-retire/<uuid>.json` under the run directory.
After local capability/workspace, approval, grant, branch/pin, snapshot and
adapter-request checks pass, Mizu durably writes an intent immediately before
adapter dispatch. Failure to persist it prevents dispatch; a local refusal leaves
no intent. The initial record has `evidence_id`, relative `evidence` path,
`outcome: "unknown"`, and `intent: {op, parameters}`. Parameters are bounded tool
pins only: publish branch/code digest/approval identity; retire branch/expected
sha; dispose proposal/target/integration-branch pins and, for merge, code digest
and approval identity; conditional close's six exact observed pins. No adapter
argv, credentials, approval body or unbounded external text is copied into intent.
The configured per-action grants are checked, not inferred from intent.

A validated observation atomically updates the **same** file and evidence identity,
retains `intent` unchanged, sets `outcome: "observed"`, and adds the existing
response fields (`result` or `adapter_receipt`, trust, outcome/approval fields).
The file equals the returned response. A well-formed `close_if_adopted` refusal
(`closed: false`) is an observed refusal, not success. Adapter exceptions,
cancellation, malformed/stale echoes, interrupted calls, or a failed final evidence
write leave outcome-unknown intent, not a successful mutation receipt. Atomic
writes preserve the prior intent on failure; if a failure follows replacement,
Mizu attempts to restore the unknown intent and surfaces the storage failure.
As with any evidence, continuing storage failure can prevent durable repair.
Unknown means the remote outcome is unknown, not that dispatch or mutation did
or did not happen. Intent is never deleted, automatically retried, or reconciled.
Conditional close reserves its one attempt at the validated pre-dispatch boundary,
then persists intent; even an intent-write failure does not release that reservation.
Read and materialize evidence are unchanged and do not gain mutation intent.

For existing `close`/`merge`/`retire` recovery, re-observe after a failed mutation before any fresh attempt. `close_if_adopted` is deliberately different: no refetch or retry is initiated, and a `closed: false` receipt does not invite merge. A malformed adapter echo leaves the remote state unknown; only a fresh observation or a matching receipt supports a conclusion.

## Materialize (offline-input adapter: generic host-side warming, no ecosystem logic)

Configures the operator-owned offline-input adapter. `src/mizu/materialize.py`
(`invoke`, `parse_output`, `load_source`) implements the adapter contract and
the retained-receipt validation; `src/mizu/fs.py` (`safe_write`) implements
the atomic relative writer; `src/mizu/runtime.py` (`Context._op_materialize`)
implements the grant-gated `materialize` tool.

| Key | Default | Description |
|---|---|---|
| `command` | `[]` | Trusted argv executable; JSON stdin/stdout, never a shell string. Empty disables |
| `timeout_seconds` | `20` | Per-invocation deadline (1-16,777,216) |
| `max_bytes` | `524288` | Maximum adapter request/response payload, 512 KiB (1-16,777,216) |

Adapter contract: stdin is `{"spec": "..."}` (nonempty declarative description,
at most 8192 chars, opaque to the product); stdout must be
`{"content": str, "digest": 64 hex, "cache"?: str, "output"?: str}` with
`digest` equal to sha256 over `fs.canonical(content)` recomputed locally
(a mismatch is `Denied`). Content must be valid UTF-8 and at most
`max_bytes` encoded bytes. `cache` (at most 256 chars, no NUL/newlines)
names the warmed location for audit only. `output` is the adapter
policy-checked exact workspace-relative destination (at most 4096 chars, no
NUL/newlines), authorized by the host adapter against its own ops.toml
`[materialize] output_allowed` policy; it is preserved verbatim in the
invoke receipt. Cache-only/older responses may omit `output` and still
invoke successfully, but apply then fails closed. Invocation uses
`process.run` with timeout, no shell, `maximum=max_bytes`.
Trust: operator-owned host program only; never model-provided, never run in the
sandbox. Results stay `external-untrusted` until applied as ordinary workspace
edits and verified. Retry/cancellation: single invocation per call under the
configured timeout; no shell retry. Evidence: per-invocation `materialize/<uuid>.json` receipt named in the response `evidence` path, plus
the returned digest; new invoke receipts also bind `run`/`role`/`kind`. Failure: `Denied` on unconfigured adapter, bad spec,
timeout, oversize response, nonzero exit, malformed JSON, or digest mismatch.
Defaults apply when `[materialize]` is absent so existing configs keep loading.

Apply mode shares the same `materialize` grant (no new capability): the tool
accepts exactly one of `{spec}` or `{apply: {run, evidence_id}}`, validated
exclusively at runtime. Apply is writable-workspace only and takes no
caller path or content: the destination comes only from the retained
receipt, and the bytes only from the retained digest-bound `result.content`
(exact UTF-8 bytes). The retained adapter `result` must carry a
policy-checked `output` that equals the top-level string `output` in the
same retained JSON spec exactly; absent, empty, mismatched, or path-unsafe
outputs fail closed, including cache-only/older receipts with no adapter
`output`. The spec is otherwise opaque; malformed JSON, duplicate keys,
non-finite numbers, or a missing/non-string output is `Denied`. Started
records, receipts and specs all reject duplicate keys and non-finite numbers
(including numeric overflow). Validation reads
`runs/<run>/started.json` (capped at 64 KiB) and
`runs/<run>/materialize/<uuid>.json` (capped at `file_bytes`) with the
existing safe bounded reads under the current project, requires the same
project and role, the exact evidence id/path/kind/trust, and a digest that
recomputes over `fs.canonical(content)` and matches both top-level and
result digests. Old receipts without top-level `run`/`role` stay valid
through their `started.json` binding, but old receipts without an adapter
`result.output` are refused. The output path must equal the retained
spec/result `output` exactly, must pass the existing relative-path safety
(no absolute paths, traversal, or backslashes), must not be excluded, must
already have its parent directory (no parent creation), and is written
atomically via `fs.safe_write` (POSIX `openat`/`O_NOFOLLOW` pinning,
portable resolve-and-check fallback): symlinked parents/targets, hardlinks,
and special files are refused, and failed validation never partially replaces
the destination. The apply response is a small summary plus a retained
`materialize/<uuid>.json` audit record (`kind: materialize-apply` with source
run/evidence, path, digest, bytes, trust), never the content. Apply never
verifies, commits, or publishes; normal verification and publication gates
still decide.

First use: the adapter warms host caches already mounted read-only via static
`[sandbox] mounts` (e.g. a cargo/npm cache directory) and/or returns
digest-bound text content (e.g. an updated lockfile). The product never mounts
arbitrary adapter paths and never grants network to the sandbox; dynamic
per-request mounts and image switching are explicitly out of scope. Ecosystem
logic (cargo/npm/rustup, registry allowlists, layer builds) and decisions
(grants per role, which caches, size/time limits, auto-apply vs approval,
retention) belong in operator configuration and the concrete adapter, not the
product. The `materialize` capability grants the tool; consultation roles
cannot hold it; `none`-workspace roles are refused at call time.

## Role research state (bounded current coverage, replacement only)

Each role may hold one current research record under `role-state/<role>.json`: investigated questions with conclusions, consequential unknowns, coverage/evidence references, and explicit revisit conditions. The store is a bounded generic JSON object (nesting at most 4, keys at most 64 chars, total within `role_state_bytes`); section choice is role policy, never a hardcoded topic list. Updates replace the whole record under a generation compare-and-swap: first write expects generation 0, each replacement expects the generation its writer read, and stale expectations are refused so newer evidence is never overwritten. Read-compare-write runs under a per-role non-blocking lock, so overlapping writers never share a generation: the loser is refused and re-reads. The bound covers the full stored record (state plus envelope and authoring run token); non-finite numbers, non-JSON values, bad run tokens, and boolean generations are refused before the store is touched. Reads distinguish `absent` (never written, generation 0) from `unavailable` (unreadable or malformed, no generation): absence never reads as "nothing researched", and unavailable records refuse replacement until an operator clears the file, while atomic writes keep interrupted updates from disturbing the previous record. History lives only in run records; insight submission is never a research-memory store. Retention follows run records: current state persists until replaced, audit evidence under the existing log-retention windows. Retrieval is capability-gated: `research_read` serves the caller's own record with its generation (consultations may read), while `research` replaces the own record from a JSON `state` at the read `expected_generation` and writes `research-state.json` with the new generation and stored state as run-record audit (consultations cannot hold it, and malformed JSON is refused before the store is touched). Prompts inject the own record for capable roles in both full and resumed deltas: whole while within `role_state_prompt_bytes`, else `truncated` with generation and sizes and no partial content. Retention is configuration without a new knob: the current record is never a prune candidate and persists until replaced; run-record audit (`research-state.json` receipts) persists with the run like other receipts, and only bulky engine logs age out under the event-log windows.

## Project metadata

`projects/NAME/PROJECT.md` is the human goal. `project.toml` has `roles`
and repeatable `verify` command strings. These are outside the workspace and
are not exposed as writable model tools. Alter them as operator while paused.
Verification command exit status alone does not prove semantic correctness,
particularly when the repository's tests are editable. Use independent review
and external acceptance fixtures for critical requirements.

Thinking and effort values belong to each engine's native options. Codex custom
providers use native model_providers; Claude provider endpoints use explicit SDK
configuration. No common provider or effort allowlist is imposed. Consultation
and smoke never reuse persistent sessions.
