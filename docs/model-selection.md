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
# counts_as_failure = true # optional; defaults to false

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

A fixed `on_error` entry accepts `when`, `scope` (`profile` or `group`), and
exactly one of `seconds` or `until = "error.retry_at"`. Without `probe_seconds`,
fixed deadlines and overlap semantics are unchanged. The first matching entry with a
usable future deadline applies. An unavailable/past retry time does not match an
`until` action; a later fixed-duration rule can provide an explicit alternative.
Overlapping blocks retain the latest expiry. Shared groups represent operator
chosen quota scopes, never guessed from provider names or credentials. Active
blocks persist across configuration changes and restarts until expiry or an
explicitly configured observation-based recovery.

Every `on_error` rule may set `counts_as_failure` (strict boolean, default
`false`). A matched terminal error still blocks its configured scope and returns
a deferred/waiting outcome; by default it does not advance the role's consecutive
failure brake. Setting it to `true` opts that terminal matched outcome into the
existing brake and its configured auto-pause threshold. A successful fallback
still completes normally and clears the consecutive count. Unmatched errors and
rules without a usable future deadline keep the existing generic failure behavior.

A fixed (non-probe) `on_error` entry may set `action = "fallback" | "retry"`
(default `"fallback"`) and `retries` (integer `0..64`, default `2`). `fallback`
keeps today's behavior exactly: block the rule's scope and use the next eligible
candidate. `retry` keeps the SAME candidate: wait until the rule's deadline
(`seconds` after the failure, or `error.retry_at` for `until` rules), try it
again, and repeat up to `retries` same-candidate attempts; when retries run out
the rule continues as `fallback`. Probe rules reject `action`/`retries`; `until`
rules accept them with the wait measured to `error.retry_at`.
Provider timestamps are external-untrusted, so every retry wait is additionally bounded
at `[limits] maximum_wait_seconds` (default 86400): a farther deadline still records
verbatim in the action evidence, but the wait is capped and then the retry fires.

The retry wait is a bounded wait that holds NO execution slot and spends
nothing: dispatch admission is released for the wait, the wait polls at
`[limits] idle_seconds` with stop wake, authority and the daily budget are
revalidated without consuming anything, and fail-fast re-admission is required
before the retry. Waiting never renews deadlines. Budget, stop, wall/deadline
or re-admission failure abandons the retry and the unit drops through the
existing path for the raised error (the original failure deferred, or the
infra/authority/bound error itself). The wait between runs for the earliest
unblock is unchanged: a deferred run exits and the next evaluation at
`next_evaluation_at` holds no slot either.


### Optional paid recovery probes

An error rule may instead opt into an exact-target recovery obligation:

```toml
[[selectors.coding.on_error]]
when = {path = "error.code", op = "eq", value = "EXPLICIT_PROVIDER_CODE"}
scope = "group"
probe_seconds = 60
seconds = 3600 # optional upper expiry, measured from the original failure
```

`scope` is required. `probe_seconds` is an integer in 1..31,536,000 seconds;
there is no default and no probing without this explicit grant. In probe mode,
`seconds` is an optional upper bound, also in that interval range. `until` is
rejected. Without an upper bound the obligation remains until an exact-target
probe succeeds. The common `counts_as_failure` option applies to probe rules too;
its default is false, with explicit true opting a terminal matched probe failure
into the existing role failure brake.

A group block excludes **all** candidates in the operator-selected group, but
its probe always targets the exact profile whose attempt failed, never an
alternate or a group representative. Every failure contributes independently:
two failing profiles in one group require two recoveries, and one profile's
older recovery does not clear a newer failure. Fixed blocks retain their
maximum expiry independently of probe obligations, in either creation order.
Candidate `recover_when` remains a local, freshness-bound eligibility override;
it neither deletes nor mutates any underlying fixed block or probe obligation.
An overridden candidate does not cancel the paid obligation.

Before the failed attempt executes, selection captures an opaque binding to its
effective profile (engine/provider/model/session/options/resources/native
content), reviewed adapter content, native settings, command argv and resolved
executable bytes (at most 256 MiB). It also binds digests of the configured engine
directory and credential-source locators: `credentials.env` beside the main
configuration for Pi-family engines and Codex providers using environment
credentials, and Codex's selected `CODEX_AUTH_FILE` (or its default auth locator). The sponsor is that exact project and role,
bound to the project directory identity and `project.toml` content, not just its
name. Changing/deleting a target, relevant native content, command, sponsoring
project configuration, or enabled role never silently retargets an obligation.
Restoring the same target/sponsor binding permits later retries; a replacement
project with the same name is not a substitute sponsor. These bindings contain
no credential content and expose digests, not private paths. Secret refreshes
within the same source remain possible: credential bytes/values are never read
for binding. Consequently, switching account identity at the same source path
(including changing its symlink destination or inherited environment credential
values) cannot be detected by this binding without reading secrets.

