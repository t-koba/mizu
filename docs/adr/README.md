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
Its only extension talks to the host capability bridge. Model commands execute
in rootless Podman with fixed mounts and limits. Editor is a separate capsule.
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

- Mechanism: capability checks, sandbox argv and networklessness, atomic
  publication pointers, content hashes, budget/slot admission, failure
  brakes (auto-pause, lock refusal, unarmed refusal, no self-promotion),
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

## ADR-007 — Generic inference-engine abstraction (pi/codex/claude)

**Accepted.** One driver contract, many CLIs. No per-model ad-hoc features.

- Profiles gain `engine="pi"|"codex"|"claude"` (default `pi`; backward
  compatible). `provider/model/thinking` stay exact IDs verified at runtime.
  Trusted commands are operator-owned argv arrays only
  (`pi_command` pattern extended to `codex_command`/`claude_command`); no
  shell strings, no model-supplied host commands.
- Driver interface is `execute(context, prompt, profile)` one point
  (`docs/extensions.md`). Every engine guarantees: empty control cwd,
  system prompt = role policy verbatim, tools = capability-filtered `mizu_*`
  only, exact model-identity check before paid use, shared
  budget/slot/deadline/cancellation admission, sealed `finish`, usage
  records, session key = role/model/goal/policy/caps.
- One shared MCP bridge (private per-run socket/token, `required=true`
  fail-closed). Pi uses the Unix-socket extension; codex/claude use stdio
  MCP config pointing at the same bridge. Capability matrix per engine is
  documented, not branched in policy prose: writer `exec/experiment/verify`
  always runs in the networkless Podman sandbox; Codex runs with
  `shell_tool=false`, `web_search=disabled`, `multi_agent=false`; Claude
  allows `mcp__mizu_*` only. `agy` is excluded (no per-run system/MCP
  pinning, global-config mutation, non-TTY risk, unclear automation terms).
- Auth is operator-owned and out of band (`login` flows, `CODEX_HOME`,
  keyring/file); secrets never enter containers or logs. Subscription caps
  (shared 5h/weekly, message quotas) are account-level regardless of route:
  no SLA claim, rate-limit maps to `wait` then auto-`pause`, no admission
  refund, no silent model swap (ADR-005).
- CLI versions are pinned with `adapters/*/compatibility.json` + installer
  and doctor pin checks, same minimum-version policy as Pi >=0.87.1. Default behavior stays Pi;
  new engines are opt-in per profile. Fake drivers cover contracts offline;
  live provider gates stay separate.
