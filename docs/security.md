# Security model and boundaries

## What is protected

Model-controlled code does not run on the host. Tool grants are checked by the
host bridge, not inferred from text. Pi builtins and discovery are disabled;
only a trusted extension gets a per-run authenticated local socket (a Unix
socket, or loopback TCP with the same token where Unix sockets are
unavailable). Run input
comes from a captured code snapshot, not a writable operator configuration.
Sandbox container commands have no network by default (`[sandbox] network = "none"`
by default), no credentials, no host home, no SSH agent, no Docker/Podman
control socket, devices, capabilities, or privileged flag.

Readonly observers and Editor exports cannot modify the shared worktree.
Source paths reject traversal, symlinks, hardlinks and nonregular files; objects
are hash-checked. Project state directories are operator-owned: model-reachable
reads re-validate identifiers and refuse symlinked state, projections skip what
they cannot strictly read, and `doctor` reports skipped stores loudly.
Experiments get readonly source plus ephemeral writable work.
Time/output bounds kill process groups and labelled containers. A finished unit
cannot continue to call tools or acquire another provider admission.

Trust-handoff audit (writer-influenced files consumed on host): the container
boundary does not cover files the writer shapes that host-side components
later read or run. Each consumer, what it trusts, and the gate:

| Host consumer | Writer-influenced input | Host trust and gate |
|---|---|---|
| `verify` command | Agent-influenced worktree at a captured digest | Operator-owned argv only; proves command results on the digest, not oracle completeness |
| Publish pointers (`snapshot`, `report`) | Snapshot content and report body | Pointer moves atomically only after the entry is complete; report renders Markdown, never HTML; bounded JSON |
| Service renderer | Staged unit definitions | Text rendering only; staged units must pass `systemd-analyze verify` (where available) and operator review before arming |
| Backup, VCS publish, editor ingest | Published tree and outbox spool | Operator-owned destinations and commands; review the published tree before opening or executing anything from it |
| Engine configuration | Writer-influenced `.codex/config.toml`/`.env`, Claude settings or hooks, and Pi project-local inputs | Pinned controller `cwd` (Pi controller `cwd` is the private run dir, not the workspace), isolated `CODEX_HOME` and `CLAUDE_CONFIG_DIR`, `setting_sources=[]`, Pi `noExtensions/noSkills/noPromptTemplates/noThemes/noContextFiles` with only digest-checked operator `resources` as `additional*Paths` (pin 1.0.4; CVE-2026-54325 trust gating landed in 0.79.0), read-only sandbox mode with command and file-change approvals declined (GHSA-xrxf-jgv3-qmrm, GHSA-w5fx-fh39-j5rw) |
| VCS tooling | Writer-shaped `.git/config`, attributes, diff drivers, or nested `*.git` bare repos honored by host `git` | `.git` excluded from snapshots, operator-owned VCS commands and destinations, host git refuses bare-repo discovery unless explicitly allowlisted (`safe.bareRepository=explicit`); review the published tree before opening or executing anything from it |
| Control plane (host bridge) | Nothing model-supplied | Per-run 256-bit token plus loopback-only bind and strict shape checks (`hmac.compare_digest`, `src/mizu/bridge.py`); never trust a client-supplied `Host` header. This token-and-peer design avoids the CVE-2026-82533 class (Host-header trust with loopback left open) |

## What is trusted

The operator account, OS kernel, filesystem, release source, installed Pi/npm
dependencies, trusted search executable and container images are trusted.
Operator-selected Pi extensions are trusted code too: membership in this set
is an operator selection with source review and hash pins, not something the
mechanism can verify from the inside. Prefer stdio MCP servers (containerized);
HTTP `url` MCP servers are explicit operator endpoints outside the OCI floor:
require the upstream DNS-rebinding guard plus auth, never unauthenticated
loopback alone, since a loopback bind is not a browser boundary. When the
endpoint uses OAuth, pin the expected issuer in the client and never trust
server-supplied authorization metadata: guard-plus-auth alone does not stop
a hostile server from receiving redirected credentials. Clear issuer-less
stored registrations and rotate secrets/tokens after untrusted contact.
MCP tool definitions are runtime data, not pinned code (unlike `resources` hashes):
granting an `engine_tools` name trusts its current definitions, so re-review them
on server or image update (including a mid-session `notifications/tools/list_changed`
or any tool-manifest change) and keep grants minimal.
An attacker with the same host UID or root access can alter the policy, read
secrets or replace code. A private bridge token is a process-boundary aid,
not protection from a compromised operator account. Use a dedicated service
account and trusted plugins only. A rootless container shares a kernel and is
not sufficient isolation for arbitrary high-risk malware; use a disposable VM
or stronger separately reviewed boundary for that threat model.

