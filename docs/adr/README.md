# Architecture decisions

## ADR-001 — Local files and operating-system supervision

**Accepted.** Use JSON/Markdown files, atomic publication, flock and systemd user
units instead of a database, queue and custom scheduling service. The initial
scope is one Linux host with modest role concurrency. This makes records
inspectable and recovery explicit. It requires a local filesystem and bounds
metadata scans; multi-host coordination is not supported. Revisit only with
measured workload requirements, not hypothetical scale.

## ADR-002 — Isolated commands, not host tool wrappers

**Accepted.** Pi is a trusted inference/session process with no built-in tools.
Its primary extension talks to the host capability bridge (with up to 64 operator-configured,
hash-verified extensions allowed). Model commands execute
in a rootless container (Podman / Docker) with fixed mounts and limits. Editor is a separate capsule.
This costs startup overhead and requires prepared images, but avoids policy-only
permissions and a broad host shell. A VM may be required for a stronger threat
model. No unsafe host fallback is provided.

## ADR-003 — One integrating writer, many independent observers

**Accepted.** Independent models and search/review processes submit evidence or
proposals. One Worker owns integration and records proposal disposition. It may
adapt/reject proposals, not human goals. Read snapshots are anchored to one
published state. This avoids cross-agent edit races and context disagreement;
it sacrifices unconstrained parallel implementation throughput.

## ADR-004 — Explicit update promotion

**Accepted.** Maintainer works on a candidate project. Installer stages source,
checks it and records integrity metadata; operator promotion changes a symlink
after all projects are paused and services stopped. No model self-deploys or
changes safety policy. Updates preserve private configuration. This is simpler
and more recoverable than autonomous in-place mutation, but requires an operator
at the deployment boundary.

## ADR-005 — Accountable limits, not invented monetary guarantees

**Accepted.** Enforce provider admission count, runtime, output and container
resources outside the model. Retain reported usage. Do not call count budgets
currency limits or per-file bounds volume quotas. Provider billing caps and
filesystem quotas remain explicit complementary controls. Document and test
these boundaries rather than hiding them behind aspirational settings.

## ADR-006 — Mechanism/policy separation line

**Accepted.** The mechanism owns safety, resource and evidence invariants
plus the protocol commit vocabulary; policy owns who, when, in what words
and what counts as good. Concretely:

- Mechanism: capability checks, sandbox argv and privilege floors, atomic
  publication pointers, content hashes, budget/slot admission, failure
  brakes (auto-pause unless `max_failures = 0`, lock refusal, unarmed refusal,
  no self-promotion),
  `finish` outcomes (`continue`/`wait`/`blocked`/`done`) and `decide`
  verdicts (`accept`/`modify`/`defer`/`reject`). The verdict/outcome
  vocabularies are fixed because they are commit-point states, not prose.
- Policy: role formation (`init` defaults to a single unarmed `worker`;
  the four-role set is an operator-spelled `--roles` choice), arm posture
  (`init --armed` is allowed only as an explicit operator act at import
  time; restore stays unarmed), consultation answering role and answer
  combination (the runtime records answers unranked),
  artifact presentation (HTML lives in `examples/`, never in `src/`) and
  artifact retention (`prune --keep-artifacts`).
- Mechanism constants that stay code, not knobs: systemd unit template
  values (restart delays, timer accuracy), smoke-probe budgets, the prompt
  `mizu_finish` call convention. The runtime prompt no longer claims
  same-turn `alone` enforcement it does not implement; that convention is
  policy guidance in `policies/`. Tunable context bounds
  (`history_index`, `prompt_snapshots`) live in `[limits]` because an
  operator can legitimately trade recall against cost.

Add a knob only when an operator would plausibly turn it; otherwise name
the constant and document it here.

## ADR-007 — Current native engine adapters

**Accepted.** Profiles explicitly select an engine, provider, model, session
mode, native options, reviewed local resources and MCP servers. Runtime argv and
agent directories live under `[engines.pi|codex|claude]`. Native thinking/effort
values are forwarded without a common fixed list; requested and observed values
remain separate evidence.

Pi uses the public SDK and metered ModelRuntime. Codex uses app-server stdio;
Claude uses the official Python Agent SDK in an independently locked adapter
environment. All drivers require the bridge before dispatch and a sealed finish
plus native terminal success. Resume failures never become new conversations.
Session identity binds policy, model, grants, effective options, local resource
content and adapter content. No internal generation numbers, readers for older
formats, conversions or engine version rejection paths exist.

Role capabilities authorize Mizu operations; `engine_tools` authorizes extra
native tools. Built-in host commands and native workspace writes cannot bypass
the OCI/publication bridge. Operator stdio MCP runs through existing OCI limits;
HTTP MCP endpoints and trusted local extension/plugin code remain explicit
operator choices. Approvals do not expand grants; child tools cannot exceed the
parent. Trusted extension code is not isolated from the adapter host.

Pi reserves logical model requests before dispatch, including auxiliary calls.
Codex reserves turns and Claude reserves queries, without claiming observation
of their internal HTTP requests. Native cumulative usage is differenced and
repeated notifications do not multiply it. SDK/CLI dependency locks and actual
executable version evidence remain; doctor checks required capabilities.
Synthetic peers, installed SDKs with mock providers, paid inference and actual
OCI isolation are reported separately. Deployment remains an operator action.

