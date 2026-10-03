# Dynamic model selection

Selection is opt-in and runs once **before each work unit**, using the existing
Pi, Codex or Claude driver. Profiles keep exact engine/provider/model/native
options. There are no built-in provider rankings, quota interpretations or
error-message heuristics. A selector never changes grants or switches an active
conversation. Ordinary fixed-profile configurations retain their behavior.

## Configuration

A role declares exactly one of `profile` or `selector`. The selector table is
operator policy. Candidate lists are ordered; the first matching rule owns the
selection, even if all its candidates are unavailable. There is no fallthrough
to later rules after a match. An omitted `when` means true. No matching rule, or
no eligible candidate, yields a waiting result without launching the main model.
`retry_seconds` is required and controls reevaluation when there is no earlier
known block/observation deadline.

For example, with separately configured profiles named `free` and `subscription`:

```toml
[selectors.coding]
retry_seconds = 60

[[selectors.coding.rules]]
candidates = [
  {profile = "free", group = "free-account"},
  {profile = "subscription", group = "subscription-account"},
]

[[selectors.coding.on_error]]
when = {path = "error.code", op = "eq", value = "EXPLICIT_PROVIDER_CODE"}
scope = "group"
seconds = 900

[roles.worker]
selector = "coding" # replaces profile; retain policy/workspace/grants/schedule
policy = "policies/worker.md"
workspace = "write"
capabilities = ["files", "read", "verify", "finish"]
```

The provider code and the waiting time above are placeholders selected by the
operator, not built-in classifications. If an engine exposes only a message,
`contains` may be explicitly configured against `error.message`. Message text
is never parsed to manufacture a status or reset time. Models and quotas must
be reviewed in that provider's own documentation and account settings.

Each candidate accepts `profile`, optional `group` (defaults to the profile
name), `when`, and `recover_when`. `when` can require fresh quota observations,
check task attributes or inspect past results. A block normally lasts until its
configured deadline. `recover_when` can override it early **only with a fresh
observation for that candidate's group newer than the block**. This override is
not permanent: if the observation expires before the block does, the block
applies again. A newer observation alone does not silently clear blocks.

Each `on_error` entry accepts `when`, `scope` (`profile` or `group`), and exactly
one of `seconds` or `until = "error.retry_at"`. The first matching entry with a
usable future deadline applies. An unavailable/past retry time does not match an
`until` action; a later fixed-duration rule can provide an explicit alternative.
Overlapping blocks retain the latest expiry. Shared groups represent operator
chosen quota scopes, never guessed from provider names or credentials. Active
blocks persist across configuration changes and restarts until expiry or an
explicitly configured observation-based recovery.

## Facts and predicates

The same bounded predicate evaluator is used everywhere:

```toml
when = {all = [
  {path = "attributes.difficulty", op = "in", value = ["hard", "critical"]},
  {path = "observations.free-account.facts.remaining", op = "gt", value = 0},
]}
```

Logical nodes are `all`, `any` (nonempty arrays), or `not` (one predicate).
Leaf nodes contain `path`, `op`, and `value`. Operators are `eq`, `in`, `lt`,
`lte`, `gt`, `gte`, `exists`, and case-sensitive literal string `contains`.
Ordering is numeric; booleans are not numbers. No regular expressions, shell,
Python expressions, plugins or callbacks are evaluated by selectors.

Missing/null facts evaluate to unknown; negating unknown is still unknown.
Only true matches. To explicitly permit missing data, use `exists = false`
as a leaf (`op = "exists", value = false`) in an `any` condition. A stale
observation exposes its timestamps and `fresh = false`, but not its facts.

Available paths:

| Context | Facts |
|---|---|
| Classification rules | `role`, `project` (names), `attributes.NAME`, `task.goal`, `task.state` |
| Selection rules | Above, plus `observations.GROUP.fresh/observed_at/expires_at/facts.NAME`, `history.PROFILE.status/at/run/error` |
| Candidate conditions | Above, plus `candidate.profile/group/history` |
| Error rules | Recorded selection facts (excluding task text), plus `error.source/kind/code/message/retry_at/details` |