Any daemon for the project services at most one due probe event per loop,
**before** ordinary readiness, on-change, done/wait outcome, or wake gating;
contribution selection is project-scoped, not tied to the daemon's role. The
probe still revalidates the stored sponsoring role, target, budget and locks.
This adds no services or timers: at least one project daemon must be running;
a non-daemon sponsor alone does not install a probe service. The daemon rechecks
at least at `idle_seconds` cadence and waits for an earlier probe/upper-expiry
deadline when present. These extra ticks do not shorten the ordinary role
cooldown. Probe
admission still requires armed, unpaused, not draining, and not stopped. It
uses the normal role lock, writer lock where applicable, and execution slot;
there is no lock across inference on the shared selection state. Lock contention,
budget refusal and loss of target/sponsor retain the block with visible reason
codes, and retry no sooner than the next configured probe interval. Configuration
is reloaded after claiming and again after acquiring launch locks, before setup
or driver execution; a changed data root, sponsor or target defers without
admission. Context and driver receive fresh configured limits. Context request
and non-Pi invocation admission revalidate uncached authority and refresh Budget
limits immediately before admission as well. These reads are not atomic with
configuration edits, Budget admission, or the provider request: a revocation
after the last read (or after admission but before the provider call) can still
race an already-authorized request. Postflight checks can prevent recovery, not
undo payment or refund an admission.

A probe uses the configured driver directly in a fresh ephemeral session, an
empty workspace, a fixed minimal challenge policy, and only `finish`. It never
runs selection/classification/fallback, consumes routing, receives goal/task
text, allocates `projects/.../runs`, publishes snapshots, or changes role health.
Native tools and external MCP/resource/extension/plugin/skill/hook surfaces are
not granted. Profiles requiring those surfaces (including Pi codemode/tool
search) defer as `native_tools_incompatible` rather than being silently reduced
to another profile. Other driver-specific incompatibilities fail closed through
normal driver checks. Durable probe stores are disposable event-local stores,
not retained project conversations.

The live ceiling is 90 seconds, two admitted model requests, and one finish tool
call; smaller sponsoring-role/configured limits win, and no wrap-up requests
are granted. Requests go through the normal Context/driver admission to the
**real sponsoring project's Budget**, including its shared daily limit. This
is separate paid event accounting, not an ordinary run or a quota reservation.
Logical admissions and reported token usage are not provider bills or HTTP
request counts. Ordinary run-based usage reports exclude probes; probe spend
is retained in the event ledger, while Budget totals include both.

Success requires a verified driver settlement, at least one confirmed
admission, matching configured target identity (no explicitly contrary provider
or model in either `usage_observations` or `observed_models`, including assistant
messages without usage attribution; missing identity remains unknown), an exact challenge-only `finish` seal, and authorization rechecked
against current configuration. Merely calling finish, an unknown/missing
terminal result, a cancellation, wrong target/challenge, or a failed driver
never recovers. The intent/claim is durably saved before launch, admissions and
known numeric usage update that intent, and a terminal event is saved **before**
applying recovery. Recovery is fenced by contribution, revision and claim;
it releases only that contribution. Failed probes leave all blocks in place.

Event records have bounded numeric usage/admission counts and stable reason
codes, not prompts, private paths, arbitrary exception messages or raw completion
text. Driver scratch/evidence files live in a disposable selection event
directory and are removed on settlement. After a hard crash they are cleaned
when that claim is next serviced. Concurrent daemons cannot spend twice on the
same claim. An interrupted intent waits until its 90-second live ceiling,
is marked `unknown`, and then waits a full probe interval before a fresh claim.
A terminal success left unapplied by a crash is likewise treated as uncertain,
not replayed to guess recovery. Budget retains its claim-bound admission even
if a crash interrupts the admission-to-event write; missing evidence never
becomes a success.

`mizu status PROJECT` includes `selection`: relevant active fixed/probe profile
and group blocks, selector/selection rule/error rule, upper expiry, sponsor,
next probe, last event/result, current authorization reason and saved defer
reason. Shared group blocks may name another sponsor but never expose its task,
errors or history. Legacy fixed blocks have only their original expiry/run
metadata. `mizu selection status` remains the raw, bounded shared ledger view,
including completed/expired receipts and probe events. Status deduplicates target and
sponsor authorization checks by opaque identities within each read only; it
writes no ledger/cache and its display cache is never used for admission.

