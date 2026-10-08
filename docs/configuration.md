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

## Root keys

| Key | Meaning |
|---|---|
| `data_dir` | Private local filesystem state; restart before moving it |
| `engines` | Named Pi/Codex/Claude environments: trusted `command` argv and private `directory` |
| `timezone` | IANA zone for Linux calendar timers and the request-budget day; default UTC. macOS uses explicit local; Windows supports local and UTC (see completion-contracts.md) |
| `consult_profiles` | Permitted independent consultation profiles |
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
| `requests_per_run`, `tools_per_run` | Per-work-unit request and tool bounds; child consultations consume shared daily budget |
| `run_seconds`, `command_seconds` | Deadlines for the overall work unit and individual container commands |
| `idle_seconds`, `cooldown_seconds` | Daemon sleep between poll iterations and pause between work units |
| `default_wait_seconds`, `maximum_wait_seconds` | Default and maximum model-selected wait times (1–86,400s) |
| `max_failures` | Consecutive role failures before auto-pause (0 disables auto-pause; `Busy`/`InfraExceeded` infra waits (daily budget, disk reserve) defer without counting, plain `LimitExceeded` bound faults still count, `mizu resume` resets to 0 — ADR-016, ADR-017) |
| `parallel_runs`, `parallel_consults` | Global execution slots and per-consultation fan-out (`parallel_consults <= parallel_runs`) |
| `output_bytes` | Maximum captured stdout/stderr per command |
| `file_bytes` | Maximum readable file size |
| `snapshot_bytes`, `snapshot_files` | Total visible snapshot size and file-count bounds |
| `history_index` | Maximum published snapshot IDs retained in the immutable history generation (8–1,000,000; manifests stay on disk) |
| `prompt_snapshots` | Number of recent anchored snapshot summaries offered to prompts (1–64) |
| `role_state_bytes` | Maximum canonical JSON bytes of one role-owned current research state record (1,024–65,536; default 8,192) |
| `role_state_prompt_bytes` | Maximum bytes of a role's own research state injected into its prompt (1–65,536; default 4,096); over-bound records inject status/generation/sizes only |
| `retention_days` | Retention window (days) for daily budget files and decided proposals (0 disables; 0–3,650). Reaping reports `reaped`. Snapshots, runs, decisions, and evidence are never reaped |
| `event_log_compress_days` | Compress bulky per-run engine logs (`*-events.jsonl`, `diagnostics.txt`) at or beyond N days old to `.gz` (0 disables; 0–3,650; default 7) |
| `event_log_retention_days` | Drop bulky per-run engine logs (raw and `.gz`) at or beyond M days old (0 disables; 0–3,650; default 31). Result/error/consultation/started/selection/usage records, snapshots, objects, sessions, decisions, and proposals are never candidates |
| `event_stream_bytes` | Per-run engine event-stream retention in bytes (1 MiB–256 MiB; default 4 MiB). Bytes past the budget are dropped from `*-events.jsonl` with an explicit `*-events-truncated.json` marker while parsing and the run continue, so diagnostic volume can never discard sealed work. The per-record transport bound stays fixed mechanism |
| `pending_insights` | Newest pending proposals offered in prompts (1–1,000; default 30). `mizu insight list` uses 1,000 |
| `session_max_tokens` | Cumulative session tokens at which a resumed persistent session restarts with a fresh session key (0 disables; 0–1,073,741,824; default 0). The published snapshot and composed policy reload on the next unit; Pi accumulates per-run usage into the session record, Codex/Claude reuse their saved cumulative totals, unknown shapes count 0 (age still rotates) |
| `session_max_cost_usd` | Estimated session cost (USD) at which a resumed persistent session restarts (0 disables; 0–1,000,000; default 0). Cost is the native provider estimate where reported (ADR-005); sessions without cost evidence never rotate on this bound |
| `session_max_age_seconds` | Wall-clock session age (file mtime versus now) at which a resumed persistent session restarts (0 disables; 0–31,536,000s; default 0) |
| `free_disk_mb` | Minimum free disk space required to start a work unit |

All limits are positive integers except `daily_requests`, `shared_daily_requests`, `max_failures`, `free_disk_mb`, `retention_days`, `event_log_compress_days`, `event_log_retention_days`, `session_max_tokens`, and `session_max_age_seconds`, which may be zero. `session_max_cost_usd` is a number (integer or float) which may be zero.

