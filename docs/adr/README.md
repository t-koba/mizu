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