A run whose main-model error matches an `on_error` rule falls back within the
same dispatch: the recorded block applies to an immediate re-selection, and
the run continues on the next eligible candidate instead of losing the whole
interval to the next scheduled start. Attempts are bounded by the candidate
count, and the same run context is reused so the run's own deadline and
budget keep accumulating across attempts. For pi-durable, the durable requestId
normally stays bound to the same selector attempt so retry/recovery cannot
submit it twice. Only a known terminal `ModelFailure` from `pi-durable` with
`kind = "unanswered"`, followed by a successfully selected fallback, advances
a bounded per-run attempt ordinal; that fallback gets a distinct deterministic
requestId while retaining the same durable session/store and grant/resume
checks. Launcher, protocol, cancellation, admission, and other uncertain errors
do not advance it, so a changed authority still fails closed against the old
run row. The ordinal is not derived from model or grant identity, and repeated
calls for one candidate keep the same identity. If no candidate remains
eligible, or no rule matches, the run fails as before (deferred with the block,
or raised). Whether to fall back stays configuration: the selector's
candidates and rules decide, never a built-in retry.

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
| Selection rules | Above, plus `observations.GROUP.fresh/observed_at/expires_at/facts.NAME`, `history.PROFILE.status/at/run/error`, `recommendation.profile/reason` (fresh only; absent otherwise) |
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