Run-evidence retention keeps growth bounded: `mizu prune` (paused project, dry-run by default, `--apply` writes a `maintenance/prune-*.json` audit) gzips raw engine logs older than `event_log_compress_days`, drops raw/`.gz` logs older than `event_log_retention_days`, and drops web-cache entries expired past `[web] cache_seconds` (`retrieved_epoch` age; entries without a usable epoch fall back to mtime). Age is file mtime versus now unless noted; the `.gz` keeps the raw mtime so the drop clock does not restart. Only top-level `runs/*/` `*-events.jsonl`, `diagnostics.txt`, and `*-events-truncated.json` markers (plus their `.gz`) and expired `data_dir/web-cache/*.json` entries are candidates — `result.json`, `error.json`, `consultation.json`, `started.json`, `selection.json`, admission/usage records, snapshots, objects, sessions, decisions, and proposals are always kept, so `mizu usage` and restores keep working. Sessions are durable audit evidence: `mizu storage` reports their counts/bytes but no command reclaims them. The web cache is shared under `data_dir`; the invoking project's `cache_seconds` decides expiry and a concurrent fetch recreates a removed entry, so `prune --apply` is safe to retry. Snapshots reference workspace objects only, never run logs, so compressed/dropped logs are disposable projections absent from later backups by construction.
`daily_requests` counts logical admissions, not dollar amounts; configure spending limits with your provider. Use `mizu status` and `mizu budget` for per-project and shared day counts in the configured `timezone`, and `mizu usage` for token statistics.
Day-files (`data/budget/YYYY-MM-DD.json`) keep the date in the configured `timezone` (UTC when unset) plus the shared aggregate, a per-project map, and the `zone` that wrote them, so two configs sharing `data_dir` with different `daily_requests` admit independently unless the optional shared total is reached. `mizu budget <project>` and `mizu status` report both numbers with the day and zone. Projects sharing one `data_dir` should share one `timezone`: different zones name different days for the same instant, splitting the shared total across day-files.

## Fixed mechanism bounds

Safety-critical bounds are fixed in code rather than exposed as configuration knobs:

| Category | Bound | Value | Purpose |
|---|---|---|---|
| **Framing & Preview** | `PREVIEW_BYTES` (`fs.py`) | 128 KiB | File read clamp, diff preview cutoff, and CLI stdin framing |
| | `MAX_FRAME` (`fs.py`) | 1 MiB | Maximum JSON-RPC message size across bridge and MCP servers |
| **Tool inputs** | `exec` / `experiment` script | 64 KiB | Sandbox command size (`SCRIPT_MAX`) |
| | `submit_insight` body | 60 KiB | Proposal prose size |
| | `report` body | 48 KiB | Markdown artifact size |
| | `finish` summary / state | 12 KiB / 32 KiB | Work-unit completion summary and carried state |
| | `consult` question / profiles | 8 KiB / max 8 | Consultation query and bounded fan-out |
| | `search` query / path | 1,000 / 4,096 chars | Retrieval query and file path limits |
| **Sandbox** | Process termination | 1s TERM, 5s KILL | Clean process-group termination without orphan leaks |
| | `MAX_VERIFY_COMMANDS` | 32 | Prevents unbounded verify fan-out |
| **Web broker** | URL / redirects / media | 4,096 chars / max 3 / text, JSON, XML | Strict URL length, redirect limit, and safe media types |
| **Restore** | `--max-bytes` default | 1 GiB | Archive bomb defense; override explicitly if needed |
| **Smoke probe** | `SMOKE_REQUESTS_PER_RUN` / `TOOLS` / `SECONDS` (`smoke.py`) | 4 / 5 / 120 s | Paid `smoke --live` spend cap with margin for one benign listing plus one auxiliary request; applied as `min(fixed, operator limits)` |
| **Config include** | `MAX_INCLUDE_DEPTH` / `MAX_INCLUDE_FILES` / `MAX_INCLUDE_BYTES` (`config.py`) | 8 / 32 / 1 MiB | Shared-fragment recursion, file-count, and total-byte bounds; per-file entries max 32, entry length max 4096 |
| **Locks** | `WINDOWS_LOCK_TIMEOUT` (`platform.py`) / `_LOCAL_BLOCKING_TIMEOUT` + per-path guard (`fs.py`) | 30 s | Liveness bound: contended blocking acquires fail as Busy instead of hanging the daemon thread; intra-process guard has no plausible operator tuning (ADR-026) |