## ADR-008 — Upstream sync via trusted argv adapter and `sync` capability

**Accepted.** Add an operator-configured trusted argv `[vcs] command` adapter
speaking JSON on stdin/stdout (same shape discipline as `web.search_command`:
bounded request and response, timeout, no shell). The host side (not the
sandbox) uses it to fetch upstream refs and injects them read-only into the
managed workspace as `refs/remotes/upstream/*`. New capability `sync` lets the
writer merge or rebase `upstream/main` and resolve conflicts, then proceed with
normal verify and publish. The daemon schedule performs a periodic fetch.
Other human developers may commit upstream at any time.

## Context

Mizu projects are single-writer workspaces with content-addressed snapshots.
Upstream collaboration needs a bounded bridge to outside refs without giving
models host shell, network, or push rights. `web.search_command` already
establishes the trusted-argv JSON pattern; reuse it for VCS.

## Decision

- Mechanism provides capabilities; policy decides behavior. The `[vcs]`
  adapter, `sync` grant, read-only ref injection, and snapshot-digest
  exclusion are mechanism. When to fetch/merge, merge vs rebase choice, and
  conflict-resolution judgment are operator/worker policy, not code
  heuristics.
- `[vcs]` table: `command` (argv array, empty disables), `timeout_seconds`,
  `max_bytes`. Unknown keys fail validation. Defaults keep existing configs
  loading. Step 1 ships validation/docs only; fetch, ref injection, `sync`
  dispatch, and daemon fetch land in later steps behind the same bounds.
- Adapter contract (full M1): JSON stdin `{"op": ..., ...}` bounded by
  `max_bytes`; JSON stdout object with refs; `process.run` with timeout, no
  shell, `maximum=max_bytes`; nonzero exit, timeout, oversize, malformed JSON
  are `Denied`. Host-side only, never in sandbox, never model-provided argv.
- Refs appear read-only under `refs/remotes/upstream/*` in workspace views;
  snapshots exclude them from `code_digest` tampering (digest covers tracked
  source only). `sync` refused without grant; unknown op refused otherwise.
- Conflict flow: writer with `sync` merges/rebases `upstream/main`, resolves
  conflicts as normal workspace edits, then must pass normal `verify` and
  publish. No silent auto-resolve in mechanism.
- Daemon periodic fetch uses a fake-clock-testable interval; fetch failures
  are best-effort events, never silent publication.

## Schema, bounds, trust, retry/cancellation, evidence, failure

- Schema: `[vcs]` as above; adapter JSON request/response documented in
  `docs/configuration.md` (this step) and later in the fetch module docstring.
- Bounds: argv entries nonempty without newlines; `timeout_seconds`,
  `max_bytes` in `[1, 16777216]`; adapter stdout capped at `max_bytes`.
- Trust: operator-owned host program; upstream refs are external-untrusted
  until merged and verified. No secret access, no host execution outside the
  trusted argv.
- Retry/cancellation: single invocation per fetch under the configured
  timeout; no shell retry. Daemon cancellation via stop event.
- Evidence: fetch receipts with adapter digest; `sync` results bound to
  resulting `code_digest` via normal verification.
- Failure: `ConfigError` on bad config; `Denied` on adapter shape/timeout/
  oversize/nonzero-exit; `Denied` on `sync` without grant.

## Consequences

Staged delivery: step 1 (config/docs/tests) lets operators stage `[vcs]`
without behavior change. Later steps add the adapter module, ref injection,
`sync` dispatch, and daemon fetch, each with contract tests (fake command:
success, timeout, oversize, malformed JSON, nonzero exit), read-only and
digest-exclusion tests, grant tests, end-to-end conflict test with fake
engine, and periodic-fetch test with fake clock. Offline `check.py` stays
green. No model-specific defaults; portable stdlib-only runtime.

## ADR-009 — VCS publish behind recorded human approval, read/publish split

**Accepted (M2 step 1).** Add `vcs_read` (CI status/logs, PR comments) and
`vcs_publish` (push, PR) operations over the existing operator-owned trusted
argv `[vcs]` adapter (same JSON stdin/stdout, timeout, `max_bytes`, no shell
as M1/`web.search_command`). Push/PR fail closed without a recorded human
approval: an insight titled exactly `GO <branch>` whose body carries a
`digest: <code_digest>` line for the exact tree being published, with an
`accept` decision on that insight. CI failures become insights with stable
deduplicating IDs.

## Context

M1 gave the writer a read-only upstream view (`sync` refresh) with no push
path. Publishing to the outside world needs a mechanism that cannot be talked
into shipping code: capability-gated tools, a human record bound to the exact
bits, and a read path that cannot mutate. AGENTS.md states the invariant:
"no external publication without recorded human approval".

## Decision

- Mechanism provides capabilities; policy decides behavior. The `vcs_read` /
  `vcs_publish` tools, the `GO <branch>` digest check, and the stable CI
  insight IDs are mechanism. When to publish, which branch, and whether CI is
  green enough are operator/worker policy, not code heuristics.
