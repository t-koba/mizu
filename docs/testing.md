# Testing and acceptance

## Layered evidence

| Layer | Command | Proves | Does not prove |
|---|---|---|---|
| Offline core | `python3 scripts/check.py` | Files, locks, budgets, failure paths, JSONL, grants, MCP, escaped reports, bounded process behavior | Real Pi/provider/container behavior |
| Installer | `./scripts/test-install.sh` | Core staging, spaces, idempotence, manifests, CLI import/export/report/backup/restore | npm registry/install or model execution |
| Dependency install | `./scripts/setup.sh` | Locked Pi dependency install and local staging gates | Live credentials or isolation |
| Installed SDK / mock provider | `node scripts/check-pi-sdk.mjs` and `python3 scripts/check-model-adapters.py --help` | Current public SDK, runtime metering, managed bridge/MCP, Codex/Claude resume and usage deltas | Paid inference or OCI isolation |
| Real isolation | `mizu doctor --sandbox` | Actual rootless command boundary on that host | VM-grade isolation or kernel security audit |
| Live protocol (pi) | `mizu smoke --live` (exact-path `probe.txt` goal plus fixed probe policy, fixed 4-request / 5-tool / 120 s cap) | Exact model selection, trusted extension handshake, admission hook, read + finish + settled | Research quality, all providers, large codebases |
| Live protocol (codex/claude) | `mizu smoke --live` with an `engine = "codex"`/`"claude"` profile | Trusted argv, isolated home/config, required MCP bridge, budget admission, finish round trip, no side effects | Host-tool absence (configuration, not proof), all models, quotas |
| Real work unit | Demo `run` with verification | Write, command execution, verification and publication together | Long-term reliability |
| Soak | Operator-observed 24-hour deployment | Behavior over the tested workload/window | General availability SLA |

`tests/fake_pi.py` and fake runners in `tests/test_drivers.py` are contract test doubles, not production fallbacks. Tests verify argv construction, stdio MCP framing (`mizu.mcp_proxy`), and error handling without contacting live endpoints. Node tests use a local Unix socket with a simulated extension registry.

## Offline test execution (Linux / macOS / Windows)

```sh
python3 -m unittest discover -s tests -q
node --test tests/bridge.test.mjs
python3 scripts/check.py --report private-validation/offline.json
```

On Linux:

```sh
./scripts/test-install.sh
```

`scripts/check.py` writes a versioned offline receipt (`report_version`,
`mizu_version`, `source_sha256` over contents plus executable bits, exact
`python`/`os`, exact `node_version`, canonical sorted `allowed_not_run` from
the CLI, functional `bash_available`, `ok`, and the full `checks` array). The
source digest is captured before validation and compared again after all checks;
`source-manifest-stable` must pass, otherwise the report fails. JavaScript
syntax check names include source-relative paths to avoid basename collisions.

The gate's own stdout is bounded. Every step's output is relayed live with
its arrival time, but the relay draws on one shared budget (49 152 bytes)
and keeps a bounded tail (16 384 bytes) of the lines that budget no longer
covers, so the gate's output cannot grow with the number of tests the suite
runs. A child's summary and failure text sit at the end of its output, so
the tail keeps them visible, and the receipt keeps the structured outcome
either way. A step whose own output the capped relay hid (the steps that
follow the one that spends the budget) carries the tail of its output in the
receipt's `details`, so a failing step still names why it failed. The
harness captures a command's stdout and stderr under one cap
(`limits.output_bytes`, 262 144 bytes by default), so a gate whose output
grew with the suite was killed by its own harness before it could report its
own outcome.

The receipt the gate prints is bounded the same way. Its step entries carry
the tail of each failing step's own output, so the printed receipt would
grow with the number of failing steps; the steps' free text draws on one
shared budget (16 384 bytes), and the tail-visible failure summary that
follows it draws on its own (8 192 bytes), spent on the last failures first
because the summary is the view a truncated log keeps. The step names, statuses, exit
codes and counts are never shortened, so the gate always names every step
that failed, and the receipt written by `--report` keeps the full text of
every step. When the printed receipt is shortened, the gate says so on its
own output.
Core-only installation may reuse that receipt instead of running the nested
staged gate a second time, only via an explicit handoff:

```sh
python3 scripts/check.py --report private-validation/offline.json --allow-not-run node-durable-contract-tests,node-pi-sdk-contract-tests,node-pi-reasoning-replay-tests
./scripts/test-install.sh private-validation/offline.json
```

