# Configuration reference

The complete, commented canonical example is `config/config.example.toml`.
Run `mizu configure` once; edit the private copy, not the distributed template.
Unknown keys, invalid types, unknown grants, unsupported schema versions and
missing policy files fail validation. Configuration paths are resolved relative
to the config directory; `~` and `${ENVIRONMENT_VARIABLE}` are supported.
Environment substitution reads the controller process environment, not
`credentials.env`. Prefer literal model aliases for systemd services.

## Root keys

| Key | Meaning |
|---|---|
| `schema` | Currently 1; no implicit migration |
| `data_dir` | Private local filesystem state; restart before moving it |
| `pi_dir` | Private, dedicated Pi configuration/authentication directory |
| `pi_command` | Trusted argv array, not a shell string; installer writes absolute Node and CLI paths |
| `codex_command` | Trusted Codex CLI argv, not a shell string; operator-installed binary |
| `claude_command` | Trusted Claude CLI argv, not a shell string; operator-installed binary |
| `timezone` | IANA zone for calendar timers; default UTC |
| `consult_profiles` | Permitted independent consultation profiles |
| `exclude` | Snapshot/import exclusion patterns; not a secret detection system |

The daemon reloads configuration at work-unit boundaries. Changing schedules
requires regenerating units and `systemctl --user daemon-reload`. Changing an
active grant or model does not mutate an in-flight call. Pause first for urgent
changes. Operator policies live outside model-writable trees.

## Limits

| Keys | Unit and scope |
|---|---|
| `daily_requests` | Provider-hook admissions, all projects/roles, per UTC day; 0 disables, max 100K count and 4 MiB day-file (older files reaped after 31 days on admission). Pi counts provider requests; codex/claude count driver invocations (one per work unit), so counts compare only within one engine |
| `requests_per_run`, `tools_per_run` | Per work unit; child consultation units also consume shared daily budget |
| `run_seconds`, `command_seconds` | Overall unit and individual command deadlines |
| `idle_seconds`, `cooldown_seconds` | Non-LLM daemon polling and between-unit pause |
| `default_wait_seconds`, `maximum_wait_seconds` | Default and maximum model-selected waiting time |
| `max_failures` | Consecutive role failures before project pause |
| `parallel_runs`, `parallel_consults` | Shared execution slots and per-consult fan-out |
| `output_bytes` | Combined captured command stdout/stderr bound |
| `file_bytes` | Individual visible file/read bound |
| `snapshot_bytes`, `snapshot_files` | Total visible snapshot byte/file bounds |
| `history_index`, `prompt_snapshots` | Published IDs retained in the history index (8–1000000; manifests stay on disk) and recent summaries offered per work unit (1–64) |
| `free_disk_mb` | Pre-run free-space reserve, not an enforceable volume quota |

All limits are positive integers except `daily_requests` and `free_disk_mb`,
which may be zero. A consultation consumes a slot in addition to its parent;
leave spare slots for the intended fan-out. A busy slot produces a bounded
failure, not unbounded queuing or a host fallback. Request-count idempotency is
per run and sequence. Network faults after admission do not refund that request.

Provider-reported usage is retained in completed run records. It is not a
billing authority, and hidden SDK/HTTP retries or compaction behavior may vary
with upstream versions. Do not interpret `daily_requests` as a dollar ceiling.
Use `mizu status` (includes shared budget counts) and `mizu budget` for the
current UTC-day usage; `mizu usage` covers provider-reported token facts.

## Fixed mechanism bounds (not TOML knobs)

Bounds and fixed values below live in code, with the reason each is fixed
rather than operator policy. Operator-tunable counterparts live in
`[limits]`/`[web]`/`[sandbox]` above.