`on_change` is orthogonal to scheduling: it skips execution when the published `code_digest` is unchanged, so state-only republications do not reschedule it. An unacknowledged decision event routed to the role (`decision_events`) or a due structured wait owned by the role is a distinct admission reason that unchanged-code suppression must not swallow. Event delivery reaches scheduling two ways: the writer daemon re-checks `should_run` against the event and wait stores every idle tick, and `mizu service` additionally renders file triggers for scheduled roles with `decision_events` (systemd path units on Linux, `WatchPaths` on macOS) that invoke the same `mizu run` before the next interval; the timers keep their own schedules, and a trigger with nothing due reports unchanged without consuming anything. A role with `on_change` alone and no `daemon`/`interval_seconds`/`calendar` runs only via explicit `mizu run`.

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
| `env` | `{}` | Extra environment variables; runtime vars (`HOME`, `TMPDIR`, `PATH`) and tokens matching `(^|_)(KEY|SECRET|TOKEN)(_|$)` rejected (`HF_TOKEN`, `PUBLIC_KEY_PATH` rejected; `MONKEY_PATH`, `TOKENIZERS_*` allowed) |
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

Optional [dynamic model selection](model-selection.md) uses named TOML selectors,
explicit task attributes, rule/inference classification and operator observations.
Only roles with `selector` enable it; fixed profiles keep their existing behavior.


A profile explicitly selects `engine`, `provider`, `model`, and `session`
(`ephemeral` or `persistent`). IDs remain exact. Native `options` belong to
that engine: Pi `thinkingLevel`, `settings`, `codemode`, `toolSearch`,
`excludeTools`, `scopedModels`; pi-durable `thinkingLevel` plus
`durable_backend` (`sqlite`), `durable_resume` (`compatible`|`fresh`),
`durable_retention_days` (0-3650), `durable_max_turns` (1-1024). The turn
budget is shared per run: persisted turns already spent leave the remainder
for the launcher, which aborts the Harness submission past it (`turn_bound`
maps to a bound error, never a silent stop). Pi SDK knobs
are refused on pi-durable profiles and durable policy is refused on pi
profiles instead of silently ignored. pi-durable resolves the exact model
over built-in providers only; custom provider setups stay on pi. Codex native configuration keys plus `options.turn` for native turn fields
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
one as unauthenticated loopback alone. No
installation, login, package acquisition or deployment runs during work.

A role has exactly one of `profile` or `selector`, plus `policy`, `workspace` (`write`, `read`, `none`),
`capabilities`, `engine_tools` (maximum 128 explicit names), `on_change`,
`decision_events` (at most 8 unique lowercase decision actions, never
`withdraw`), and at most one of `daemon`, `interval_seconds`, `calendar`. `policy` is a path
string or an array of path strings (e.g. shared principles plus the role file);
parts are read as UTF-8 in order and joined with one `\n` (trailing newline
kept), so the composed text is the prompt and the policy digest. Missing,
unreadable, non-UTF-8, or oversize parts fail load; unknown keys still fail.
Bounds (fixed mechanism): at most 16 parts, each at most 64 KiB, 256 KiB total. Capabilities grant
Mizu operations; `engine_tools` grants native or external MCP tools such as
`codemode`, `web_search`, or `mcp__docs__search`. Exposure and automatic approval
never grant execution authority. Native host command/file tools cannot bypass
the bridge and publication contract.

| Engine | Managed interface | Completion | Admission unit |
|---|---|---|---|
| Pi | `createAgentSession`, `ModelRuntime`, `runRpcMode` | `agent_settled` and seal | Logical model request (includes auxiliary inference) |
| Pi-durable | Pinned pi-durable Harness over file-backed SQLite, one admitted submission per run with requestId resume binding | Settled submission and seal | Logical model request (includes auxiliary inference) |
| Codex | `app-server --listen stdio://` | `turn/completed` with completed status and seal | Turn |
| Claude | Official Python Agent SDK in separate interpreter | `terminal_reason=completed`, successful subtype, no error and seal | Query |