History is the latest completed model execution or reported model failure per
profile, shared across projects. Local permission/configuration/protocol errors
are not converted into model availability. Selection decisions use a captured
snapshot of facts; observations arriving during inference affect the next unit.

## Attribute sources and inference classification

Add `[attributes]` in operator-owned `project.toml`, `[roles.NAME.attributes]`
in configuration, or pass `mizu run PROJECT --attributes attributes.json`.
These are scalar labels chosen by the operator, not new tool permissions.
Precedence is **run > role > project > rule classification > inference**.

Rules fill missing attributes, in declaration order. They see previously filled
attributes. Their text inputs are the current project goal and latest published
state, not an arbitrary filesystem scan:

```toml
[[selectors.coding.classify_rules]]
when = {path = "task.goal", op = "contains", value = "migration"}
attributes = {difficulty = "hard"}
```

Inference classification is optional:

```toml
[selectors.coding.classifier]
profile = "classifier" # fixed, explicitly configured profile
policy = "policies/classifier.md"
on_failure = "continue" # or "wait"
retry_seconds = 120

[selectors.coding.classifier.attributes.difficulty]
type = "string"
values = ["easy", "hard"]
```

Attribute declarations accept `type = "string"`, `"number"`, or `"boolean"`,
and optional nonempty `values`. The classifier returns a subset of declared
attributes. Undeclared names, invalid types/values, nonfinite JSON and non-object
output fail validation. Its Markdown policy defines how to classify tasks and
instructs it to call `mizu_finish` with an attribute JSON object in `summary`.
The existing `finish.summary` limit (12,000 characters) still applies.

The classifier receives JSON with `task`, existing `attributes`, `role`,
`project`, and `output_attributes`. Task text is data, not authority. Only
`finish` is granted; no native tools, consultation or shared-state mutation is
exposed. The profile's existing reviewed resources/native options remain
operator-owned and subject to the normal driver checks. Use a profile compatible
with those restricted grants. This is an independent ephemeral work unit, under
the parent's execution slot and role/writer locks, using normal request/time
budgets. Classification does not itself enter selection or recurse.

Successes are cached per project/role by inputs, declaration, policy content,
effective profile, command, adapter digest and native settings digest. A cache
hit spends no model request. Classification runs retain their own usage/error
records. A failed inference or invalid label output either continues with the
existing attributes or waits, according to `on_failure`; retries are throttled
by `retry_seconds`. Local config/permission/protocol integrity failures, budget
exhaustion and cancellation propagate normally, rather than selecting around
a broken execution contract. Corrupt cache/state files fail explicitly.

## External observations and preview

No quota API clients or credential-reading paths are added. The operator obtains
facts separately and registers a finite JSON observation:

```json
{"group":"free-account","observed_at":1790985600,"expires_at":1790985900,"facts":{"remaining":12,"available":true}}
```

Times are UTC Unix seconds. `observed_at` must not be in the future;
`expires_at` must be later. Use current observed times, not these example values.
Groups must be referenced by a configured candidate. Facts use the same scalar
attribute format. Updates are idempotent for identical observations; older or
conflicting observations at the same timestamp are rejected.

```sh
mizu selection observe --file observation.json  # '-' reads bounded stdin
mizu selection status
mizu selection delete free-account             # deletes observation, not blocks
mizu selection preview PROJECT --role worker --attributes attributes.json
```

Preview does not launch inference, register state, acquire budgets or write
files. It uses a matching successful classifier cache if available. Otherwise
it reports `provisional = true` and `classification.status = "not_run"`; the
proposed profile is conditional on that missing classification. Selection
status shows raw saved observations, blocks and history; preview evaluates
freshness at the current time.

## Execution and evidence contract