| Bound | Value | Why fixed |
|---|---|---|
| `PREVIEW_BYTES` (`src/mizu/fs.py`) | 128 KiB | Single preview clamp shared by `read` (`min(file_bytes, 128 KiB)`), diff output/file skip, Editor read/search skip, insight canonical size, web-worker and CLI stdin framing |
| `finish.wait_seconds` (`src/mizu/protocol.py`) | 1–86400 | Mirrors `[limits] maximum_wait_seconds` (also 1–86400); effective wait is `min(requested, maximum_wait_seconds)` |
| Tool strings: `exec`/`experiment` script 64 KiB | 65536 | Mirrors the sandbox `execute` bound; one shell-input size end to end |
| Tool strings: `submit_insight` body 60 KiB, `report` body 48 KiB, `finish` summary 12 KiB / state 32 KiB | 60000/48000/12000/32000 | Proposals, artifacts and carried state stay reviewable inside bounded prompt context; the capsule `submit_insight` shares this schema, with outbox files additionally framed by `PREVIEW_BYTES` at ingest |
| Tool strings: `experiment` question/comparison/measure 2000, `decide` reason 4000 / revisit 2000, `consult` question 8000, `search` query 1000 | 2000/4000/8000/1000 | Short framing prose; query bound mirrors `Web.search` |
| Tool IDs/arrays: path 4096, insight/consult/role IDs 64, default text 32768, default array 30 | — | Paths and hex IDs are short by construction; array 30 matches the insight list bound |
| `consult` profiles array | max 8 | Bounded fan-out (`parallel_consults` default 2); answers are recorded, never voted on |
| Capsule reads (`src/mizu/editor.py`): manifest 16 MiB, changes/history 256 KiB, scan 8 MiB, 50 hits, text slice 1000, MCP frame 1 MiB | — | Exported-bundle and JSON-RPC framing; the capsule `submit_insight` shares the runtime 60 KiB schema, with outbox files additionally framed by `PREVIEW_BYTES` at ingest |
| Web broker (`src/mizu/web.py`, `web_worker.py`): URL 4096, feeds/results 30, media text/JSON/XML only | — | 30 matches the tool array bound; no PDF/image interpretation, no intranet, no credentialed URLs |
| Web timeouts | `timeout_seconds*(min(feeds,30)+1)` for search, `+2s` for fetch, capped by `run_seconds`; 2 MiB IPC cap | Search may fan out over feeds but never outlives the parent unit |
| Pi adapter (`src/mizu/pi.py`): JSONL record 4 MiB, bridge queue 128, handshake 30 s | — | Single-record and startup bounds; exact provider/model match is verified before any paid prompt |
| Snapshots/insights: `history()` default 8, insight title 1–200, insight list 30, ingest batches 100 | — | Prompt offer uses `[limits] prompt_snapshots` (default 6); batches bound crash-recovery work |
| Restore default (`restore --max-bytes`) | 1 GiB | Malicious-archive bound; raise explicitly per restore |
| Processes/sandbox: pipe chunks 64 KiB, cleanup 15 s / 8 KiB, inspect 15–30 s / 64 KiB | — | Fixed plumbing sizes; operator policy controls command time and output totals |
| Budget day-file window | 31 days | `Budget.gc()` reaps older day-files on admission; current UTC-day accounting is never reaped |
| Web redirects and error tails | 4 redirects; Pi error text 4000, web-worker stderr tail 2000, probe tails 500–1000 | Bounded failure evidence keeps deferral/denial messages readable |
| Feed item shaping | title 300, summary 1500, results 30 | Small discovery pointers; fetching a result still needs an allowlisted host |
| Operator `insight list` | limit 1000 | Operator-side review bound (the model path uses 30) |
| Bridge socket (`src/mizu/bridge.py`) | frame 1 MiB, socket timeout +10 s, tool timeout +5 s, token 32 bytes, poll 0.1 s, join 2 s | Per-run private socket; frames fit tool results within `output_bytes` |
| MCP proxy (`src/mizu/mcp_proxy.py`) | 1 MiB both layers | Single bound shared with the capsule framing; the bridge would reject more anyway |
| Drivers (`codex.py`/`claude.py`) | events 4 MiB, diagnostics tail 256 KiB, MCP startup 10 s, tool 60 s | Bounded evidence per invocation; required/forbidden argv pinned in `adapters/*/compatibility.json` |
| Doctor smoke | sandbox probe 30 s | Real isolation exercise; unit auth and budget gates stay separate |
| Dashboard shaping | pending 30, recent decisions 10, reason 500 chars (flagged), day-groups 31 | Small-screen projection of recorded facts; raw snapshots/runs/decisions stay on disk |
| Usage scan | 5000 runs, 200 recent entries, 20 unknown keys | Bounded aggregation; overflow is flagged (`truncated`/`entries_truncated`), never silently dropped |
| Pi shutdown/diagnostics | stderr 4 KiB reads capped by `output_bytes`, terminate grace 2 s, reader join 2 s | Bounded shutdown; admission and usage evidence stay in the run record |
| Project goal | 64 KiB, nonempty | Goal is carried per snapshot; larger design docs stay outside the project record |
| Restore member loop | 100000 members, 1 MiB copy blocks | Malicious-archive bounds alongside `max_bytes` |
| Doctor probes | podman info 20 s/1 MiB, namespace evidence 10 s/1 KiB | Installation checks fail loudly with bounded evidence |
| Doctor version gates | node/pi probes 10–30 s, 1–8 KiB; error tails 500–1000 | Pinned-interface verification with readable refusals |
| Dashboard control reason | 1000 chars | Operator pause/resume reasons stay short in the projection |
| Managed-repo git calls | timeout 15 s, output 8 KiB; import status probe 30 s/1 MiB, clone 300 s | Fixed plumbing for init/restore; repository content itself is operator data |
| Smoke probe (`src/mizu/smoke.py`) | requests 2, tools 5, 120 s (each `min()` with operator limits) | Paid live probe never exceeds a tighter operator budget |
| Initial snapshot text | summary/state from init, checkpoint "not published" | Fixed fallback prose for unstarted projects; all later text is operator/model data |
| Git identity | `Mizu <automation@localhost>`, hooks disabled | Managed worktrees never inherit operator git identity or hooks |
| Storage maintenance | `maintenance/` in backup scope; prune audit per apply | Destructive prune operations leave a timestamped record that is itself backed up |
| Process termination | SIGTERM grace 1 s, then SIGKILL, wait up to 5 s; 64 KiB pipe chunks | Process groups never outlive their unit; chunks bound pump memory |
| service templates (`src/mizu/services.py`) | systemd: stop 45 s, restart 15 s, accuracy 10 s, delay 15 s, boot 1 min; launchd: KeepAlive/StartInterval; Task Scheduler: LogonTrigger/repetition | Fixed per-platform template values; schedules stay operator policy |
| Prune retention default | keep 30 artifacts (live pointer included) | Retention stays operator-overridable; explicit 0 keeps all |
| Backup scope and quiescence | `MEMBERS` tuple in `src/mizu/storage.py`; paused plus workspace/run locks | Single source for backup/restore membership; maintenance records are included |
| `file_bytes`/`snapshot_bytes`/`snapshot_files` | operator policy | Only the preview/diff caps above stay fixed; bulk snapshot bounds are tunable |