[selectors.coding.classifier.inputs] # optional bounds, defaults shown
max_proposals = 8   # pending proposals offered, newest actionable revisions
max_decisions = 8   # unacknowledged decisions routed to the role, as evidence
max_text_bytes = 2048 # per-text cap for goal/state excerpts, titles, reasons
```

Attribute declarations accept `type = "string"`, `"number"`, or `"boolean"`,
and optional nonempty `values`. The classifier returns a subset of declared
attributes. Undeclared names, invalid types/values, nonfinite JSON and non-object
output fail validation. Its Markdown policy defines how to classify tasks and
instructs it to call `mizu_finish` with an attribute JSON object in `summary`.
The existing `finish.summary` limit (12,000 characters) still applies.

When every declared attribute is already labelled (run, role, project, or
classification rule), inference is skipped entirely: no model request is spent
and the decision records `classification.status = "explicit"`. Invalid explicit
values fall through to inference rather than failing selection. Otherwise the
classifier receives JSON with bounded `task` excerpts, pending `proposals`
(newly actionable revisions), `evidence` (latest verification verdict plus
unacknowledged decisions routed to the role), existing `attributes`, `role`,
`project`, and `output_attributes`. Task text is data, not authority. Counts
and text lengths follow the operator's classifier `inputs` bounds; wider
history and unrelated backlog are never replayed. Only
`finish` is granted; no native tools, consultation or shared-state mutation is
exposed. The profile's existing reviewed resources/native options remain
operator-owned and subject to the normal driver checks. Use a profile compatible
with those restricted grants. This is an independent ephemeral work unit, under
the parent's execution slot and role/writer locks, using normal request/time
budgets. Classification does not itself enter selection or recurse.

Successes are cached per project/role by material inputs (goal text, pending
proposals, verification/decision evidence, attributes), declaration, policy
content, effective profile, command, adapter digest and native settings digest.
Raw state text is deliberately excluded: bookkeeping state churn between idle
units reuses the cache, while new proposals, fresh evidence, goal edits, label
changes, or policy/settings changes reclassify. A cache hit spends no model
request. Every decision records `classification` with `status`, `source`
(`explicit`, `inference`, `cache`, or `none` for preview), a human-readable
`reason`, and the reported model summary (identity, requests, known token
totals) when inference ran. Classification runs retain their own usage/error
records. A failed inference or invalid label output either continues with the
existing attributes or waits, according to `on_failure`; retries are throttled
by `retry_seconds`. Local config/permission/protocol integrity failures, budget
exhaustion and cancellation propagate normally, rather than selecting around
a broken execution contract. Corrupt cache/state files fail explicitly.

## Worker-directed next-unit routing

A finishing unit can recommend the profile the next unit should use, with
no extra model call: the recommendation rides on the existing `finish`
tool, which accepts optional `next_profile` (a profile identifier) and
`next_reason` (bounded text, requires `next_profile`). Malformed values and
names that match no configured profile are refused at finish time.

The runtime records the recommendation under the run lock in
`routing/ROLE.json`, bound to the finished task inputs: the goal digest
and the published snapshot id. The next unit's selection exposes it as
`recommendation.profile`/`recommendation.reason` facts only while the
binding still matches the current goal and snapshot. A goal edit or a new
publication makes it stale; a missing file reads as absent; a corrupt file
reads as invalid. Stale, absent, and invalid records fall back to ordinary
selection and are reported in the decision's `recommendation` status, never
fatal. How long a fresh recommendation lasts is operator policy per
selector: `recommendation_validity = "once"` (default) covers only the
following dispatched unit — the dispatched unit consumes the file so the
unit after falls back unless a newer recommendation was recorded — while
`recommendation_validity = "sticky"` keeps the legacy behavior of staying
valid while the task is unchanged. A unit countermands by recommending
again, including back to the default profile. Preview and waiting
selections never consume; only a dispatched unit consumes an `once`
recommendation, best-effort and never fatal.

The recommendation-to-profile mapping is operator policy: ordinary
selector rules match on it, so blocks, availability, and candidate order
keep working unchanged. No profile names or difficulty criteria live in
the product; the recommended string is only validated as an identifier:

```toml
[[selectors.coding.rules]]
when = {path = "recommendation.profile", op = "eq", value = "deep"}
candidates = [
  {profile = "deep-reasoning"},
  {profile = "main"},
]
```

A profile switch that changes model, engine, grants, policy, or content
starts a fresh bounded session; an effort-only switch (native reasoning-effort
keys) continues the role's conversation with the current effort, so
conversations are never transplanted across incompatible settings;
returning to the earlier profile resumes its conversation. Routing applies
to selector roles; fixed-profile roles ignore recorded recommendations.
Deployment and routing policy activation remain operator work: without
selector rules matching `recommendation.*`, recorded recommendations change
nothing.

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

Quality evidence must be measured under the effective role grants, sandbox,
image, and native tools, never imported from a model-only ranking: assess a
frozen profile+driver+tools+image+grants bundle on a fixed, independently
checked task known to be solvable under that exact policy. Record failures in
the request/token/time/cost observations alongside successes (failed
trajectories can cost as much as passing ones); hardening moves success and
spend independently, mostly through workaround construction. When comparing
recovery behavior, distinguish an impossible-by-policy task from one with a
demonstrated compliant alternative, and keep denied-action/workaround
evidence. The `scripts/comparison.py` conditions/evidence/limitations fields
carry this without schema growth; the live smoke in docs/testing.md proves
protocol interop only, never research quality or large-codebase utility.

## Execution and evidence contract

Reported model failures are distinct from local protocol invariants. Pi provides
assistant `error` and its message; abort remains a protocol/stop path. Pi-durable
preserves textual settlement `detail` (bounded to 4,000 Unicode characters)
alongside the unchanged `reason`; `error.message` combines them as
`<reason>: <detail>`, so an explicitly configured `contains` predicate can
match provider text without inferring a status code. Codex
provides failed-turn error details or a `turn/start` RPC error. Its structured
`codexErrorInfo` is retained under `error.details.codex_error_info`. Claude
provides terminal reason/subtype/errors, or query-stage exception class/message.
Unavailable codes and retry times remain null. Only explicit numeric retry times
are accepted; none of the current adapters derives them from prose or headers.
Cancellation, native turn/cost/tool-abort limits, invalid handshakes, missing seals and local authorization failures
do not trigger selector actions.

A matched error action saves its evidence and blocks the failed profile or
group; the same dispatch then continues on the next eligible candidate, one
attempt each bounded by the candidate count, and never replays the blocked
profile within the run. Only when no candidate remains eligible does the unit
return `status = "deferred"`. Fallback attempts remain within one dispatch and
do not each advance the brake. The ordinary brake advances once for a terminal
failure dispatch: a matched terminal deferral does not count by default, but
setting its matched `on_error` rule's `counts_as_failure` to `true` opts it into
that single increment. A completed fallback resets the consecutive count. An
unmatched failure follows the existing health/pause behavior.
`status = "waiting"` means no main-model invocation was made. Both results
provide `next_evaluation_at`. The daemon uses interruptible waits and rechecks
at least at its configured idle cadence so external updates can be noticed.
This is not a new scheduling/wake policy: existing project readiness and
on-change checks still apply.

Pre-execution waits are evaluated before a run directory is allocated. The
first wait with a given reason audits as one run (`started.json`,
`selection.json`, `result.json`); repeated polls with the same stable reason
return `status = "waiting"` with `coalesced = true` and the fresh
`next_evaluation_at` without creating another run directory. Only the
time-varying `at`/`next_evaluation_at` fields are excluded from the comparison,
so any changed candidate, block, observation, history, attribute, task,
classification, or recommendation reason audits again. A successful dispatch
clears the record, and the per-project `selection/<role>-wait.json` pointer
names the audit run. The pointer is validated before reuse: when the audit
run no longer carries its `selection.json`/`result.json` wait evidence
(retention, restore, or manual cleanup), the next wait audits a fresh run
instead of coalescing onto a missing one, and a failed clear poisons the
record for the same reason. Timing stays operator policy (`retry_seconds`,
`idle_seconds`); the mechanism only avoids idle-poll history spam while every
tick still re-evaluates for new facts.

Every attempted selection records `runs/RUN/selection.json`: definition digest,
timestamp, task-input digest, attributes and provenance, observation freshness, prior results,
matching rule, exclusions, selected profile and classification-run reference.
Main result/error records also reference the decision and failure action.
Classification results are separate canonical run records, never counted twice
through their parent. Private task text stays in normal inputs, not selection
facts. Model sessions retain existing identity checks minus live effort keys:
profiles differing beyond reasoning effort use different session namespaces;
conversations are not transplanted. Consultations resolve each answering role through a bounded selector preview (no routing consumption, no classifier inference) instead of overriding its profile. A selector role
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
inference. Attribute bounds also apply to the merged attribute map. Classifier `inputs`
accept at most 64 proposals/decisions (0 disables that input) and a per-text
cap of 256..65536 bytes; the assembled work-input document is bounded at
128 KiB. Every validated bound combination fits that budget: over-budget
documents shed oldest items first (ties shed decisions before proposals, the
actionable work), so a large configured window degrades to the newest workload
instead of failing at runtime.
Selection configuration, input JSON and the shared state file are bounded at
1 MiB; each state table has at most 8192 entries. Classifier cache files are
bounded at 64 KiB. Existing run/tool/usage evidence bounds remain in force.

State lives under `data/selection/state.json` (version 2). Version 1 reads migrate
in memory as fixed-only state; the next update writes version 2. No probe policy
is guessed for old blocks. Updates use a shared state lock and atomic replacement; reads and preview do not mutate it. The
shared state lock is not held across inference; probe execution retains its per-contribution claim
fence, and the normal role/workspace/slot admission locks remain scoped to their work unit. Concurrent failures retain the latest block end;
this does not reserve quota, prevent already-running requests, or guarantee
account limits. State is not silently reset on parse/schema failures or
oversize data. Removing obsolete policy names does not destroy their audit
state. A probe attempt identity includes project, role, run and the selected
candidate attempt ordinal (distinct from pi-durable's request ordinal).

Persistent attempt receipts are stored only for failures matched to `probe_seconds`.
Ordinary successes, unmatched failures and fixed blocks have no receipt; the
live runtime guards duplicate selector-failure accounting, and a completed
`Engine.run` returns once under its unique run id with no resume/replay path.
While retained, a probe receipt makes identical replay return the original
action without extending deadlines/revisions or resurrecting a recovered or
expired contribution; conflicting reports for the same identity are refused.
Replay after retention pruning is not a supported product path.

At most 256 terminal (recovered or expired) contributions and their receipts
are retained. When another contribution becomes terminal, the oldest terminal
contribution is pruned together with its receipt and all its events; active
contributions and receipts are never pruned. Each contribution keeps at most
the latest 8 probe events, including an active intent/current claim. Only older
terminal events may be pruned, and the latest event/status remains visible.
Each intent event reserves 512 bytes for admission/progress and terminal
proof; progress consumes that padding while preserving at least 128 bytes for a
terminal result, and the terminal transition removes any remainder. Each state table still has at most 8192 entries, each probe event is
at most 4 KiB, and the whole state remains at most 1 MiB. At true active-obligation
capacity overflow, existing active evidence is retained and a visible
`exhausted` latch closes selector admission and new probes rather than dropping
an active obligation. A full legacy file exposes the same latch without
rewriting it. Back up the global selection directory separately from project
backups; project backups include project-local classification caches and run
evidence.

`tests/test_selection.py` and `tests/test_recovery_probes.py` use synthetic facts/driver doubles and controlled time
for failover, recovery, unknowns, classification, concurrency, publication and
usage attribution. Offline tests and actual installed Pi SDK with faux provider
are separate from live-provider authentication, quota reset and real OCI tests.
Activation remains an operator configuration change; no deployment or login is
performed by selection.