Reuse is explicit and content-bound: `scripts/install.py --reuse-source-checks`
is allowed only with `--core-only`, binds to the exact shared distribution
source-manifest digest, exact Python version and OS, the versioned report
schema, `ok: true`, passing `python-syntax`, `python-unit-and-contract-tests`,
`node-transport-and-extension-tests`, `source-manifest-stable`, and the source
tree's JavaScript/shell syntax checks. All three Pi SDK gates must be present,
with either `pass` or an allowed `not_run` result: a real SDK pass is accepted.
The requested `allowed_not_run` must be sorted, unique and contain only names
from `staging_not_run_checks(core_only=True)`. Every skipped SDK gate must be
explicitly declared; a fully passing report with no waivers can be reused
without repeating its checks.
The current Node version and functional Bash availability must also match the
report. No other `not_run` is accepted except generic `shell-syntax` when the
report records no functional Bash. Duplicate check names are rejected. Explicit
reports must be regular files of at most 1 MiB, read boundedly before parsing.
Stale, malformed, failed, incomplete, wrong-environment, or unexpected-waiver
reports are rejected, and the staged-manifest equality check still runs. Without an
explicit receipt the installer runs its full staged gate unchanged. The
installation receipt records `checks_provenance` as `reused-source-gate` or
`staged-gate` so reused evidence is never presented as having run inside the
staged tree. Never substitute the general `offline.json` receipt implicitly;
the CI core-installer step passes the prior report path explicitly, while a
plain local `./scripts/test-install.sh` still executes the staged gate.

On Windows without bash: run `python scripts/install.py --core-only ...` and test via `bin\mizu.cmd`. Missing bash marks shell-syntax checks as `not_run`; missing Node fails the suite. The Pi SDK, durable-contract, and reasoning-replay gates need the pinned adapter dependencies: absent dependencies fail closed with the provisioning remedy unless their exact gate is explicitly waived; a version mismatch always fails. Installer staging records mode-specific `not_run` waivers: core-only stages waive the Pi SDK, durable-contract, and replay gates because neither adapter is provisioned; full Pi stages run the Pi SDK contract gate against the installed `adapters/pi` and waive only durable-contract and replay, because `pi-durable` is not installed. The required `durable-contract` CI job provisions both pinned adapters from committed lockfiles in a network-enabled setup step, then runs all gates unwaived and offline; the CI matrix and installer-staging waivers are scoped fallbacks, not substitutes for that authoritative job.

The gate installs the pinned dependencies itself when they are absent: it runs
`npm ci --offline --engine-strict --no-audit --no-fund --ignore-scripts` with
`npm_config_offline=true`, so the install never reaches the network. A frozen
source copy -- the input of a check job -- carries no `node_modules`, so
without this the SDK gates fail closed on absent dependencies and the
acceptance command has to be waived. The install happens only for a gate that
will actually run (a waived gate needs no dependencies) and only over an
adapter that has no dependency tree at all: `npm ci` is the clean install and
replaces the whole tree, so it never runs over a tree the operator
provisioned. An adapter that holds some of the pinned packages is left exactly
as it is and fails closed, naming the packages that are missing; an adapter
that holds none of them is absent, and the install does not run over the tree
it has either. An installed version that differs from the pin is a contract
violation, never reinstalled over. A missing npm, a missing lockfile, a failed install or an elapsed 300 s
bound leaves the gate's verdict unchanged and the previous installation
untouched, and names npm's own output.