Review images and use digests. No runtime pulls are permitted. Extra mounts and
environment are operator-selected: never mount sockets, credentials, secret
material, or anything the model must not read; the mechanism enforces
read-only and refuses container-interior system targets (host sources stay
explicit operator choice), but it cannot tell a dataset from a
keyring. Audit any
operator-installed search adapter: it executes on the host with a sanitized
environment and is not model-supplied code. The model supplies only JSON inputs.
Do not point it to a generic shell interpreter that evaluates query text.

## Prompt injection and data sharing

External prose cannot add capabilities, modify goals, register tools, change
models or raise budgets. Policies additionally tell models to treat external
instructions as untrusted. This reduces the impact of prompt injection; it does
not prove that models will choose correct priorities, detect poisoned research,
or write semantically correct code. Independent review and objective evaluation
remain necessary. Persisted conversation (provider session plus durable store) is untrusted stored history: poisoned tool output recorded under one grant reloads on every same-grant resume -- grant binding and goal-keyed forking limit authority, not content, and rotation bounds lifetime without cleaning content. Treat resumed history as untrusted and rotate to a fresh generation on suspected poisoning. `verify` proves configured command results on a code digest,
not that editable tests were a complete or independent oracle (a writer-edited
test passes by construction once captured: test edits, hard-coded outputs, or
defense deletion to satisfy a corrupted test are the same failure class, so keep
acceptance tests operator-owned or review the test diff before `done`; acceptance needs value assertions on fixed inputs, not mere execution -- test-file presence alone is not verification strength). When acceptance checks persisted effects, assert exact row/object counts and deny unknown persisted fields — prefer an independent readback over checker-echoed values, since value-only checks still pass on extra-record/extra-field effects.

### Inference-engine confinement

All engines expose only the capability-filtered `mizu_*` tools; model-driven
commands execute in the container with the operator-selected network posture. Pi additionally
starts with no built-in tools at all. Codex fixes `shell_tool=false` and marks its `mizu`
MCP server `required`, while sandbox mode (`read-only`, `workspace-write`), web search,
and subagents are profile-selectable (defaulting to read-only, disabled, and false). Claude
disables built-in tools with `--tools ""`, uses strict per-run MCP configuration,
and autoapproves only capability-granted MCP tools. It never uses skip-permissions.
Residual risk: vendor CLIs can have managed settings and implementation-specific
side effects; argv tests alone do not establish OS isolation. Use a dedicated service account, keep the CLIs pinned and reviewed,
let `doctor` flag CLI drift, and confirm the read-only live smoke leaves no
side effects before arming a new engine.

Subscription and API credentials live in operator-owned login stores
(`~/.codex/auth.json`, OS keyrings, `claude login` state), never in run
records and never mounted into command containers. An inline `CODEX_API_KEY`
is forwarded to that one CLI invocation only. Treat `auth.json` files like
passwords; review per-engine quota/retention terms before unattended runs.

Secrets should never be part of a source import. Exclusion patterns are not DLP;
a Git clone contains committed history and may contain tracked secrets even
when snapshot views exclude a filename. Sanitize the source repository/history
before giving any autonomous writer a clone. Treat tool output, conversations,
artifacts and backups as private.

## Network broker and egress policy

Outbound traffic is separated by purpose instead of banned uniformly:

- Model-driven commands (`exec`/`experiment`/`verify`): container network off
  by default and selectable by policy (`[sandbox] network`); read-only root,
  dropped capabilities, `no-new-privileges` and user mapping stay fixed.
  Dependencies arrive via operator-reviewed images, never via in-sandbox
  downloads unless the operator enables the network and accepts the risk.