`on_change` is orthogonal to scheduling: it only skips a unit when the
published snapshot is unchanged, and it combines with `interval_seconds`
(the stock reviewer uses both). A role with `on_change` alone and no
`daemon`/`interval_seconds`/`calendar` gets no service definition from
`mizu service` and runs only on explicit `mizu run`.

## Sandbox

`executable` is a trusted container runtime: `podman` or `docker` (bare name
or absolute path; Podman Desktop and Docker Desktop both work on macOS and
Windows). Only flags both runtimes accept are used (`--user`, `--network=none`,
`--read-only`, `--cap-drop`, resource limits); the container interior is always
Linux. `image` must be a local `sha256:<64 hex>`
ID or a digest-pinned registry reference already available locally. Runtime pulls
are forbidden. `memory_mb`, `cpus`, `pids`, `temporary_mb`, `file_mb` control the
container. `file_mb` limits each file via `RLIMIT_FSIZE`, not total source storage.
`selinux_label` controls `:z` relabeling of dedicated mounts on Linux only.

Do not place unrelated directories under mounts or relabel home/system trees.
Use a dedicated source volume and a filesystem quota when total disk usage must
be strictly capped. Container images and their package managers are operator
policy, not something a model can pull or rebuild on the host.

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
user, no inherited secrets, networkless namespace, container cleanup) on every
OS before any project runs.

