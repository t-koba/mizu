# Public repository and release procedure

The source distribution is repository-ready; it does not create a remote or
publish anything automatically. Validate locally before creating the GitHub
repository. Keep private configuration, projects, sessions, reports, backups,
validation containing code and credentials outside the repository.

## Before the first public release

1. Run offline checks and the standalone installation smoke. Review security
   boundaries and the deployment-specific integration/soak evidence.
2. In a network-enabled environment run `scripts/lock-pi.sh`. Review and commit
   `adapters/pi/package-lock.json`. Do not fabricate registry integrity hashes.
   Reinstall with `npm ci` under the supported Node version and repeat Pi smoke.
3. Review dependency licenses, advisories and the complete dependency tree.
   `npm --prefix adapters/pi ls --all --json` can record the installed tree;
   package-lock plus that tree is inventory, not automatically a complete SBOM.
4. Scan staged files for keys, internal paths, hostnames, project data and
   copyrighted/private code. Review `.gitignore`, but do not trust it as DLP.
   Inspect `git diff --cached` before the first push.
5. Set repository description, enable private vulnerability reporting,
   branch protection, required CI and review rules. Add a security contact via
   the repository's supported private reporting channel, not a placeholder email.
6. Create a version tag only after recording precisely
   which integration tests ran. Initial version 0.1.0 must not imply production
   certification or an established support SLA.

Example local initialization, intentionally without a remote address:

```sh
git init --initial-branch=main
git add .
git status --short
git diff --cached --stat
# Review all staged content and lock before committing or adding a remote.
git commit -m 'Initial commit'
```

The workflow pins third-party action commit SHAs, grants `contents: read`, avoids
`pull_request_target`, and does not use provider secrets. Dependabot proposes
updates; it does not auto-merge or change the deployed runtime. Action SHA pins
and engine dependency locks should be refreshed through reviewed changes, not
silently replaced by `latest`.

For Pi package updates, Dependabot's npm entry covers both adapters but
explicitly ignores `@earendil-works/*`: its per-dependency grouping cannot satisfy
the lockstep invariant, and a partial update would fail the lockstep gate. Those
packages therefore are not release-detection signals from Dependabot. The opt-in
host check below can deliver a release signal for writer review; use
`docs/upstream.md` to confirm the currently adopted version. When considering a
newer stable Pi release, review
the package-specific changelogs, move every Pi package in both adapters to that
same version, regenerate both lockfiles with the configured offline materializer,
install from the approved offline npm cache, and run the Pi SDK gate plus the
full check. Make this lockstep adoption one reviewed commit; do not claim that
Dependabot produces a single release-wide PR.

## Opt-in Pi release notice (operator host only)

An operator may run this one-shot CLI command using the intended Mizu source or
installed distribution and its private host configuration:

```sh
mizu --config /path/to/config.toml pi-release-check PROJECT --role WRITER_ROLE
```

`PROJECT` must already exist and `WRITER_ROLE` must be an explicitly named,
configured role in that project. This is not a model tool. It requires the
existing explicit notice policy and web allowlist in the private configuration
(merge with existing settings; do not duplicate TOML tables):

```toml
[operator_records."operator.notice"]
enabled = true
route = "addressee"
retention = "permanent"
wake = true

[web]
hosts = ["api.github.com"]
```

The fixed authoritative **release signal** is
`https://api.github.com/repos/earendil-works/pi/releases/latest`, not an npm
package listing. The command uses the existing host `Web` transport: configured
byte/time bounds, public-DNS pinning, redirect destination checks and shared
cache apply. The one full source receipt is temporary for this command and is
removed after parsing; no per-poll receipt directory accumulates. An optional
configured destination decider must also permit the fetch; it does not replace
this command's allowlist prerequisite. No credentials
are injected. The JSON receipt must retain the exact API source and final URL,
`application/json`, an exact stable `vX.Y.Z` tag (no prerelease/build suffix or
leading-zero components), literal false draft/prerelease flags, a valid UTC
publication timestamp, and the exact official GitHub `/releases/tag/vX.Y.Z` URL.
Missing, duplicate, malformed or inconsistent fields fail closed; upstream
release prose is never put in a notice. This does **not** verify that every
lockstep package has been published, is compatible, or should be adopted.

