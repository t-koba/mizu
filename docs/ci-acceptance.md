# Trusted CI acceptance (disabled by default)

This is an **operator-only** alternative producer for retained acceptance, not
an inference from a green CI status. Nothing enables it in shipped configuration.
Independent review must approve both the implementation and the project's actual
equivalence declaration before activation. It does not deploy, request CI, alter
CI waits, mutate another project's ledger, or satisfy a smoke/promotion gate.

## Project policy

The operator may add this table to private `project.toml` (illustrative only;
replace the digests and review the complete coverage/environment declaration):

```toml
[ci_acceptance]
schema = 1
version = "reviewed-offline-v1"
repository = "example/project"
workflow = ".github/workflows/offline.yml"
workflow_digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
acceptance_scope = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

[[ci_acceptance.checks]]
check = "offline-linux"
commands = [0]
coverage = ["complete-offline-command-including-required-adapter-contracts"]
[ci_acceptance.checks.environment]
os = "linux"
python = "3.14.0"
image = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
```

Omit the table to disable selection/ingestion. There is no implicit trusted
provider or default equivalence. The existing operator-configured `[vcs].command`
is the sole adapter authority; it must implement the protocol below. The policy
is not a workspace file and cannot be supplied by a model tool.

`acceptance_scope` is the SHA-256 of canonical JSON for the existing local
acceptance scope: full ordered `verify` scripts, execution fingerprint, observed
local environment, pinned image and (when configured) cache-input fingerprint.
An operator can obtain it without a build using the installed host Python API:

```python
from mizu.config import load
from mizu.project import Project
from mizu.acceptance import scope
from mizu.fs import canonical, digest
project = Project(load(config_path), project_name, read_only=True)
bound = scope(project)
assert bound is not None
print(digest(canonical(bound)))
```

As for local retained proof, this requires representable inputs: check reuse
explicitly enabled, no-network digest-pinned sandbox and fingerprintable mounts
and caches. A changed acceptance command, image, dependency or cache input needs
a newly reviewed scope declaration, not automatic equivalence. `version` is an
operator-owned revision label; the entire policy, not just that label, is bound
into the receipt's scope along with a digest of the configured VCS settings and
the exact Mizu snapshot ID.

This small contract supports **one workflow and one run attempt** per policy.
All required checks must be in that workflow; cross-workflow/run joins are not
supported. `workflow_digest` is SHA-256 of the authenticated workflow definition
bytes, not a display name. `checks` has 1–32 entries with unique exact check
names. `commands` is a nonempty, duplicate-free list of zero-based indices into
ordered project `verify`; their union must cover every command. Multiple checks
may cover the same command (for example, distinct OS coverage). `coverage` is a
nonempty unique list of operator-defined coverage obligations, not a generic
"green" label. `environment` is a nonempty exact string map of relevant execution
facts (OS/runtime/image/dependency/tool identities as appropriate). Each list/map
has at most 32 members; text fields are 1–1024 UTF-8 bytes without controls. The
whole policy is capped at 32 KiB. Unknown keys or incomplete mappings refuse.
The operator must justify why the declared coverage and environment establish
full equivalence to the pinned local scope; Mizu cannot discover that from names.

## Trusted VCS adapter protocol

The host CLI calls the existing bounded `vcs.invoke` transport with:

```json
{"op":"acceptance","repository":"example/project","workflow":".github/workflows/offline.yml","sha":"<published SHA>","run":"<immutable provider run ID>","attempt":1}
```

This operation is **not** in `vcs_read`; that tool stays read-only and cannot
retain proof. The request carries selectors only, never caller pass/receipt data.
The adapter must independently authenticate the configured provider/repository,
workflow definition, tested source, latest run attempt and every check/job ID.
It must retrieve complete provider records, not trust PR prose, status display
names, unauthenticated artifacts, or a self-reported pass file from tested code.
It must verify the actual executed scripts, coverage and environment through a
trusted workflow/runner provenance path. The response is exactly:

```json
{
  "schema":1,
  "repository":"example/project",
  "workflow":".github/workflows/offline.yml",
  "workflow_digest":"<64 lowercase hex>",
  "sha":"<40 or 64 lowercase hex>",
  "code_digest":"<exact Mizu whole-source digest>",
  "run":"<immutable provider run ID>", "attempt":1,
  "current_attempt":true, "complete":true,
  "checks":[{
    "check":"offline-linux", "check_id":"<immutable provider check/job ID>",
    "commands":[0],
    "coverage":["complete-offline-command-including-required-adapter-contracts"],
    "environment":{"os":"linux","python":"3.14.0","image":"sha256:<digest>"},
    "conclusion":"success"
  }]
}
```

All rows belong to the envelope's exact SHA/workflow/run/attempt; rows from
another attempt must never be combined. `complete` attests that provider paging
and scope observation are complete. `current_attempt` attests this is not a
superseded attempt **at ingestion**. Attempts are integers 1–2147483647. Check
names and IDs must each be unique. Every declared check must occur exactly once,
with exactly matching command indices, coverage and environment; no extra rows
or unknown fields are accepted. Failed/cancelled/skipped/neutral/pending results,
partial or unknown scope, stale attempts and green-status-only payloads refuse.

`code_digest` is the independently established digest of the actually tested
whole Mizu source capture, **not** a Git tree SHA or a public projection digest.
The adapter must refuse when it cannot establish that identity. In particular,
a publication's projection digest alone cannot establish the whole capture if
publication omitted files. The host separately resolves the exact immutable
publication receipt and matches both whole-source digest and published SHA;
it never substitutes a later branch head or changes external receipt identity.
No shipped adapter is presumed to meet this contract merely by supporting status.