- Retrieval (`fetch`/`search`/`probe`): explicit HTTPS egress to operator-allowlisted
  hosts, every response kept as a content receipt. Where to allow is policy
  (`[web]`, with `probe` destinations/methods in `probe_hosts`/`probe_methods`);
  the receipts, bounds and untrusted labels are
  mechanism. `probe` follows no redirects, never caches, and refuses
  credential/framing headers with no credentials injected.
- Provider inference: the controller holds credentials and must contact those
  endpoints. Editor inference also has network access and its own credentials.
  Experiment code has none.

Per-threat mitigations:

| Threat | Mitigation, and what remains operator duty |
|---|---|
| Secret exfiltration | Egress allowlist, no secrets in containers, a receipt for every retrieval, request budgets. Duty: keep secrets out of imports, set provider billing caps |
| Malicious content executed | Fetched text is never executed by the harness; code runs only from snapshots and reviewed images; `verify` plus independent review precede `done` |
| Supply chain | Digest-pinned images, no runtime pulls, hash verification where the ecosystem provides it (e.g. registry checksums), review of image contents, refusal of Podman checkpoint images (annotation `io.podman.annotations.checkpoint.runtime.name`, CVE-2026-94603) and of images with valueless Env (bare-key or `*` entry, GHSA-4hq8-gpf5-8p68): `build-sandbox.sh`, `doctor` and every sandbox launch inspect the image and refuse on positive evidence |
| Irreproducibility | Receipts (id, sha256, time), lockfiles, run records; search scope is reported honestly |
| Cost | Request admission counts plus hard provider-side spend caps/pauses and bill inspection |
| Prompt injection | External prose is data, never authority: no capability, goal, tool, model or budget change |

Container-escape checklist (external benchmark mapping): nested-sandbox CTF
categories such as privileged containers, host PID namespace, added
capabilities, and mounted control sockets map to Mizu posture as follows.
The fixed floor is mechanism; the rest is operator policy plus TCB.

