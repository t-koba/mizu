# Extension contracts

Start with policy/configuration changes. Add mechanism only when the existing
capabilities cannot express a necessary operation safely. Every new mechanism
needs a typed contract, explicit grant, resource bounds, evidence, cancellation,
error behavior and tests. Do not add arbitrary host callbacks for model prose.

## A new role without new runtime classes

Create a policy in the private policy directory and add a `[roles.NAME]` table
with profile, workspace, capabilities and optional schedule. Add that role to a
project's operator-owned `project.toml`. Regenerate units for schedule changes.
The runtime does not require a new subclass or a new queue. Reuse `submit_insight`
for observers and retain one integrating writer.

## A new inference engine (not a new provider)

Engines are routed generically: `profiles.*.engine` explicitly selects `pi`,
`codex` or `claude` through `src/mizu/drivers.py`, and all engines share the
`execute(context, prompt, profile=...)` contract plus the `mizu-bridge` MCP
proxy (`src/mizu/mcp_proxy.py`). Do not add per-model branches in policy or
runtime code. A fourth engine needs, in this order:

1. `adapters/<engine>/contract.json`: managed entry point, required public capabilities
   and completion/evidence shape. `doctor` and `check-cli` derive probe commands,
   required flags and expected exports from this file (`adapter_contract()`), and
   declared exports must appear in the launcher `--check-contract` report; drift
   fails instead of passing silently. A one-shot probe is first measured from
   process spawn through exit under a 150 s ceiling, then run under the per-engine
   bound: 12× the largest recorded interval, with a 60 s floor and 150 s ceiling.
   A completed refusal contributes a measurement; a measuring run that does not
   complete leaves the floor in place. The record can also contain a managed
   runtime's spawn-to-first-protocol-response interval, so the probe and runtime
   endpoints are not equivalent: the record keeps the larger interval and the
   endpoint that measured it, and the published evidence names that endpoint.
   Managed Pi, Claude and Codex use that bound for their initial handshake;
   Codex's later app-server RPCs retain their 30 s cap. A new managed driver
   should start its timer before child launch, apply the bound only to the
   initial handshake, and record the first protocol response even when it is
   refused, with the runtime endpoint. `pi-durable` runtime startup remains on
   its existing transport-deadline path. A record this host cannot read or
   write measures nothing, so the published evidence carries no cost and the
   floor bound. A stopped probe is
   not evidence of a missing capability: product-selected stop reasons remain
   primary, with a termination signal as secondary detail (ADR-042). A child
   that answered and reported a failure keeps its own answer: the capability
   message names the child's exit code and the text it produced, so no
   message ends with an empty detail.
2. `src/mizu/<engine>.py`: trusted argv builder, isolated per-run config,
   event parser, `requires_sandbox = True`, no new host authority.
3. `command_for` entry plus `engines.NAME.command` trusted argv config.
4. Fake-runner contract tests plus a real-subprocess MCP proxy test; live
   `doctor`/`smoke --live` gates stay separate and unpinned until reviewed.
5. `docs/configuration.md`, `docs/security.md` and `docs/testing.md` deltas.

## A new provider/model

Add a profile with exact provider/model IDs supported by the installed Pi. For
custom compatible endpoints, use the dedicated Pi model registry in the form
specified by the pinned upstream documentation. Change a role's profile or add
a permitted consultation alias. Validate with `doctor` and `smoke --live` before
running a real project. There is no hardcoded model recommendation and no
assumption that every model can use every tool schema or thinking level.

## Search adapter

`web.search_command` is an operator-owned argv array executed without a shell.
The executable receives one JSON request on stdin and returns one JSON object on
stdout. stderr is diagnostic. The request is:

```json
{"query":"question chosen from current project context"}
```

The response is:

```json
{"results":[{"title":"Source title","url":"https://allowed.example/source","summary":"Why it may be relevant"}]}
```

Actual source hosts must be configured in `web.hosts`; `.example` here is only a
schema illustration, not a built-in service. Results are bounded and normalized.
Keep the executable fixed, validate query length, set its own API limits and
store credentials in its own operator-owned private configuration if needed.
Never interpolate a query into a shell command. No provider key is implicitly
forwarded from the Pi credential file. The built-in alternative is RSS/Atom feed
indexing with lexical matching. Empty feeds and empty adapter mean no discovery.

`fetch` returns a receipt identifying original URL, final URL, retrieval time,
content hash, type and extracted text. Reports should distinguish what the
source says from what a local experiment measured. Repeated identical content
fetched live at different times has separate receipts. Continue fetch/probe with
`receipt_id` plus `offset`/`limit` only, or search with `result_id` plus
`offset`/`limit` only: same-run continuation reads verified immutable evidence,
never a second retrieval/probe/search. ID and original request fields cannot be
mixed. Live fetch receipts retain exact bytes; text-only cache hits remain
pageable but are not experiment inputs. Optional experiment `source_receipt`
mounts verified raw fetch bytes read-only at `/work/input/body`, retaining the
`external-untrusted` label and existing sandbox network policy. It accepts no
source path and performs no refetch, extraction or implicit source execution.
See [configuration](configuration.md#immutable-web-continuation-and-experiment-input)
for bounds, evidence and failure behavior. HTML script/style content is
removed; PDFs and image-only pages are not interpreted. Add a separately isolated
PDF extractor only with an explicit grant and tested resource bounds.

## New tools

The schema subset lives in `protocol.py`: object, string, integer, boolean, array
and enum with bounds, plus string `minLength` and full-match `pattern`
constraints. Additional fields are denied. Add the capability to strict
config validation, its tool definition, a dispatch branch in `Context.handle`,
and tests proving unauthorized and post-finish calls are denied. Add a fake-RPC
contract test and a real integration test where the new boundary needs one.

Model-facing `exec` strings are shell commands **inside** an already constrained
container. Host executables always use argv arrays. Never connect an arbitrary
model command to `subprocess(..., shell=True)` or a host shell.

## Changing Pi versions

Keep current engine events, tool registration and dependency locks in
`pi.py` and `adapters/pi/`. Update `contract.json`, both direct pins, the
installer and doctor capability checks, and the reviewed lock in one change. Compare
tagged SDK/RPC/tool APIs, especially ModelRuntime admission, `terminate` and
`agent_settled`. Run Python contract tests, Node extension tests, actual Pi
startup, live read-only smoke and a real container write/verify run. Tests with
`tests/fake_pi.py` check our protocol handling, not upstream compatibility.

A future different harness can implement `execute(context, prompt, profile=...)`
and the same capability/evidence contract. The production configuration has no
mock/unsafe driver selection. Test dependency injection remains in tests.

## Trusted CI acceptance adapter operation

The optional host-only VCS `acceptance` operation has a stricter provenance
contract than `status`: independently authenticated exact tested source,
workflow definition, immutable run/attempt/check IDs and complete declared
command/coverage/environment facts. Implementations must fail rather than infer
these from green display status or code-controlled artifacts. The operation is
not available through `vcs_read`. See [the full request/response protocol and
bounds](ci-acceptance.md). No provider adapter or equivalence policy is enabled
by this extension alone.