The install resolves from npm's own cache, as selected by `npm_config_cache`
(npm's default is `~/.npm`). That cache is the mechanism's input and must hold
the tarballs of the committed lockfile; a cache that does not hold them fails
the install with npm's own `ENOTCACHED`. A sandboxed check needs the cache
visible inside the sandbox: mount the cache directory and name it in the
environment, for example:

```toml
[[sandbox.mounts]]
source = "/srv/npm-cache"
target = "/npm-cache"

[sandbox.env]
npm_config_cache = "/npm-cache"
```

`python3 scripts/check.py --no-provision` reports absent dependencies instead
of installing them.

The receipt names each child's own outcome. A child that was stopped for time
reported no exit code of its own, so the receipt carries no exit code for it
and records the reason instead; a suite that reported no summary carries no
test count rather than a count of zero. Podman and systemd validations only
run on Linux.

Offline tests require no API credentials, internet access, or running container daemons. The CI matrix targets Python 3.11–3.14; the recorded local result is Linux 3.14.

Test categories include path/symlink/hardlink refusal, immutable ID retries,
budget races and day changes, process cancellation/orphan pipes, unchanged
review skipping, goal/snapshot consistency, late Insight wakeup, stale verification,
source changes after verification, read-only Editor transport, unsafe ingest,
backup traversal/links/limits, hostile HTML, exact model mismatch, early Pi exit,
Unicode LF framing and agent_end vs agent_settled.

### Atomic state read/replace contract

`read_json` opens managed state with Windows delete sharing (a `CreateFileW`
handle transferred to a Python stream). Non-exclusive Windows publication uses
`SetFileInformationByHandle(FileRenameInfoEx)` with `FILE_RENAME_INFO.Flags` set
to `REPLACE_IF_EXISTS | POSIX_SEMANTICS` (0x1 | 0x2); delete sharing alone does
not make `os.replace` work with a retained target reader. The source handle has
`DELETE` access and the destination name is absolute UTF-16 with a byte length.
Unsupported native operations and sharing/access errors fail immediately: no
fallback or retry window. Exclusive create-only publication still uses
non-replacing `os.rename`; POSIX publication still uses `os.replace`. For files
published by `atomic_write`, a retained product reader must read complete A
while publication and new product reads complete B. External Windows readers
that omit delete sharing can still block replacement; permission/sharing errors
propagate rather than becoming missing state. This is not the untrusted-path
validation contract of `safe_read`, nor a guarantee for in-place writes.

`FileTests.test_atomic_replace_with_retained_reader` holds A behind events until
B is published and read, and checks create-only idempotence/conflicts before
releasing A. It is included in the ordinary CI matrix; Windows semantics require
an actual Windows result for the exact revision, not a Linux pass or mocked
Win32 calls. `WindowsReplaceTests` checks the native wrapper's flags, buffer
layout (including non-BMP UTF-16), error propagation and handle cleanup using
mocked calls; it does not establish actual filesystem semantics. API references:
[SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle),
[FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info),
and [rename flags](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information).
To run the focused file suite on Windows:

```powershell
python -m unittest discover -s tests -p test_primitives.py -v
```

### Windows per-test timing evidence

Timing artifact emission is explicit, independent of `--report`:

```sh
python scripts/check.py --test-timings private-validation/windows-test-timings.json
```

Ordinary local `python3 scripts/check.py` creates no timing artifact. The child
(`scripts/check_unittest.py`) flushes a value-free temporary JSONL sidecar per
test: stable test ID, start/stop event, and monotonic offset only; the parent
derives durations. Parent and child share a system-wide monotonic clock origin (Python 3.11+ on supported
platforms); absolute clock values are never exported. An unmatched start records
elapsed duration at child exit or timeout, before process cleanup, with
`completed: false`; completed entries use the matching start-to-stop duration.
Malformed/unmatched stops and invalid durations are ignored.

Writer and parser share bounds of 1,000,000 bytes, 20,000 lines/tests and
100,000 seconds per offset/duration. The writer reserves each start/stop pair
before admission and stops collecting when full, preserving the parseable prefix
rather than growing past the parser's cap. Malformed lines are skipped; missing,
empty or oversized sidecars yield no timing entries. Collection, parsing and
artifact writing are best-effort diagnostics only, never a test pass/failure
signal. There is no retry protocol; temporary sidecars are removed after use.

The dedicated artifact contains only sanitized test IDs,
`duration_seconds`/`completed` flags, and minimal runtime metadata (suite,
versions, platform, child timeout, total suite elapsed time, gate status). It never
carries failure details, output tails or diagnostics; do not substitute the general
`offline.json` receipt. Only the Windows-latest/Python 3.13 CI step supplies
`--test-timings`; CI uploads only this dedicated file for that job (`actions/upload-artifact` v4.6.2, 7-day retention,
`if: always()` with missing-file tolerance). It records diagnostic evidence; it
sets no timeout policy. One 1800 s child cap bounds the suite on every
platform: the suite costs about 923 s on the development host, about 1050 s
on a loaded verification host (the 2026-10-21 run was still running when the
previous 1000 s bound cut it off) and about 1475 s on a slow Windows runner,
so the bound covers the slowest supported host with margin. The modeled
setup/child/later-phase/cleanup/suffix path is 3246 s, leaving 354 s inside
the 60-minute Windows and POSIX scopes; this arithmetic is a contract, not a
worst-case runtime claim. The authoritative `durable-contract` scope is 70
minutes, holding the modeled path plus the two pinned-adapter provisioning
steps at the product's own 300 s per-adapter bound (3846 s). The
Linux/Python 3.13 cell also installs. That cell runs the gate once: the
installer reuses the source gate's receipt, and the idempotence reinstall
finds the release the cell already staged, so neither install runs a second
gate. Its scope is still 120 minutes, holding the source gate's modeled path
(3246 s) plus the installer's own bounded staging call (3300 s) = 6546 s,
leaving 654 s inside the job, so the cell is not cut off while the installer
works whether or not an install reaches its staged gate. Every scope that
encloses the gate holds the gate's modeled path, so a bounded run reports
its own outcome instead of being cut off by the enclosing scope; the
installer staging call holds the same path for the same reason.

The later phase is bounded as a whole, not per child. It runs one child per
check: the transport suite, every SDK contract suite that is not waived, one
syntax check per JavaScript file, and one per shell wrapper -- nine children
on Windows, sixteen in a POSIX matrix cell, nineteen in the durable job. A
budget per child would make the gate's own path grow with the number of
checked files, so the phase carries one 1200 s budget that every child
shares: a child takes at most its own 600 s bound and what is left of the
phase, with its own cleanup reserved inside that remainder. The budget
covers the measured healthy phase with wide margin -- about 4 s for the
sixteen children of a POSIX cell, and about 103 s for the nineteen children
of the durable job, whose three SDK contract suites measured 5.6 s, 75.5 s
and 18.1 s on the development host -- and it holds two children's full
bounds, so one hung child still leaves every remaining child a run. A child
the phase can no longer afford is recorded as a failure with the reason
`later-phase-budget-exhausted`; it is never skipped silently, and the
receipt records the phase's own child count, budget, and consumed time.
The 2026-10-21 Windows/3.13 run completed 2233 of about 2265 tests in
1447 s and reached the previous 1450 s child cap without a reported
test failure, and the 2026-10-21 POSIX run reached the previous 1000 s child
cap with the suite still running and three load-sensitive assertions
failing, so the child bound covers the suite's measured cost on both hosts
with margin.
A test that waits for a spawned child waits with one shared host-cost
bound (60 s, with a 600 s backstop for the child's own release): the
child is a fresh interpreter that re-imports the product's modules, so
its start-up is the host's cost and not a bound the product sets. A
short literal there let a loaded host report a healthy child as a
failed test.
If the bounded run stalls again, use its partial per-test artifact to identify
the unfinished test; timing samples are not bounds.

## Real-machine checklist

Use a disposable source copy and dedicated unprivileged account. Record release
SHA, dependency-lock SHA, Node/Python/CLI versions (Pi/Podman plus `codex`/`claude`
when those engines are configured), image digest, provider and
model IDs, engine per profile, container runtime and version, commands and sanitized evidence.
Do not store credentials in a public receipt.

Run doctor including the sandbox, then explicit live smoke. Execute the demo
work unit and inspect actual files and configured tests. Interrupt a bounded
command in a disposable project; confirm process groups/containers are gone,
the workspace remains, and restart does not silently replay. Try concurrent
workers and verify rejection. Lower request/time/output limits deliberately and
confirm refusal. Export an Editor capsule and attempt a write to its source;
confirm failure and successful proposal submission. Inspect artifact evidence
and stale snapshot labeling. Exercise backup/restore and staged update and removal of replaced code.

Then enable periodic roles with modest budgets. Observe an entire day, including
idle periods, paper generation, source refresh, provider failures and a restart.
Track request admissions, provider bills, container count, memory, disk growth,
iteration latency and usefulness of accepted changes. Use measured baselines;
no throughput or cost improvements are claimed without a reproducible workload.

Only after this evidence is reviewed should an operator mark a release as
accepted for that environment. An absent test is `not_run`, not a success.

### Single-ID hosts

Record additionally the wrapper path, the single `0 -> self` map observed by
doctor, the explicit storage paths, the delegated cgroup parent with its
enabled controllers, and the base-image digest reviewed before the build.
Prove the negative cases too: a multi-range map request against the wrapper
setup must fail, `doctor --sandbox` must fail when the cgroup parent is
missing, and an inherited `API_KEY` must fail the probe. Image-index
confusion after a failed build is recovered with `system reset` plus re-pull;
re-run `doctor --sandbox` afterwards and keep the recovery in the receipt.
