# Security model and boundaries

## What is protected

Model-controlled code does not run on the host. Tool grants are checked by the
host bridge, not inferred from text. Pi builtins and discovery are disabled;
only a trusted extension gets a per-run authenticated Unix socket. Run input
comes from a captured code snapshot, not a writable operator configuration.
Rootless Podman commands have no network, credentials, host home, SSH agent,
Docker/Podman socket, devices, capabilities or privileged flag.

Readonly observers and Editor exports cannot modify the shared worktree.
Source paths reject traversal, symlinks, hardlinks and nonregular files; objects
are hash-checked. Project state directories are operator-owned: model-reachable
reads re-validate identifiers and refuse symlinked state, projections skip what
they cannot strictly read, and `doctor` reports skipped stores loudly.
Experiments get readonly source plus ephemeral writable work.
Time/output bounds kill process groups and labelled containers. A finished unit
cannot continue to call tools or acquire another provider admission.

## What is trusted

The operator account, OS kernel, filesystem, release source, installed Pi/npm
dependencies, trusted search executable and container images are trusted.
An attacker with the same host UID or root access can alter the policy, read
secrets or replace code. A private Unix socket token is a process-boundary aid,
not protection from a compromised operator account. Use a dedicated service
account and trusted plugins only. A rootless container shares a kernel and is
not sufficient isolation for arbitrary high-risk malware; use a disposable VM
or stronger separately reviewed boundary for that threat model.

Review images and use digests. No runtime pulls are permitted. Audit any
operator-installed search adapter: it executes on the host with a sanitized
environment and is not model-supplied code. The model supplies only JSON inputs.
Do not point it to a generic shell interpreter that evaluates query text.

## Prompt injection and data sharing

External prose cannot add capabilities, modify goals, register tools, change
models or raise budgets. Policies additionally tell models to treat external
instructions as untrusted. This reduces the impact of prompt injection; it does
not prove that models will choose correct priorities, detect poisoned research,
or write semantically correct code. Independent review and objective evaluation
remain necessary. `verify` proves configured command results on a code digest,
not that editable tests were a complete or independent oracle.

### Inference-engine confinement

All engines expose only the capability-filtered `mizu_*` tools; model-driven
commands still execute only in the networkless Podman sandbox. Pi additionally
starts with no built-in tools at all. Codex runs read-only with shell, web
search and subagents disabled and its `mizu` MCP server `required`. Claude
receives an explicit `mcp__mizu__*` allowlist and never a skip-permissions
flag. Residual risk: unlike Pi, vendor CLIs keep their own host tools behind
their own permission systems, so confinement there is configuration, not
absence. Use a dedicated service account, keep the CLIs pinned and reviewed,
let `doctor` flag CLI drift, and confirm the read-only live smoke leaves no
side effects before arming a new engine.

Subscription and API credentials live in operator-owned login stores
(`~/.codex/auth.json`, OS keyrings, `claude login` state), never in run
records and never mounted into command containers. An inline `CODEX_API_KEY`
is forwarded to that one CLI invocation only. Treat `auth.json` files like
passwords; review per-engine quota/retention terms before unattended runs.

Configured providers can receive the project context that tools expose. The
controller holds provider credentials; it must contact those endpoints. Editor
inference also has network access and its own credentials. The web broker has
explicit HTTPS access with host/IP/redirect checks. Experiment code has none.
Secrets should never be part of a source import. Exclusion patterns are not DLP;
a Git clone contains committed history and may contain tracked secrets even
when snapshot views exclude a filename. Sanitize the source repository/history
before giving any autonomous writer a clone. Treat tool output, conversations,
artifacts and backups as private.

## Network broker

Only HTTPS, port 443 and exact allowed hosts are accepted. Userinfo, fragments,
control characters and unsupported URLs are rejected. DNS answers must all be
public; connections pin a checked IP and verify TLS against the original host.
Redirects repeat validation. Environment HTTP proxies are not honored. Retrieval
is a separate killable process with bounded time and response bytes, including
DNS stalls. This implementation deliberately does not support intranet fetch,
PDF/image processing, browser JavaScript or logged-in web sessions.

## Explicit limits of limits

Request admission counts are global per UTC day and retained across restarts.
They are not a monetary cap, a token cap, or a guarantee about hidden upstream
retries. Set account-level spending limits and inspect provider bills.

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

Direct Pi dependencies are exact-version pinned. A complete npm lock must be
reviewed before a public release. Dependency lifecycle scripts are disabled by
default; enabling them is explicit. Installation receipts contain source and
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