The baseline is the common Pi pin in **the CLI's own source/distribution**,
not the managed project's mutable workspace. Both adapter manifests and
lockfiles must agree, including required packages, root declarations and nested
Pi resolutions (the same validator backs the offline lockstep gate). The command
compares stable versions numerically. Equal/older observations return JSON with
`newer: false` and no notice. A newer observation writes an immutable
`operator.notice` through the existing SharedRecords contract, addressed only to
the supplied role; its body includes baseline, tag, date, official URLs and
`external-untrusted; review only; no package availability or compatibility claim`.
Normal pending delivery, configured notice wake policy and explicit role
acknowledgement apply. This is review input, never an adoption decision.

Stdout is one JSON result with baseline, tag, source and `newer`; a newer
result also includes the immutable notice envelope/digest. The selected release
metadata and official URLs are durable in the notice; the raw upstream response
is not retained by this command. Normal configured web caching may return an
earlier observation within its TTL. Failures exit nonzero without writing a new
notice (temporary receipt data is removed).
Retry the same command after a failed or interrupted invocation: the event ID is
`pi-release-` plus the first 48 hexadecimal SHA-256 characters of the exact tag.
Exact retries reuse the original notice, even after explicit acknowledgement;
they neither duplicate nor reactivate it. A changed baseline, date or addressee
for an already recorded tag conflicts and fails closed instead of rewriting the
record; review that conflict rather than inventing another event ID. Interrupts
around a notice write are resolved by the same exact retry contract.

No schedule is installed or active by default. An operator may arrange periodic
invocation with their host scheduler, selecting the project, role, configuration
and interval explicitly and monitoring nonzero exits. The command does not alter
scheduler, daemon, CI, service installation, dependencies or lockfiles. No live
network check or release adoption is part of the offline acceptance suite.

## Build distribution

```sh
python3 scripts/package.py --output dist
# Strict public-release mode requires a reviewed lock:
python3 scripts/package.py --output dist --release
```

The archive includes source, tests, policies, docs and metadata, not npm binaries,
private state, caches or a vendored Node runtime. Files have a SHA256 manifest.
`DISTRIBUTION.json`/`MANIFEST.sha256` exist only inside built archives;
do not check them into the source tree. Tar/gzip and zip timestamps and permissions are deterministic; an optional
`SOURCE_DATE_EPOCH` changes the timestamp. SHA256 checks integrity, not publisher
identity. Sign tags/artifacts through the maintainer's chosen release process if
publisher authentication is required. The build also writes `dist/SHA256SUMS`
(checksums of the archives themselves); `dist/` is local build output, not source.

An offline source archive can be generated without a dependency lock, but it is
labelled accordingly. `--release` refuses that state. The initial generated
source bundle is useful for inspection and real-machine validation; it is not a
claim of byte-reproducible transitive npm resolution.

## Current formats and dependency changes

Version lives in `src/mizu/__init__.py` and product metadata. Internal records and bridge configuration have no generation numbers or
compatibility readers. Current structure, bounds and integrity checks remain. Activation removes the
replaced managed release after successful pointer and launcher updates.

Model profiles do not change the product version. Changing the Pi dependency
requires tagged API review, refreshed lock, installed-package validation, live
probe and release notes. See `docs/extensions.md`.

Distribution inventory is shared by installer and packager through src/mizu/distribution.py. Only config/config.example.toml is public configuration. Installation receipts record individual Python, Node, JavaScript and shell checks. Candidate-copy manifests must match staged contents before promotion. Windows link fallback retains a recovery link; fallback replacement has a gap and is not atomic. See completion-contracts.md for current data requirements.
