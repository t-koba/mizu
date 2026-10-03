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