- Same `[vcs]` table, no new config keys: `command`, `timeout_seconds`,
  `max_bytes` (unknown keys still fail). Capabilities `vcs_read` and
  `vcs_publish` gate the tools; `vcs_publish` requires a writable workspace
  (config load refuses it on read roles, call time refuses replaced roles)
  and is forbidden on consultation roles. `vcs_read` is read-only and allowed
  on read roles.
- Adapter contract: `vcs_read` serves only `status`/`log`/`comments` with
  `{"op", "branch", optional "sha"}`; `vcs_publish` serves only `push`/
  `pr` with `{"op", "branch"}`. Cross-path ops are `Denied`
  (`vcs_read cannot publish`). Host-side only, never in sandbox, never
  model-provided argv; single invocation under the configured timeout.
- Approval record: insight title `GO <branch>` (branch validated like a ref
  name, max 256 chars), body line `digest: <64 hex>`, decision `accept` on
  that insight ID. The runtime captures the workspace `code_digest` at call
  time and compares against the body line; stale digests, missing lines,
  undecided or non-accept decisions are `Denied` with distinct reasons. Any
  valid matching record suffices; the returned receipt names the insight ID,
  branch, and digest.
- CI insights: `record_ci_result` records only `failure` states as insights
  from adapter facts; passes are ignored. ID `ci-<32 hex>` derives from
  branch+sha+check, so repeats resubmit identical content and return the
  existing record instead of duplicating the inbox. Bodies are labeled
  `external-untrusted`.
- No silent auto-publish, no auto-merge, no retry in mechanism. Publication
  writes `vcs-publish.json` / `vcs-read.json` evidence; completion still binds
  to `code_digest` via `verify`.

## Schema, bounds, trust, retry/cancellation, evidence, failure

- Schema: `vcs_read {op, branch, sha?}`, `vcs_publish {op, branch}` per
  `protocol.DEFINITIONS`; adapter JSON objects validated by `vcs.invoke`.
- Bounds: branch 1-256 chars; sha 40/64 hex; check names 1-256 chars; log
  URLs 4096 chars; adapter stdout capped at `max_bytes`; at most one adapter
  spawn per call.
- Trust: operator-owned host program; adapter results and CI bodies stay
  `external-untrusted` until merged/verified. Approval trust comes from the
  local insight/decision store, not the adapter.
- Retry/cancellation: single invocation per tool call under
  `timeout_seconds`; tool-cancelled runs refuse before dispatch.
- Evidence: `vcs-publish.json` (op/branch/digest/approval/result),
  `vcs-read.json`, CI insight IDs.
- Failure: `ConfigError` on `vcs_publish` without writable workspace at load;
  `Denied` on missing/stale/unaccepted approval, cross-path ops, bad
  branch/sha, adapter timeout/oversize/nonzero-exit/malformed JSON, or
  unrepresentable workspace.

## Consequences

Step 1 ships the capability/protocol split, the approval gate, the CI dedup
helper, AGENTS.md invariant, docs, and fake-adapter tests (unapproved push
refused, stale digest refused, accepted digest publishes, `vcs_read` cannot
publish, CI dedup, consult cannot hold `vcs_publish`). Later M2 steps add
daemon CI polling and richer status shapes behind the same bounds, each with
focused offline tests. `check.py` stays green; stdlib-only; no model-specific
defaults.

## ADR-010 — Config `include` of shared TOML fragments

**Accepted.** Add a top-level `include` array of shared TOML fragment paths.
Fragments merge before validation (deep-merge tables; any duplicate leaf
path is a `ConfigError` naming both files). Unknown keys still fail in
every fragment. This lets operators keep one reviewed base (limits,
engines, sandbox floors) across configs and retire the out-of-tree
concatenation helper `mizu-env/ops/render-config.py` (not edited here).

## Context

Fleet operators duplicate whole configs per host/project and concatenate
shared bases with an external script. External concatenation has no
duplicate detection, no symlink discipline, and no depth/size bounds, and
it lives outside the auditable load path. A load-time `include` keeps one
mechanism, one validation, and explicit errors.

## Decision

- Mechanism provides capabilities; policy decides behavior. Fragment
  discovery, merge order (fragments before includers, transitive first),
  duplicate refusal, symlink refusal, and depth/size bounds are mechanism.
  What to share, how to split files, and merge-vs-override choices are
  operator policy: the mechanism never silently prefers one definition.
- Schema: root-only `include = [...]` array of strings, allowed in the
  top file and in fragments, consumed at load. Tables deep-merge; arrays
  never concatenate. `include` itself unions across files (each file's
  entries are followed) rather than conflicting.
- Bounds (fixed per ADR-006, code not knobs): `MAX_INCLUDE_DEPTH = 8`,
  `MAX_INCLUDE_FILES = 32`, `MAX_INCLUDE_BYTES = 1048576` total,
  `MAX_INCLUDE_ENTRIES = 32` per file, entry length at most 4096 chars,
  `.toml` suffix required. Diamonds (same file twice) merge once.
