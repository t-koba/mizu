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
| `timezone` | IANA zone for Linux calendar timers; default UTC. macOS uses explicit local; Windows supports local and UTC (see completion-contracts.md) |
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
| `daily_requests` | Shared provider admissions across all projects per UTC day (0 disables, max 100,000). Day-files older than `retention_days` are reaped. Pi counts logical model requests, Codex counts turns, Claude counts queries |
| `requests_per_run`, `tools_per_run` | Per-work-unit request and tool bounds; child consultations consume shared daily budget |
| `run_seconds`, `command_seconds` | Deadlines for the overall work unit and individual container commands |
| `idle_seconds`, `cooldown_seconds` | Daemon sleep between poll iterations and pause between work units |
| `default_wait_seconds`, `maximum_wait_seconds` | Default and maximum model-selected wait times (1–86,400s) |
| `max_failures` | Consecutive role failures before auto-pause (0 disables auto-pause) |
| `parallel_runs`, `parallel_consults` | Global execution slots and per-consultation fan-out (`parallel_consults <= parallel_runs`) |
| `output_bytes` | Maximum captured stdout/stderr per command |
| `file_bytes` | Maximum readable file size |
| `snapshot_bytes`, `snapshot_files` | Total visible snapshot size and file-count bounds |
| `history_index` | Maximum published snapshot IDs retained in the immutable history generation (8–1,000,000; manifests stay on disk) |
| `prompt_snapshots` | Number of recent anchored snapshot summaries offered to prompts (1–64) |
| `retention_days` | Retention window (days) for daily budget files and decided proposals (0 disables; 0–3,650). Reaping reports `reaped`. Snapshots, runs, decisions, and evidence are never reaped |
| `pending_insights` | Newest pending proposals offered in prompts/dashboard (1–1,000; default 30). `mizu insight list` uses 1,000 |
| `free_disk_mb` | Minimum free disk space required to start a work unit |

All limits are positive integers except `daily_requests`, `max_failures`, `free_disk_mb`, and `retention_days`, which may be zero.
`daily_requests` counts logical admissions, not dollar amounts; configure spending limits with your provider. Use `mizu status` and `mizu budget` for UTC-day counts, and `mizu usage` for token statistics.

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

`on_change` is orthogonal to scheduling: it skips execution when the published snapshot is unchanged. A role with `on_change` alone and no `daemon`/`interval_seconds`/`calendar` runs only via explicit `mizu run`.

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
`excludeTools`, `scopedModels`; Codex native configuration keys plus `options.turn` for native turn fields
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
present in the selected image; environment belongs in `sandbox.env`. No
installation, login, package acquisition or deployment runs during work.

A role has exactly one of `profile` or `selector`, plus `policy`, `workspace` (`write`, `read`, `none`),
`capabilities`, `engine_tools` (maximum 128 explicit names), `on_change`, and
at most one of `daemon`, `interval_seconds`, `calendar`. Capabilities grant
Mizu operations; `engine_tools` grants native or external MCP tools such as
`codemode`, `web_search`, or `mcp__docs__search`. Exposure and automatic approval
never grant execution authority. Native host command/file tools cannot bypass
the bridge and publication contract.

| Engine | Managed interface | Completion | Admission unit |
|---|---|---|---|
| Pi | `createAgentSession`, `ModelRuntime`, `runRpcMode` | `agent_settled` and seal | Logical model request (includes auxiliary inference) |
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
| `vcs_read` | Read CI status, logs, PR comments via the trusted VCS adapter; never publishes (read roles may hold it) |
| `vcs_publish` | Push a branch or open a PR via the trusted VCS adapter behind a recorded human `GO <branch>` approval (writable workspace only; consultation roles cannot hold it) |
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
| `cache_seconds` | `1800` | In-memory response cache TTL |
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
| `timeout_seconds` | `20` | Per-invocation deadline (1–16,777,216) |
| `max_bytes` | `524288` | Maximum adapter request/response payload, 512 KiB (1–16,777,216) |

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
Defaults apply when `[vcs]` is absent so existing configs keep loading.

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

The daemon performs the same host-side refresh periodically without any
capability check (operator-scheduled polling, not model authority): each
daemon iteration calls `poll_upstream` once it is due. Cadence reuses the
operator-selected `[limits] idle_seconds` (no new knob per ADR-006); an
empty `command` disables polling. Polling is best-effort: adapter/shape
failures become visible `upstream_fetch` events (`ok: false`) and never
publish a snapshot (injected refs stay digest-excluded). `last_fetch`
advances on failure too, so one bad adapter cannot busy-loop.

## VCS publish (M2 step 1: `vcs_read`/`vcs_publish` behind `GO <branch>` approval)

Same `[vcs]` adapter, no new keys. `vcs_read` serves only `status`/`log`/`comments` (`{"op", "branch", optional `"sha"`}); `vcs_publish` serves only `push`/`pr` (`{"op", "branch"}`). Cross-path calls are `Denied` (`vcs_read cannot publish`). Host-side only, single invocation under `timeout_seconds`, stdout capped at `max_bytes`; results stay `external-untrusted` with `vcs-read.json` / `vcs-publish.json` evidence.

`status` responses are normalized to `{op, branch, checks: [{check, state, sha, url?}], trust}` (at most 1024 checks; `check` 1-256 chars, `state` 1-64 chars, `sha` 40/64 hex, `url` max 4096 chars); malformed shapes are `Denied`. `log`/`comments` pass through with `trust: external-untrusted`.

The daemon polls CI on the same `[limits] idle_seconds` cadence as the upstream fetch (no new knob): each iteration derives branches from injected refs (first 32 sorted, else `main`), calls `status` once per branch, and records only `failure` states as deduplicated insights (passes ignored). Polling is best-effort host-side work with no capability check: failures become visible `ci_poll` events (`ok: false`) and never publish a snapshot; `last_poll` advances on failure too so one bad adapter cannot busy-loop.

External publication requires recorded human approval (AGENTS.md: "no external publication without recorded human approval"): an insight titled exactly `GO <branch>` whose body carries a `digest: <code_digest>` line for the exact workspace `code_digest` at call time, with an `accept` decision on that insight ID. Missing, stale, undecided, or non-accept records are `Denied` (stale digests name the staleness). `vcs_publish` requires a writable workspace and is forbidden on consultation roles. CI failures become deduplicated insights via stable IDs (`ci-<32 hex>` from branch+sha+check); repeats return the existing record (the log `url` is validated but not stored, so varying per-run URLs still dedupe; latest URL via `vcs_read`), passes are ignored.

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