Settings that collide with runtime-owned prompt/model/bridge/session fields
fail before input; they are never silently overwritten. Native engine errors
remain errors. Session identifiers include model, policy, goal, capabilities,
native grants, effective configuration, command and resource content digests.
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
| `verify` | Execute operator-owned command strings and bind proof to code digest |
| `fetch`, `search` | Allowlisted retrieval / trusted discovery adapter |
| `insights` | Read proposals |
| `decide` | Record accept/modify/defer/reject and rationale |
| `submit_insight` | Submit proposal with route-derived identity |
| `consult` | Consult operator-allowlisted profiles through a named read-only role (default `consult`), no nested consultation |
| `report` | Stage a Markdown document for static artifact publication |
| `sync` | Refresh upstream refs via the trusted VCS adapter (writable workspace only); merges stay as workspace edits followed by verification |
| `vcs_read` | Read CI status, logs, PR comments, external proposals, and exact-revision proposal content via the trusted VCS adapter; never publishes (read roles may hold it) |
| `vcs_publish` | Push a branch or open a PR via the trusted VCS adapter behind a recorded human `GO <branch>` approval (writable workspace only; consultation roles cannot hold it) |
| `vcs_retire` | Retire an owned temporary integration branch at an exact expected sha behind the configured `retire_grant` (read workspace suffices: integration authority is the capability plus the grant, never source-write access; consultation roles cannot hold it; never combines with `decide` (ordinary `submit_insight` is allowed but can never mint an operator-channel approval)) |
| `vcs_dispose` | Close or merge an external proposal at its exact assessed head, base, and target behind the matching per-action grant (`close_grant` for routine close, `merge_grant` plus a recorded human `GO <branch>` approval for merges; read workspace suffices: a dedicated integrator disposes from its materialized input, whose digest plus the assessed destination and proposal endpoints bind a merge approval; consultation roles cannot hold it; never combines with `decide` (ordinary `submit_insight` is allowed but can never mint an operator-channel approval); never automatic, never inferred) |
| `research_read` | Read this role's own current research state record with its generation (read roles and consultation roles may hold it) |
| `research` | Replace this role's own current research state record under the read generation; stale generations refused, audit in `research-state.json` (consultation roles cannot hold it) |
| `finish` | Seal result; cannot acquire new permissions |

A consultation names its answering role explicitly (`role`, default `consult`).
Any configured role qualifies as long as it stays read-only (`workspace = "read"`)
without write/execute grants (`exec`, `experiment`, `verify`, `decide`,
`submit_insight`, `consult`, `report`, `sync`); read-only grants (`files`, `read`,
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
| `hosts` | `[]` | Exact HTTPS hostname allowlist; re-checked on every redirect (no wildcards) |
| `feeds` | `[]` | Allowed RSS/Atom feed URLs for discovery |
| `search_command` | `[]` | Trusted argv command receiving/returning bounded JSON |
| `intranet` | `false` | When true, permits any private unicast IP (`ip.is_private`); multicast, link-local, loopback, unspecified, and transition addresses stay refused |
| `cache_seconds` | `1800` | Response cache TTL: reuse window and expired-entry reclamation age (`retrieved_epoch` vs now; epoch-less entries use mtime) |
| `timeout_seconds`| `20` | Request timeout |
| `max_bytes` | `524288` | Maximum response payload (512 KiB) |

URL fragments are stripped client-side and never sent. Redirects re-validate hostname, port, and IP. Text/HTML/XML/JSON formats are supported; binary/PDF/image formats are rejected.

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
| `poll_enabled` | `false` | Explicit opt-in for daemon upstream/CI polling; `false` disables all daemon adapter calls |
| `poll_interval_seconds` | `300` | Daemon poll cadence in seconds (15–86,400) |
| `poll_max_branches` | `4` | CI branches polled per daemon tick (1–64) |
| `poll_fetch_timeout_seconds` | `30` | Daemon upstream-fetch adapter cap in seconds (1–300); each tick uses `min(timeout_seconds, this)` |
| `poll_status_timeout_seconds` | `15` | Daemon per-branch CI status adapter cap in seconds (1–300); each tick uses `min(timeout_seconds, this)` |

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
timeout, oversize response, nonzero exit, or malformed/non-object JSON.
Defaults apply when `[vcs]` is absent so existing configs keep loading. `retire_grant` (default `false`) is the explicit operator grant for branch retirement; `owned_prefixes` (default `[]`, e.g. `["mizu/"]`) names the temporary integration namespaces; `protected_refs` (default `[]`, e.g. `["main"]`) names exact long-lived refs that always win over owned prefixes. `close_grant` (default `false`) is the explicit operator grant for routine proposal close, and `merge_grant` (default `false`) is the separate explicit grant for proposal merge; merges additionally require a recorded human `GO <branch>` approval for the integrated tree. No project, bot, or provider names are hardcoded: an empty policy classifies nothing as owned, so nothing retires.

`ci_branch` (default `""`) names the operator-selected CI/reporting branch. It is exposed as `ci_branch` in full and delta prompts, but only to roles holding the `vcs_read` engine tool; every other role sees `null`, and an empty value reads as `null` everywhere. Reporters must select that branch for CI evidence and never guess one. Unknown keys still fail load; a non-string, overlong (>256), or whitespace-containing value fails load.

Host-side `inject_refs` writes validated refs as read-only (`0o444`) files
under the reserved workspace subtree `refs/remotes/upstream/*` (at most
4096 refs; stale entries pruned on refresh; symlink escapes refused).
`Snapshots.excluded` always excludes that subtree, so injected refs never
affect `code_digest` — even if the model rewrites them, and even under an
emptied operator `exclude` list. Operators must not keep project source
there. The `files`/`read` tools serve injected refs read-only from the
project workspace (read roles included; `none` sees none). Content stays
`external-untrusted` until merged and verified.

The `sync` tool refreshes that view on demand: it calls `fetch` on the
adapter, then `inject_refs` into the project workspace, and returns the
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
at defaults, never minutes per branch fan-out). Failures become visible `upstream_fetch` events
(`ok: false`) and never publish a snapshot (injected refs stay
digest-excluded). `last_fetch` advances on failure too, so one bad adapter
cannot busy-loop.