Transport bounds remain `[vcs].max_bytes` and `timeout_seconds`, including
combined stdout/stderr. The durable receipt remains within the existing 1 MiB
acceptance cap. Unsupported op, provider/authentication failure, timeout,
cancellation or malformed/oversized response yields no authoritative receipt.
There is no automatic retry or remote CI dispatch.

## Retention and completion

After review and with an existing exact publication receipt, the operator runs:

```sh
mizu ci-acceptance PROJECT --snapshot SNAPSHOT_ID \
  --publication-id PUBLICATION_ID --run RUN_ID --attempt 1
```

This queries only the configured adapter and returns `receipt_id`. Only host
code normalizes and mints schema-2 proof. Schema-1 local receipts and old work-item
records remain readable and local selection behavior is unchanged. CI receipts
use the same immutable content-addressed objects and first-writer scope indexes,
object-before-index ordering, no-follow bounded reads, audit/backup/recovery and
idempotent replay. Repeating an already indexed exact selector returns the same
ID without querying the provider; a different publication/run/attempt cannot
replace it. Interrupted object-before-index writes can be recovered using the
existing acceptance recovery. Concurrent conflicting retention refuses rather
than silently returning another request's evidence.

Selection checks policy, adapter, local scope and snapshot ID without provider
I/O or writes. Local retained proof takes precedence. Local timeout, cancellation,
failed or incomplete verification records, including an embedded failed snapshot
result, remain unchanged and are never relabeled as passing. They do not veto a
separate CI producer: CI can be selected only through its own retained receipt for
the exact snapshot/publication and only when the configured full command,
coverage and environment policy, authenticated run/attempt, complete check set
and successful conclusions all validate. Local and CI outcomes remain separate
evidence; Mizu does not edit, delete or turn local failures green, and it does not
infer CI success from them. Retention is not a promise that the provider will
never rerun that SHA later.

The immutable snapshot's `verification` is never rewritten. Its separately
projected `effective_verification` can name the exact retained CI receipt. The
normal WorkItems doing-to-doing delivery binding must precede completion, with
unchanged accepted scope and exact retained published snapshot/tree. Completion
pins the exact receipt ID and acceptance-scope digest and validates its snapshot
ID and publication binding. Policy removal/change prevents new CI selection but
does not rewrite or invalidate historical completion records. It does not accept
model-supplied receipt bodies, infer implementation completion, or close anything
automatically. The CLI does not grant current-workspace acceptance or allow a
writer `finish(done)` without its normal verification authority.


## Explicit reuse for staged installation

The installer still runs the **full local staged source suite by default**.
After the operator has configured equivalence and ingested trusted schema-2
proof as above, an explicit selection can replace only that source-suite gate:

```sh
python3 scripts/install.py --stage-only --reuse-ci-checks /path/to/operator-config.toml PROJECT SNAPSHOT
# Core-only disposable install and the same installed-release CLI smoke:
./scripts/test-install.sh --reuse-ci-checks /path/to/operator-config.toml PROJECT SNAPSHOT
```

`CONFIG PROJECT SNAPSHOT` are selectors, not verdicts: CONFIG is an existing
operator configuration, PROJECT identifies its retained evidence store, and
SNAPSHOT is the exact 64-hex published Mizu snapshot ID (not a Git SHA or latest
alias). The installer opens the project read-only and selects only the immutable
CI index/receipt for that snapshot under the **current** configured acceptance
scope and CI equivalence policy. A local receipt, even a green one, cannot win
this selection. The receipt is capped at the existing 1 MiB acceptance bound;
all configured checks must be present one-to-one with `success`, with exact
coverage, environment, workflow, repository, run/attempt and publication bindings.
The selector never calls a provider/adapter, dispatches CI, reads credentials,
ingests evidence, or repairs an index. Missing, ineligible, stale, partial,
non-green, tampered or mismatched evidence refuses without local fallback.
Retry only after the operator has corrected the source/configuration or retained
appropriate trusted evidence. There is no background job or cancellation state.

Before reuse, staging captures the current source without persisting it. It
requires zero capture skips and reserved paths, the exact snapshot/receipt whole
`code_digest`, and a current local Git HEAD equal to the publication receipt's
non-null `source_commit` (which need not equal the provider's `published_sha`).
The captured public projection must equal the distribution `source_manifest`;
the copied staged manifest must still equal that same manifest. A versioned
publication projection, when carried/required by the publication schema, must
match the supported projection schema, algorithm and digest. A legacy receipt
without projection fields still needs every other binding, including a known
source commit. The whole-capture code digest is never treated as a distribution
manifest digest. These checks also apply when the release directory already
exists; an existing candidate is not permission to bypass missing CI proof.

Freshness is **local exact-input freshness**, not a live provider recheck.
`current_attempt` was authenticated at ingestion. A later provider rerun or
invalidation is not detectable from retained evidence alone; operators requiring
that guarantee must not use this mode. Changed source, Git HEAD, snapshot,
acceptance inputs, adapter configuration or policy cannot reuse the old index.
No shipped configuration enables CI equivalence implicitly.

`--reuse-ci-checks` and the separate existing core-only
`--reuse-source-checks REPORT` option are mutually exclusive. The local-report
validator and its environment/schema/waiver checks are unchanged. CI reuse works
for core-only or Pi staging; full Pi staging still provisions the pinned Pi
dependencies and runs their installed contract/version checks. It does not waive
the installed-release smoke: the test-install wrapper forwards CI selection to
both installs and then executes the same version, idempotence, manifest and
CLI import/report/editor/backup/restore checks as the default path. It does not
satisfy live Pi, isolation, deployment or promotion acceptance. Installation
metadata records `checks_provenance: retained-ci-acceptance`, the exact
`ci_acceptance_receipt_id`, and retained CI check rows, never claims those checks
ran inside the staged release, and retains `live_validation: not_run`.
