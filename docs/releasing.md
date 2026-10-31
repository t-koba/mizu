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
packages therefore are not release-detection signals from Dependabot. Configure
the Pi release Atom feed `https://github.com/earendil-works/pi/releases.atom` in
`[web].feeds` (with `github.com` allowed under `[web].hosts`). The configured
searcher consults it through `mizu_search`; search results are external-untrusted
inputs
that reach the writer as insights, not adoption decisions. There is no
Pi-specific release-check command or automatic package update. Use
`docs/upstream.md` to confirm the currently adopted version. When considering a
newer stable Pi release, review the package-specific changelogs, move every Pi
package in both adapters to that same version, regenerate both lockfiles with
the configured offline materializer, install from the approved offline npm
cache, and run the Pi SDK gate plus the full check. Make this lockstep adoption
one reviewed commit; do not claim that Dependabot produces a single
release-wide PR.

### Pi 1.4.0 durable database migration and rollback

`@earendil-works/pi-durable` 1.4.0 automatically migrates a SQLite database to
schema version 2 when it is first opened. After that migration, pi-durable
1.3.0 and earlier cannot open the database. This change does not migrate any
live state during development or publication; the first 1.4.0 runtime open is
the migration boundary.

Before installing 1.4.0 on a deployment with existing durable SQLite stores,
quiesce the affected project and every process that can hold its durable store
open, then use the deployment's existing SQLite-consistent release backup for
`<data_dir>/durable/`. Keep that
pre-migration backup through the rollback window. If a rollback to 1.3.0 is
needed, stop the 1.4.0 runtime and restore the pre-migration database backup
before starting 1.3.0; downgrading package files alone cannot reverse schema
version 2. Use the existing deployment backup/restore process, not a raw copy
of a live SQLite file. `mizu backup PROJECT` archives project data and does not
include the global `<data_dir>/durable/` stores, so it is not this backup.

The Mizu adapter keeps `store_compat` unchanged so 1.4.0 continues the existing
conversation through its forward migration. That identity choice does not make
a migrated database readable by 1.3.0. Mizu does not perform the live migration
or create a durable-store backup automatically.

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
