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