- Trust: entries are literal relative paths against the declaring file's
  directory. Absolute paths, `~`, `${VAR}`, NUL/newlines refused; any
  symlink in the written path at/below the includer directory or in the
  target refused. Values inside fragments resolve against the top-level
  config directory as if inlined. `${VAR}` expansion semantics unchanged
  (fail-closed at existing call sites, never applied to entries).
- Retry/cancellation: single load-time pass, no retries; any failure is a
  `ConfigError` before validation.
- Evidence: duplicates name the dotted key and both files; cycles name
  the chain; per-fragment unknown keys name the fragment.
- Failure: `ConfigError` on bad entries, missing fragments, symlink
  escapes, cycles, bound breaches, duplicates, or unknown keys. No
  `include` means byte-identical behavior to before.

## Consequences

Operators can split configs into reviewed shared fragments with
duplicate mistakes failing loudly with both filenames. The external
render script can retire. Offline tests cover merge, nesting relative to
the includer, duplicates naming both files, absolute/symlink refusal,
cycles, depth/size bounds, unchanged `${VAR}` behavior, and unknown keys
still failing. `check.py` stays green; stdlib-only; portable.

## ADR-011 — Non-blocking work via park-and-continue worker policy (no new mechanism)

**Accepted.** `blocked` on one backlog item must not stop other runnable work.
Finding: worker policy alone suffices — no mechanism change. The writer parks
the operator-dependent item in state as `Qn (parked): ...` and finishes
`continue` while runnable work remains; `blocked` is reserved for when every
remaining item waits on the operator. The daemon then keeps running under the
existing `should_run` contract (`continue` always runs; `blocked`/`wait` resume
only on wake, new proposals, or elapsed wait). `needs_operator_input` stays
exactly `outcome == blocked`, so it keeps meaning "nothing further without the
operator" instead of "something wants the operator while work continues".

## Context

`should_run` (mechanism, `src/mizu/runtime.py`) returns `False` for a `blocked`
snapshot until a goal change, a `wake_generation` change, or a new proposal
arrives. A writer that finishes `blocked` because one item needs operator input
therefore idles the daemon even when other backlog items are runnable. The
question was whether to add mechanism (e.g. per-item blocking states, a
continue-while-blocked outcome) or to settle the scheduling choice in policy.

## Decision

- Mechanism provides capabilities; policy decides behavior. Run/don't-run
  stays the fixed `should_run` vocabulary; which backlog item to work, in what
  order, and when an item is parked are worker policy, not code heuristics.
  No new outcome, knob, or per-item state is added.
- Park-and-continue rule (in `policies/worker.md`): while any runnable item
  remains, record the stalled item one line per question (`Qn (parked):` with
  tried, needs, and resume) and finish `continue`. Finish `blocked` only when
  every remaining item waits on the operator, keeping the existing per-item
  `Qn:` lines.
- A parked question is still answered without a fresh wake: the operator's
  answer arrives as a proposal/decision (or `A: <qid>`), which advances the
  inbox generation and resumes even a later `blocked` snapshot; the worker
  then unparks the item in the next unit.

## Schema, bounds, trust, retry/cancellation, evidence, failure

- Schema: no new interface. State lines use the existing `Qn:` shape with a
  `(parked)` marker; `finish` outcomes keep the fixed
  `continue`/`wait`/`blocked`/`done` vocabulary (ADR-006).
- Bounds: unchanged — state stays short and scoped per worker policy; no new
  stored records.
- Trust: parked items are worker prose (data, not proof); operator answers
  arrive through the existing insight/decision store, not through state text.
- Retry/cancellation: unchanged daemon semantics; parking never retries or
  cancels operator input.
- Evidence: fake-engine scenario test (`tests/test_nonblocking.py`) shows the
  daemon continues past a parked item and `needs_operator_input` stays bound
  to `blocked`.
- Failure: finishing `blocked` while runnable work remains is a policy miss
  (daemon idles until new input), not a mechanism error; review catches it via
  the published state. Finishing `continue` with nothing runnable spins the
  daemon at cooldown cadence — `wait`/`blocked` remain the correct outcomes
  there.

## Consequences

No code change to runtime, daemon, dashboard, or protocol. Operators get
non-blocking progress without a new commit-point state; `needs_operator_input`
keeps its exact meaning. If a future workload shows policy alone failing
(e.g. many interleaved stalls where state lines lose track), revisit with
measured evidence — not hypothetical scale — per ADR-001.

## ADR-012 — AGENTS.md holds contributor rules only; operator rules live in docs/

**Accepted.** `AGENTS.md` keeps contributor rules only (mechanism/policy
separation, minimal mechanism, single-writer, offline checks, focused
regression tests, interface-doc duties, no private paths, no external
publication without recorded human approval). Operator instructions for
deployment, credentials, scheduling, and promotion are removed from
`AGENTS.md`; the file points at the operator docs instead
(`docs/setup.md`, `docs/operations.md`, `docs/releasing.md`,
`docs/security.md`, plus `docs/configuration.md` for the `GO <branch>`
approval shape). The removed operator sentences are delivered to the
operator as the insight "Operator rules extracted from AGENTS.md". No new
mechanism, config keys, or stored records.

## Context