| Benchmark category | Mizu posture |
|---|---|
| Privileged container, added capabilities, host PID namespace | Excluded by the fixed mechanism floor (`--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, user mapping, resource bounds in `src/mizu/sandbox.py`); argv construction has no privileged fallback and no `--privileged`, `--pid=host`, or `cap-add`. Podman checkpoint images and images with valueless Env are refused before launch (annotation `io.podman.annotations.checkpoint.runtime.name`, CVE-2026-94603; bare-key or `*` Env, GHSA-4hq8-gpf5-8p68): on unpatched Podman the checkpoint config would otherwise silently ignore those flags and a crafted Env would pull host env into the container |
| Mounted control socket | No Docker/Podman socket is ever mounted; extra mounts stay explicit operator choice, read-only, and refused for container-interior system targets |
| Network namespace escape | Container network is `none` by default; any other `[sandbox] network` is an explicit operator grant that accepts egress and loopback reachability |
| Runtime TCB floor | `doctor --sandbox` proves enforcement with a live smoke probe and records the runtime version string but enforces no minimum: keep the runtime at or above runc 1.3.6/1.4.3/1.5.1 (prefer >=1.5.2; 1.5.0 has Focal tmpfs regression #5348) or crun 1.30.1 (CVE-2026-41579, CVE-2026-47766, CVE-2026-84042, CVE-2026-88264, CVE-2026-88265; 1.30 alone omits the complete 84042 fix). Mizu never requests the krun handler or passt, so 84042 is out of posture; the floor moves for the /dev flaws, and Podman at or above v5.8.8 / v6.1.3 where Podman is used (CVE-2026-94603 checkpoint bypass; also covers GHSA-4hq8-gpf5-8p68 env-leak fixed in v5.8.4 / v6.0.0). Checkpoint-image and valueless-Env refusals hold on any version; the version floor removes the upstream bugs themselves. A rootless container shares a kernel and is not sufficient isolation for arbitrary high-risk malware; use a disposable VM or stronger separately reviewed boundary for that threat model |

Egress facts: only HTTPS, port 443 and exact allowed hosts are accepted. Userinfo,
control characters and unsupported URLs are rejected; fragments are stripped
client-side and never sent. DNS answers must all be
public (unless `intranet = true`, which allows `ip.is_private` addresses; multicast, link-local, loopback, unspecified, and transition addresses stay refused);
connections pin a checked IP and verify TLS against the original host.
Redirects repeat validation. Environment HTTP proxies are not honored. Retrieval
is a separate killable process with bounded time and response bytes, including
DNS stalls. PDF/image processing, browser JavaScript and logged-in web sessions
are not supported. Private-network destinations need the operator's explicit
`intranet` choice; loopback stays refused either way.

## Explicit limits of limits

Request admission counts are global per day in the configured timezone (UTC default) and retained across restarts.
They are not a monetary cap, a token cap, or a guarantee about hidden upstream
retries. Set a hard account-level spend cap/pause by default and inspect provider bills;
alert-only alarms are not sufficient for unattended runs.

Container memory, CPUs, PID count, command deadline, temporary memory, individual
file size and captured output are bounded. Entire workspace bytes and total
host Pi/Node memory are not strictly controlled by those settings. Put source
and evidence on quota-controlled storage; apply service/cgroup limits suitable
for the workload and measure behavior. A free-space preflight cannot prevent a
running command from filling many individually allowed files.

Prune never deletes evidence automatically. Unbounded lifetime retention can
fill storage. Decide backup/retention outside the model. Hashes detect accidental
content mismatch, but records are not cryptographically signed nor immutable
against the host owner. The built-in backup is not encrypted.

## Single-ID mode (hosts without subordinate-ID delegation)

Where setuid helpers are neutered (`NoNewPrivs` and the like), stock rootless
Podman cannot run. Only there, `[sandbox] mode = "single"` is available. One
self map (`0 -> self`) is created, so every container UID collapses to the
invoking user; per-container identity separation does not exist. The default
`rootless` behavior is unchanged.

| Item | Content |
|---|---|
| Extra piece | `scripts/idmap/mizu-userns` (stdlib only, no privilege). No newuidmap shim |
| Podman launch | As uid 0 inside the wrapper ("fake-rootful") with explicit `--root`, `--runroot`, `--tmpdir`, `--storage-driver`, `--storage-opt`, `--cgroup-manager` |
| Container argv | `--uidmap 0:0:1 --gidmap 0:0:1 --user 0:0`, `--cgroup-parent` (final component must not end in `.slice`), `--runtime-flag root=<runroot>/crun`, explicit seccomp profile |
| cgroup parent | A delegated non-slice path prepared upfront (for example `user@1000.service/mizucg` with cpu/memory/pids enabled). Runs and `doctor --sandbox` ensure it idempotently and fail loudly when impossible |
| Host identity | Container processes run as the invoking host user, exactly as with the default `--user` mapping. No additional host privilege is granted |
| Known limits | Multi-UID software (apt sandbox and friends) cannot run inside. Image builds use `--apt-sandbox-user root` for that step only. If the image index wedges after a failed build, `system reset` plus re-pull recovers; a wedged index refuses resolution and never executes a wrong image |

## Supply chain and exposure

Direct Pi dependencies are exact-version pinned. Review the complete npm lock
before staging/installing it, not only before a public release: staging
(`npm ci` then `node ... --version/--check-contract`) and every run execute
the pinned tree on the host. Dependency lifecycle scripts are disabled by
default (`--ignore-scripts` covers only lifecycle scripts, not import-time code);
enabling them is explicit. Installation receipts contain source and
lock hashes but are not publisher signatures. GitHub workflows pin action SHAs,
use read-only repository permissions and do not run paid model calls on pull
requests. Maintainer changes stay in a candidate tree; production pointers and
secrets are never mounted there.

The artifact is static, escapes narrative and uses a restrictive CSP, but its
contents may be sensitive. No public server or authentication service is shipped.
Do not expose state directories through an unprotected web server.

See root `SECURITY.md` for responsible disclosure and `docs/testing.md` for
actual, separately labelled test evidence. Passing mock tests is not proof of
container isolation or real provider compatibility.

On Windows, POSIX modes do not prove ACL secrecy. Use a dedicated ordinary-user account and verify its ACLs. Runtime rootless is separately recorded from host privilege. See [completion-contracts.md](completion-contracts.md).