Reported model failures are distinct from local protocol invariants. Pi provides
assistant `error` and its message; abort remains a protocol/stop path. Codex
provides failed-turn error details or a `turn/start` RPC error. Its structured
`codexErrorInfo` is retained under `error.details.codex_error_info`. Claude
provides terminal reason/subtype/errors, or query-stage exception class/message.
Unavailable codes and retry times remain null. Only explicit numeric retry times
are accepted; none of the current adapters derives them from prose or headers.
Cancellation, native turn/cost/tool-abort limits, invalid handshakes, missing seals and local authorization failures
do not trigger selector actions.

A matched error action saves its evidence and returns `status = "deferred"`;
it does not automatically replay the work or increment the ordinary consecutive
failure brake. An unmatched failure follows the existing health/pause behavior.
`status = "waiting"` means no main-model invocation was made. Both results
provide `next_evaluation_at`. The daemon uses interruptible waits and rechecks
at least at its configured idle cadence so external updates can be noticed.
This is not a new scheduling/wake policy: existing project readiness and
on-change checks still apply.

Every attempted selection records `runs/RUN/selection.json`: definition digest,
timestamp, task-input digest, attributes and provenance, observation freshness, prior results,
matching rule, exclusions, selected profile and classification-run reference.
Main result/error records also reference the decision and failure action.
Classification results are separate canonical run records, never counted twice
through their parent. Private task text stays in normal inputs, not selection
facts. Model sessions retain existing identity checks: profiles with different
effective settings use different session namespaces; conversations are not
transplanted. Explicit consultation profiles bypass selection. A selector role
requires `smoke --profile` so the paid probe has an exact target.

New adapters provide `usage_observations` pairing each reported usage fragment
with its actual provider/model when available. Missing attribution is grouped
as `unknown`, never assigned to the configured virtual/fallback model. Pi can
supply both names; Claude supplies model names but not per-model providers;
Codex turn aggregates do not establish attribution for auxiliary calls and stay
unknown. Admission counts belong to the unknown/unknown bucket because they
cannot be distributed among these observations reliably. Legacy records without
`usage_observations` retain their existing grouping. Group run counts can overlap
when one run used multiple models; `totals.runs` and total unknown-run counters count unique canonical runs.
Token observations, logical admissions and external quota facts are distinct;
none is a bill or a remaining-quota reservation.

## Bounds, persistence and verification

Selectors: at most 64 definitions; every rule/candidate/error/classification list
has at most 64 entries. Predicate depth is at most 12 and each predicate is at
most 16 KiB. Attribute maps have at most 64 entries and 16 KiB, with strings up
to 4096 characters and finite numbers within ±2^53. Names follow Mizu IDs
(lowercase letters/digits, `_`, `-`, at most 63 characters). Intervals are explicit
integers in 1..31,536,000 seconds. Classifier policy files are at most 64 KiB.
Each decision is bounded at 256 KiB; exceeding the bound fails before main
inference. Attribute bounds also apply to the merged attribute map.
Selection configuration, input JSON and the shared state file are bounded at
1 MiB; each state table has at most 8192 entries. Classifier cache files are
bounded at 64 KiB. Existing run/tool/usage evidence bounds remain in force.

State lives under `data/selection/state.json` (version 1). Updates use a shared
lock and atomic replacement; reads and preview do not mutate it. No lock is
held across model inference. Concurrent failures retain the latest block end;
this does not reserve quota, prevent already-running requests, or guarantee
account limits. State is not silently reset on parse/schema failures or
oversize data. Removing obsolete policy names does not destroy their audit
state. Back up the global selection directory separately from project backups;
project backups include project-local classification caches and run evidence.

`tests/test_selection.py` uses synthetic facts/driver doubles and controlled time
for failover, recovery, unknowns, classification, concurrency, publication and
usage attribution. Offline tests and actual installed Pi SDK with faux provider
are separate from live-provider authentication, quota reset and real OCI tests.
Activation remains an operator configuration change; no deployment or login is
performed by selection.