`AGENTS.md` mixed contributor prohibitions ("do not add automatic
deployment ...") with operator-facing sentences ("Executing Maintainer
roles work in their own candidate projects. Repository contributors update
the current development source; deployment remains an explicit operator
action."). Contributors need prohibitions; operators need procedures
(service units, credential stores, promotion staging). One file serving
both invites private paths and stale duplicated procedures, and lets
contributors mistake operator procedure for something to encode in code.

## Decision

- Mechanism provides capabilities; policy decides behavior. This split is
  documentation policy, not mechanism: no code, schema, bounds, or grant
  changes.
- Keep in `AGENTS.md`: contributor scope line, mechanism/policy and KISS
  rules, single-writer contract (as a contributor prohibition), offline
  checks and test discipline, interface-doc duties, Markdown/TOML policy
  line, "work in the current development source; do not stage, promote, or
  deploy a release from a contributor change", and the publication
  invariant with a pointer to `docs/configuration.md`.
- Remove from `AGENTS.md`: the Maintainer-candidate/operator-deployment
  sentences (delivered as the operator insight); replace with an explicit
  out-of-scope pointer to the four operator docs above.
- Operator docs (`docs/setup.md`, `docs/operations.md`,
  `docs/releasing.md`, `docs/security.md`) are unchanged and remain the
  single home for deployment, credentials, scheduling, and promotion.

## Schema, bounds, trust, retry/cancellation, evidence, failure

- Schema: no new interface. `AGENTS.md` keeps prose only; doc pointers are
  relative `docs/*.md` paths.
- Bounds: no private paths or operator commands in `AGENTS.md`
  (`systemctl`/`launchctl`/`schtasks`, credential-store paths, home
  directories); pointers name four existing docs.
- Trust: `AGENTS.md` grants no permissions; operator procedures stay
  operator-owned docs, not model instructions.
- Retry/cancellation: not applicable (docs-only change).
- Evidence: `tests/test_agents_split.py` pins contributor scope plus the
  invariant, the four pointers, link validity (every `docs/*.md` mention
  resolves), and absence of operator commands/private paths.
- Failure: a broken doc pointer or a reintroduced operator instruction
  fails the new test; `python3 scripts/check.py` stays green.

## Consequences

Contributors see one short rule file with pointers; operators keep
procedures in the existing docs without duplication. If a future audit
finds contributor-relevant operator detail missing from the pointers,
extend the pointer list — do not reintroduce procedure into `AGENTS.md`.

## ADR-013 — Unambiguous smoke probe with a bounded 4-request margin

**Accepted.** `mizu smoke --live` keeps its paid, read-only probe shape and
evidence checks, but the probe goal names the exact path (`probe.txt` in the
workspace root, single read, do-not-list-first) and the fixed request cap
moves from 2 to 4 (`SMOKE_REQUESTS_PER_RUN = 4` in `src/mizu/smoke.py`;
tools stay 5, wall-clock stays 120 s).

## Context

The probe capped spend at 2 provider requests with the goal `Read probe.txt
...`. On 2026-10-03 the contributor model listed files before reading
`probe.txt`: files plus read plus finish is 3 sequences, so the third
admission raised `Per-run provider request budget exhausted` and the live
smoke failed on `main`/`main-deep`. The instruction was ambiguous (no exact
path, no list-vs-read guidance), and the cap had no margin for one benign
extra step or one auxiliary Pi inference.

## Decision

- Mechanism provides capabilities; policy decides behavior. The probe goal
  wording, the fixed request/tool/time caps, and the exact-evidence check
  are mechanism/test vectors (ADR-006), not operator policy. Ideal model
  behavior (read directly vs list first) stays model judgment; the mechanism
  only makes the target unambiguous and budgets one benign extra step.
- Goal: `Read the file probe.txt in the workspace root (exact path
  probe.txt) with a single read. Do not list files first; the workspace
  contains only probe.txt. Then call mizu_finish alone with outcome 'wait'
  and summary exactly the file contents. Do nothing else.` The `mizu_finish
  alone` convention and the exact-summary evidence check are unchanged.
- Caps: `SMOKE_REQUESTS_PER_RUN = 4`, `SMOKE_TOOLS_PER_RUN = 5`,
  `SMOKE_RUN_SECONDS = 120`, applied as `min(fixed, operator limits)` so the
  probe never raises an operator budget. Four admits files plus read plus
  finish plus one auxiliary Pi request (Codex turns / Claude queries admit
  once per run, so they stay well inside the cap) without opening paid
  spend. No new config keys; unknown keys still fail.

## Schema, bounds, trust, retry/cancellation, evidence, failure

- Schema: no new interface. `smoke.live` still takes `(config, profile,
  role_name)` and returns the same pass/fail report shape.
- Bounds: at most 4 provider admissions, 5 tools, 120 s per probe run;
  single-file workspace (`probe.txt`); summary comparison is exact.
- Trust: paid opt-in probe only (`--live` consent); read-only consult role
  via the shared `check_consult_role` gate; never touches a real project.
- Retry/cancellation: single probe run under the existing deadline/stop
  handling; no retry in mechanism.
- Evidence: `tests/test_smoke_probe.py` pins the exact-path wording, the
  4/5/120 constants, the never-raise-operator-limit clamp, the
  files-plus-read-plus-finish-fits-4 scenario (and that the old cap of 2
  refused it), and the unchanged wrong-summary refusal.
- Failure: `Denied` on root, unconfigured role, non-consult role, or
  evidence mismatch, as before; `LimitExceeded` only when the fixed cap or
  an even lower operator limit is reached.

## Consequences

The probe passes when the model reads directly (2 sequences) and when it
lists once first (3 sequences), plus one spare for auxiliary inference,
while paid spend stays capped at 4. If future models need more benign
steps, revisit with measured probe traces — not hypothetical generosity —
per ADR-001. `check.py` stays green; stdlib-only; no model-specific
defaults.

## ADR-014 — Stable CI dedup body excludes the volatile log URL

**Accepted (M2 fix).** `vcs.record_ci_result` validates the optional `url`
(bounds: at most 4096 chars, no NUL/newline) but no longer persists it: the
insight body covers only branch/sha/check plus the `external-untrusted`
label, while the stable ID stays `ci-<32 hex>` from branch+sha+check. Repeats
with a varying per-run log URL therefore resubmit identical content and
return the existing record instead of raising `Denied: Insight ID was reused
with different content` and aborting the `poll_ci` tick. The latest log URL
stays available via `vcs_read` `status`; the single-writer insight invariant
is unchanged.

## Context

M2 shipped `ci_insight_id` from branch+sha+check with a body including
`log: {url}`. Any adapter emitting a varying log URL per run for the same
failing check hit the insight-store reuse guard on the second poll, and the
single outer `try` in `poll_ci` turned that `Denied` into `ok: false`,
skipping every remaining branch/check. Existing tests only repeated the
identical URL, so the path was uncovered (REVIEW 858aec2…: CHANGES; CI-FAIL
768cf614…).

## Decision

- Mechanism provides capabilities; policy decides behavior. The stable-body
  rule and the unchanged `poll_ci` best-effort contract are mechanism; which
  failures matter stays policy.
- No new interface, config keys, or stored records. `parse_status_checks`
  still validates `url`; only persistence changes.
- No per-check isolation added: with identical resubmits the tick stays
  `ok: true` for this path, and broader isolation would change `ok: false`
  semantics beyond the reported defect.

## Schema, bounds, trust, retry/cancellation, evidence, failure

- Schema: `record_ci_result(..., url="")` unchanged; body is now
  `CI check '<check>' failed on branch '<branch>' at <sha>.` plus
  `trust: external-untrusted`.
- Bounds: `url` still bounded (4096, no NUL/newline); `Denied` on violation.
- Trust: bodies stay `external-untrusted`; approval trust untouched.
- Retry/cancellation: none; single local submit per failure.
- Evidence: `tests/test_vcs_ci.py::test_varying_log_url_dedupes_and_poll_stays_ok`
  pins varying-URL dedup, body without URL, and a due poll staying `ok`.
- Failure: `Denied` only on bad branch/sha/check/url shapes; never on a
  repeated failure with a new URL.

## Consequences

CI polling deduplicates as ADR-009 promised; varying URLs no longer spam the
inbox or abort the tick. Operators needing per-run URLs use `vcs_read`.
`check.py` stays green; stdlib-only; no model-specific defaults.

## ADR-015 — Rootless Podman keeps the host uid via `--userns=keep-id`

**Accepted (M7).** Rootless (`mode = "rootless"`) Podman runs pass
`--userns=keep-id` alongside the existing `--user <host uid:gid>`, so the
host uid is kept inside the container and the bind-mounted workspace stays
readable with an empty user `containers.conf`. Docker runs omit the flag
(Podman-only mode); `single` mode is unchanged (`--uidmap 0:0:1`,
`--gidmap 0:0:1`, `--user 0:0`, no `userns` flag).

## Context

Rootless Podman without an explicit userns maps `--user <host uid>` to a
subordinate container id, so the workspace bind mount appears owned by
another id and `doctor --sandbox` (`real sandbox smoke`) fails with exit 1.
Operators worked around it with `userns = "keep-id"` in the user
`containers.conf`, which is operator state outside the auditable argv.
Passing `--userns=keep-id` per run moves the choice into the fixed
mechanism argv, where `doctor --sandbox` proves it under a systemd user
service with an empty `containers.conf`.

## Decision

- Mechanism provides capabilities; policy decides behavior. The extra
  `run` flag, its Podman-only gate, and the unchanged hardening floor
  (read-only root, cap-drop, no-new-privs, resource limits) are mechanism.
  Which runtime binary, image, network, mounts, and env to use stays
  operator policy in `[sandbox]`.
- No new config keys; unknown keys still fail. `sandbox.executable` with
  basename `podman` selects the flag; any other runtime (notably `docker`)
  runs with `--user` only. No operator knob per ADR-006.
- `single` mode keeps its single-ID mapping and never carries `userns`.

## Schema, bounds, trust, retry/cancellation, evidence, failure

- Schema: no new interface. `Sandbox._fixed_floor` appends
  `--userns=keep-id` after `--user` in rootless Podman argv only.
- Bounds: one literal flag value; no lengths, sizes, or timeouts involved.
- Trust: local argv construction from operator config, never model input;
  the flag does not grant host privilege beyond the invoking user.
- Retry/cancellation: none (argv construction); execution keeps the
  existing timeout/cleanup contract.
- Evidence: `tests/test_platform.py` pins Podman carries `keep-id` and
  Docker omits it; `tests/test_single_id.py` pins rootless Podman carries
  it and single mode omits it; `docs/setup.md` records the empty
  `containers.conf` outcome and the Docker note.
- Failure: an unknown future runtime simply runs without the flag (its
  own mapping applies); a Podman lacking `keep-id` fails loudly at run
  time through the existing startup-error record, never as a silent
  downgrade.

## Consequences

`doctor --sandbox` passes with an empty user `containers.conf` under a
systemd user service on rootless Podman; Docker behavior is documented and
unchanged. `check.py` stays green; stdlib-only; no model-specific
defaults.

## ADR-016 — Resume rebase plus infrastructure-wait deferral

**Accepted (M8).** `mizu resume` resets per-role `consecutive_failures` to 0
(keeping `last_run` evidence), and `Busy`/`LimitExceeded` failures defer
instead of counting toward the `max_failures` auto-pause brake. Error
records are still written and the exception still raised (daemon emits
`run_deferred`), so real faults stay visible.

## Context

`mizu resume` only cleared `paused`, leaving `consecutive_failures` at its
pre-pause value, so one further failure immediately re-paused (2026-10-04:
a UTC-day budget exhaustion plus a 15 s `podman ps` timeout left the
counter at 2; the next single failure hit 3 and paused). Pre-dispatch
slot/workspace locks and the daily-budget pre-check already defer outside
the run `try`, but in-run budget/lock waits (`Busy`, `LimitExceeded` from
admission, slots, or disk/budget guards) incremented the same brake as
model faults.

## Decision

- Mechanism provides capabilities; policy decides behavior. The resume
  reset, the `INFRA_WAIT = (Busy, LimitExceeded)` type gate, and the
  unchanged error/raise contract are mechanism. When to resume and what
  counts as healthy stay operator/worker policy.
- `Project.reset_health()` rewrites each `health/*.json` with
  `consecutive_failures = 0`, preserving `last_run`; `_cmd_resume` calls
  it before clearing `paused`. No new config keys; unknown keys still
  fail.
- `Engine.run` skips the health increment and auto-pause when
  `is_infra_wait(exc)` is true. All other exceptions (`Denied`,
  `ProtocolError`/`ModelFailure`, `OSError`, `ValueError`, plus
  `Cancelled` keeping its existing no-pause rule) count as before.
- No message parsing: the gate is the exception type, not string
  matching, so container-runtime `Denied` (missing runtime, failed
  smoke, cleanup failure) still counts; the resume rebase is its
  mitigation (one transient no longer re-pauses).

## Schema, bounds, trust, retry/cancellation, evidence, failure

- Schema: no new interface. `reset_health() -> {role: 0}`; `is_infra_wait`
  takes an exception only.
- Bounds: health dir `*.json` only; symlink records skipped.
- Trust: local operator state and local exception types, never model
  input.
- Retry/cancellation: none; resume is a single operator act, infra waits
  retry at daemon idle cadence via the existing `run_deferred` event.
- Evidence: `tests/test_resume_health.py` pins resume reset (counter 0,
  `last_run` kept, next single failure does not re-pause), budget/Busy
  deferral without counting or pausing (error.json still written), and
  real `ProtocolError` faults still counting to pause.
- Failure: `Denied` on unarmed resume as before; infra waits still raise
  (`Busy`/`LimitExceeded` codes unchanged) and still record `error.json`.

## Consequences

One transient after a resume no longer re-pauses; sustained real faults
still pause after `max_failures`, and daily-budget/lock contention waits
for the operator/day rollover instead of pausing. `check.py` stays green;
stdlib-only; no model-specific defaults.

## ADR-017 — Narrow infra-wait deferral to budget/disk guards; Pi budget refusal defers by type

**Accepted (M8 fix for CI-FAIL b1617770, ce093c1c).** `INFRA_WAIT` narrows from
`(Busy, LimitExceeded)` to `(Busy, InfraExceeded)`, and `Engine.run` defers via
`is_deferred(exc, context)` instead of `is_infra_wait(exc)` alone.

## Context

ADR-016 gated the consecutive-failure brake on the exception type
`(Busy, LimitExceeded)` with the stated intent of only "admission, slots, or
disk/budget guards" while claiming "all other exceptions count as before".
The type was broader than the intent: engine deadlines, event-stream evidence
bounds, RPC input deadlines, per-run tool/request bounds, file-count and
snapshot bounds all raise plain `LimitExceeded` and silently stopped counting
(REVIEW 2d067dac Finding 1; experiment 1eca27b4 showed five engine-deadline
failures leaving `consecutive_failures=None`). Separately, the M8 acceptance
"budget exhaustion defers" was only demonstrated for the in-process
`LimitExceeded` path: on the Pi transport a host-side `_budget` refusal becomes
`{"ok": false}` on the bridge, the adapter collapses, and the host surfaces
`ProtocolError` (or `ModelFailure` for a real SDK `stopReason=error`), neither
of which deferred (Finding 2; experiment 8d5cebee).

## Decision

- Mechanism provides capabilities; policy decides behavior. The narrowed gate,
  the new `InfraExceeded` type, and the host-side `admission_wait` flag are
  mechanism. When to resume and what counts as healthy stay operator/worker
  policy.
- New `errors.InfraExceeded(LimitExceeded)`: deferrable daily-budget and
  disk-reserve guards only. It subclasses `LimitExceeded` so existing
  `except LimitExceeded` admission handling still catches it. `Budget.take`
  (disabled, exhausted, day-file bound) and the `Engine.run` pre-dispatch
  daily-budget/disk-reserve checks raise it. Every other `LimitExceeded` site
  (engine deadline/event bound, RPC deadline, per-run tool/request bounds,
  runtime-usage bound, workspace file-count, snapshot/file bounds) stays plain
  `LimitExceeded` and counts toward the brake.
- `Busy` stays fully deferrable (all `Busy` sites are lock/slot waits).
- `Context.admission_wait` (default `False`) records an in-run host-side
  budget refusal by type: set only on the `InfraExceeded` path in
  `Context.handle("_budget")` and `drivers.admit_invocation`, never by message
  text. `is_deferred(exc, context)` defers when `is_infra_wait(exc)` holds, or
  when `admission_wait` is true and `exc` is `ProtocolError`/`ModelFailure`
  (the Pi transport collapse shape). A `ModelFailure` arriving with
  `admission_wait` is normalized to `InfraExceeded` before the brake so the
  gate stays type-based.
- No message parsing anywhere; no new config keys; unknown keys still fail.

## Schema, bounds, trust, retry/cancellation, evidence, failure

- Schema: `InfraExceeded(LimitExceeded)`; `is_deferred(exc, context=None)`;
  `Context.admission_wait: bool`.
- Bounds: none beyond the existing limit values.
- Trust: local exception types plus one host-side boolean, never model input.
- Retry/cancellation: unchanged; infra waits retry at daemon idle cadence via
  the existing `run_deferred` event.
- Evidence: `tests/test_resume_health.py` pins the narrow gate (budget
  defers, engine-deadline `LimitExceeded` counts to pause), the Pi-channel
  budget refusal through `tests/fake_pi.py` deferring without counting, plus
  the existing resume/Busy/real-fault pins.
- Failure: infra waits still raise (codes unchanged) and still record
  `error.json`; non-wait faults count and auto-pause exactly as before.

## Consequences

The runaway-model brake is restored for engine/local bound faults while daily
budget, disk reserve, and lock contention wait instead of pausing; the Pi
incident path (in-run budget exhaustion) now defers on every engine. `check.py`
stays green; stdlib-only; no model-specific defaults.
## ADR-018 — Operator-channel publication approval

**Accepted.** `GO <branch>` approvals count only with insight source
`operator` and `accept` decision run `operator` (`mizu insight decide`);
model submits/decides are refused as forged. Configs combining
`vcs_publish` with `submit_insight`/`decide` fail to load.

## Context

Model `submit_insight`/`decide` could mint a `GO` approval and accept it.
No operator `decide` path existed.

## Decision

- Mechanism: source/run gate in `require_go_approval`, config separation,
  `mizu insight decide` (run `operator`).
- Model run names are directory hex, never `operator`; source is runtime-set.

## Evidence / failure

- `tests/test_vcs_publish.py`: forged submit/decide refused, separation refused.
- `Denied` (`operator channel` / self-approval); `check.py` green.

## ADR-019 — Digest-bound publication adapter

**Accepted.** `vcs_publish` sends the approved `code_digest` as `digest`;
the adapter verifies the pushed tree before pushing and echoes it back.
Missing/mismatched echoes are `Denied`.

## Context

A `GO` approval bound the host check but not the pushed bits.

## Decision

- Mechanism: `publish_via` requires `code_digest`, sends `digest`, checks echo.
- Adapter contract: verify-then-push, echo `digest` (documented in
  `docs/configuration.md`).

## Evidence / failure

- `tests/test_vcs_publish.py`: echo, mismatch, missing, param-required.
- `Denied` on missing/mismatch; `check.py` green.

## ADR-020 — Reserved role names keep the operator channel unforgeable

**Accepted.** Config load refuses role names `operator`, `vcs`, `editor`.

## Context

Model `submit_insight` fixes source to the role name, so a role named
`operator` could author a `GO` body a later human accept would record as
operator-authored (REVIEW finding on ADR-018).

## Decision

- Mechanism: `RESERVED_ROLE_NAMES` in `config.load`; `ConfigError` at load.
- No new keys; run names stay hex vs `operator`.

## Evidence / failure

- `tests/test_vcs_publish.py`: reserved names refused at load.
- `Denied`/`ConfigError` keep the approval-binds-pushed-bits invariant.
## ADR-021 — Bounded opt-in daemon VCS polling

**Accepted.** Daemon upstream/CI polling needs `vcs.poll_enabled = true`
plus a command; cadence is `vcs.poll_interval_seconds` (15-86400, 300).
One project polls once per tick via `vcs-poll.lock` plus `vcs-poll.json`;
a tick is at most one 30 s fetch plus 4 branches at 15 s each.