## Profiles and roles

A profile has `provider`, `model`, `thinking`, `engine`. IDs are exact; for
`pi` they are verified against the running Pi after startup. `thinking` is
one of the accepted Pi levels; support is still model-dependent. `engine` is
one of `pi` (default, preserves existing behavior), `codex` or `claude`,
resolved through the generic driver registry (`src/mizu/drivers.py`) under
the same work-unit contract. No named model is hardcoded. A new profile can
be selected by changing `roles.NAME.profile` or a permitted consultation
alias.

| Engine | Auth (operator, out of band) | System prompt | Tools | Sessions | Counting |
|---|---|---|---|---|---|
| `pi` | `pi` login: API key or subscription (including `openai-codex` via subscription login) | `system.md` = role policy verbatim | Unix-socket extension, no builtins | Persistent, keyed by role/model/goal/policy/caps | Provider requests via admission hook |
| `codex` | `codex login` (ChatGPT subscription or API key; file store, keyring, or inline `CODEX_API_KEY` for that invocation only) | Generated `instructions.md` = role policy verbatim | Required `mizu` MCP server only; read-only sandbox, shell/web/multi-agent off, no persisted sessions | One-shot `--ephemeral`; continuity comes from Mizu snapshots | Invocations |
| `claude` | `claude login` (subscription) | `--system-prompt` = role policy verbatim | Per-run MCP config plus explicit `mcp__mizu__*` allowlist; skip-permissions flags never passed | One-shot; continuity comes from Mizu snapshots | Invocations |

Subscription quotas are account-level and shared across every route to that
account (Pi, vendor CLI, web): 5-hour/weekly caps can pause a 24-hour
deployment regardless of engine. Prefer API keys or enterprise automation
tokens for unattended runs; use subscriptions for assisted runs or as a
second profile for comparison. Rate-limit errors map to `wait` and then to
the repeated-failure auto-pause; there is no silent model swap and no
admission refund.

A role has `profile`, `policy`, `workspace` (`write`, `read`, `none`),
`capabilities`, optional `persistent`, optional `on_change`, and at most one of
`daemon`, `interval_seconds`, `calendar`. `finish` is required. `verify` requires
a writable workspace. Project `roles` select which configured roles run there.
Daemon roles stream JSON Lines (result objects and `run_deferred` events, see
`operations.md`); the other schedules run one unit per trigger.

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
| `finish` | Seal result; cannot acquire new permissions |

A consultation names its answering role explicitly (`role`, default `consult`).
Any configured role qualifies as long as it stays read-only with only
`files`/`read`/`finish`; anything broader is refused before any model call.
How answers are combined is worker judgment under policy, never a mechanism
vote: the runtime records answers without ranking them.

Avoid multiple writer roles on one project even though a shared workspace lock
serializes them. The supplied Maintainer belongs to its own candidate project.
Editor is intentionally not an autonomous role table: it is a separate,
human-triggered stock harness connected through a readonly capsule.

## Web

`hosts` is an exact HTTPS hostname allowlist. No wildcard suffix matching.
`feeds` contains RSS/Atom URLs whose hosts must also be allowed. `search_command`
is an optional trusted argv executable accepting/returning bounded JSON; see
[extensions](extensions.md). `cache_seconds`, `timeout_seconds`, `max_bytes`
control retrieval. Redirects revalidate hostname, scheme, port and public IP.

Empty sources disable discovery; they are not replaced by an implicit provider.
Search results are untrusted pointers. Fetching a result still requires an
allowed host. Text/HTML/XML/JSON are supported; PDF/image interpretation is not.

## Project metadata

`projects/NAME/PROJECT.md` is the human goal. `project.toml` has `schema`, `roles`
and repeatable `verify` command strings. These are outside the workspace and
are not exposed as writable model tools. Alter them as operator while paused.
Verification command exit status alone does not prove semantic correctness,
particularly when the repository's tests are editable. Use independent review
and external acceptance fixtures for critical requirements.