## VCS publish (M2 step 1: `vcs_read`/`vcs_publish` behind `GO <branch>` approval)

Same `[vcs]` adapter, no new keys. `vcs_read` serves only `status`/`log`/`comments`/`proposals`/`acquire` (addressed reads take `{"op", "branch", optional `"sha"`}; `proposals` enumerates with an optional `branch` filter; `acquire` takes `{"op", "id", "sha", "scope"}` with no branch); `vcs_publish` serves only `push`/`pr` (`{"op", "branch", "digest"}`). Cross-path calls are `Denied` (`vcs_read cannot publish`). Host-side only, single invocation under `timeout_seconds`, stdout capped at `max_bytes`; results stay `external-untrusted` with `vcs-read.json` / `vcs-publish.json` evidence. Publish requests carry the approved `code_digest` as `digest`; the adapter must verify the pushed tree matches it before pushing and echo the same `digest` in its response, else the call is `Denied` (`must confirm the pushed code digest`).

`status` responses are normalized to `{op, branch, checks: [{check, state, sha, url?}], trust}` (at most 1024 checks; `check` 1-256 chars, `state` 1-64 chars, `sha` 40/64 hex, `url` max 4096 chars); malformed shapes are `Denied`. `log`/`comments` pass through with `trust: external-untrusted`. `proposals` responses are normalized to `{op, branch?, proposals: [{id, state, head, base, draft, mergeable?, checks?, reviews?, url?}], complete?, cursor?, trust}` (at most 256 proposals; `id` 1-128 chars, `state` `open`/`closed`/`merged`, endpoints live `{repo, ref, sha}` or explicit tombstones `{deleted: true, repo, ref}` for removed forks, `draft` bool, `mergeable` bool or unknown, CI check rows reused with each row keeping its own tested sha (rollup checks may run on a merge commit rather than the head sha, so the adapter omits rather than misattaches them), `reviews` at most 64 `{reviewer, verdict, sha}` rows (`approved`/`changes_requested`/`dismissed`, each bound to the exact revision reviewed; absent means unknown, never none), `url` max 4096 chars). The envelope may carry `complete` (bool) and an opaque `cursor` (max 256 chars) for truncated enumerations: callers resume with the cursor, observation flows on partial evidence, and safe mutation stays blocked while required evidence is unavailable. A missing or truncated row never implies terminal disposition; only a reported `state` does. Proposal observations are recorded as one deduplicated insight per proposal (`proposal-<32 hex>` from the proposal id): identical facts are a read-only no-op with no rev bump, while changed head/base/check/state facts revise the record (`record_proposal_state` returns `changed`). The body carries facts only, never per-run log URLs.

The daemon polls CI only when `poll_enabled` is true, on the same `poll_interval_seconds` cadence under the same per-project lock and shared timestamps: each tick derives branches from injected refs (all when they fit `poll_max_branches`, else a rotating window tracked in poll state so every branch is covered; `main` when no refs exist), calls `status` once per branch with the `poll_status_timeout_seconds` per-call cap, and records only `failure` states as deduplicated insights (passes ignored). Explicit branch lists longer than `poll_max_branches` are refused. Failures become visible `ci_poll` events (`ok: false`) and never publish a snapshot; `last_poll` advances on failure too so one bad adapter cannot busy-loop.

External publication requires recorded human approval (AGENTS.md: "no external publication without recorded human approval"): an insight titled exactly `GO <branch>` with source `operator` (`mizu insight submit`) whose body carries a `digest: <code_digest>` line for the exact workspace `code_digest` at call time, with an `accept` decision recorded via `mizu insight decide` (run `operator`) on that insight ID. Model-submitted insights and model `decide` records are refused as forged (`operator channel`). Missing, stale, undecided, or non-accept records are `Denied` (stale digests name the staleness). `vcs_publish` requires a writable workspace, is forbidden on consultation roles, and must not share a role with `decide` (config load refuses the combination; use a dedicated publisher role). A publisher may hold `submit_insight` for ordinary reports: only operator-channel `GO` records satisfy the approval gate. Role names `operator`, `vcs`, and `editor` are refused at load so model roles cannot mint those sources. CI failures become deduplicated insights via stable IDs (`ci-<32 hex>` from branch+sha+check); repeats return the existing record (the log `url` is validated but not stored, so varying per-run URLs still dedupe; latest URL via `vcs_read`), passes are ignored.

## VCS acquire (exact-revision external content)

`vcs_read` `acquire` materializes proposal content for assessment at an explicit scope: it sends `{"op": "acquire", "id", "sha", "scope"}` where `sha` is the exact head sha from the proposal record and `scope` is `"head"` (the head commit only) or `"full"` (the base revision through the head: every proposal commit), plus an optional `base_sha` pin with `"full"` taken from the observed base endpoint. The adapter returns `{"id", "sha", "scope", "digest", "content"}` plus `"base_sha"` exactly for `"full"` scope. `head` content is immutable for the sha but omits earlier commits, so it must never be treated as the whole proposal; review and integration acquire `"full"` so the receipt binds both revisions. The product checks the `id`/`sha`/`scope` echo against the request (a pinned `base_sha` must echo back unchanged; an unpinned `"full"` binds whatever base the adapter served), and recomputes `digest` locally as sha256 over `fs.canonical(content)` instead of trusting the echo. A moved head, a moved base, a scope mismatch, or a digest mismatch is `Denied`: the caller re-observes `proposals` and retries with the fresh revision pair, so a retargeted base with an unchanged head acquires fresh content under a fresh digest rather than passing as old evidence. No branch is needed: the endpoint travels in the proposal record, so fork heads resolve by id and sha alone. Provider fetching lives in the operator adapter, which labels the immutable scope it actually serves; the generic request, validation, receipt, and retry discipline live in the product. Results stay `external-untrusted` with `vcs-read.json` evidence. `acquire` mutates nothing and needs no grant; final disposition and retirement stay separate operations.

## VCS retire (branch lifecycle: classify, then retire owned only)

`classify_branch` sorts every ref into `owned` (temporary integration branches under configured `owned_prefixes`), `protected` (exact `protected_refs`, always wins), `external` (pull/fork namespaces outside local heads), `tracking` (`refs/remotes/*`), or `other` (active work of unknown ownership). Prefixes match on component boundaries (`mizu` owns `mizu/x` but not `mizu-x`). `vcs_retire` serves only `{"branch", "expected_sha"}`: it refuses without `retire_grant = true`, refuses every non-owned class (protected/external/tracking/other are preserved, and a failed call implies nothing about the remote ref), refuses early while a recorded proposal observation still openly references the branch at head or base (naming the blocking proposal ids), then sends `{"op": "retire", "branch", "expected_sha"}`. The adapter must delete only when its current head still equals `expected_sha` AND no live (non-terminal) proposal still references the branch — one atomic provider step, never a separate observe-then-delete round trip — and echo `branch`/`sha` with `deleted: true` plus `live_refs` naming the blocking proposal ids it checked (empty on success), else the call is `Denied`. Recorded observations may be stale, so the provider affirmation is the fresh authoritative check; a provider that cannot enforce the atomic check must answer `deleted: false` instead of pretending, and the limitation surfaces as a refusal, never as an unchecked delete. Tombstoned (deleted) endpoints reference nothing live, and CI records never block. The lifecycle order is integrate, then `GO`-approve, then `vcs_dispose`, then re-observe `proposals` until the terminal state is recorded, and only then `vcs_retire`; a closed-unmerged proposal is not proof its commits are disposable. Evidence lands in `vcs-retire.json` with the classification.

## VCS dispose (final disposition after approved integration)

`vcs_dispose` serves only `close`/`merge` (`{"op", "id", "sha", "base", "target", "branch"}`): `branch` names the integration branch holding the approved tree, `id` names the proposal, `sha` is the exact assessed head, `base` the exact assessed base, and `target` the assessed target branch. Close is routine terminal reconciliation behind `close_grant = true` alone. Merge promotes the tree behind `merge_grant = true` plus a recorded human `GO <branch>` approval for the current `code_digest` (checked before the adapter spawns), so main/release promotion without a matching approval is refused. The merge approval must additionally bind the assessed destination and proposal endpoints with body lines `target: <branch>`, `proposal: <id>`, `sha: <head>`, and `base: <base>` naming exactly the merge arguments: a digest-only approval, or one bound to another target or other proposal content, never authorizes the merge (a moved head, moved base, or retarget refuses until the operator re-observes and re-approves). Either op then sends `{"op", "id", "sha", "base", "target"}`. The adapter must act only while the proposal is still open at all three assessed endpoints (compare-and-dispose immediately before action: a moved head, a moved base, a retargeted proposal, or an externally superseded proposal refuses) and echo `id`/`sha`/`base`/`target` with the terminal state (`close` ends `closed`, `merge` ends `merged`), else the call is `Denied`. Tree-content approval stays local to the `GO` gate: the adapter contract binds endpoint identity only, never pretended content atomicity. Disposition is explicit per action: no automatic merges, no bot rules, and the main promotion flow is untouched. A failed call implies nothing about the remote proposal; reconcile by re-observing `proposals`, which revises the recorded observation on state change. Evidence lands in `vcs-dispose.json` (with the approval for merges). Safe mutation stays blocked when required evidence is unavailable: tombstoned heads carry no sha to dispose, and an incomplete enumeration never implies terminal disposition.

## VCS proposal lifecycle (observe, integrate, dispose, retire)

The four stages share one contract shape. Observation (`vcs_read` `proposals`/`acquire`) is read-only and revision-bound: enumerate with cursor resume, assess exact heads by `(id, sha, scope)` with digest-bound content bound to the base revision, and record observations that revise on change. Integration is workspace edits plus verification under the untouched main promotion flow. Disposition (`vcs_dispose`) resolves one proposal to `closed`/`merged` behind the matching per-action grant (`close_grant` alone for routine close; `merge_grant` plus a `GO <branch>` approval for promotion), acting only while the proposal is still open at the assessed head, base, and target. Retirement (`vcs_retire`) deletes one owned branch behind `retire_grant` at an exact sha, and refuses while a recorded proposal still openly references it.

Receipts are scoped per mutation: `vcs-publish.json`, `vcs-dispose.json`, and `vcs-retire.json` each name the op, the exact revision (branch/sha or proposal id/sha), the terminal outcome, and the approval or grant behind it. Nothing is written on refusal; a failed call implies nothing about the remote either way, so reconcile by re-observing rather than assuming an outcome.

Revalidation is re-observation: after any failed mutation, `proposals` re-reads current state before retrying at a fresh revision. Recovery follows the failure kind: a moved head means re-assess and retry at the new sha; an externally superseded proposal (terminal state the action did not produce) means stop and reconcile instead of re-issuing; a malformed adapter echo means the adapter is at fault and the remote state is unknown. A failed call never implies the remote changed; only a fresh observation or a matching receipt does.

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
