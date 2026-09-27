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

Engines are routed generically: `profiles.*.engine` selects `pi` (default),
`codex` or `claude` through `src/mizu/drivers.py`, and all engines share the
`execute(context, prompt, profile=...)` contract plus the `mizu-bridge` MCP
proxy (`src/mizu/mcp_proxy.py`). Do not add per-model branches in policy or
runtime code. A fourth engine needs, in this order:

1. `adapters/<engine>/compatibility.json`: CLI name, pin basis, `required_flags`
   for the `doctor` drift check, `forbidden_argv`, protocol/completion shape.
2. `src/mizu/<engine>.py`: trusted argv builder, isolated per-run config,
   event parser, `requires_sandbox = True`, no new host authority.
3. `command_for` entry plus `codex_command`-style trusted argv config.
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
fetched at different times has separate receipts. HTML script/style content is
removed; PDFs and image-only pages are not interpreted. Add a separately isolated
PDF extractor only with an explicit grant and tested resource bounds.

## New tools

The schema subset lives in `protocol.py`: object, string, integer, boolean, array
and enum with bounds. Additional fields are denied. Add the capability to strict
config validation, its tool definition, a dispatch branch in `Context.handle`,
and tests proving unauthorized and post-finish calls are denied. Add a fake-RPC
contract test and a real integration test where the new boundary needs one.

Model-facing `exec` strings are shell commands **inside** an already constrained
container. Host executables always use argv arrays. Never connect an arbitrary
model command to `subprocess(..., shell=True)` or a host shell.

## Changing Pi versions

Keep version-dependent flags, events, schema registration and package pins in
`pi.py` and `adapters/pi/`. Update `compatibility.json`, both direct pins, the
installer and doctor pin checks, and the reviewed lock in one change. Compare
tagged CLI/RPC/tool APIs, especially `before_provider_request`, `terminate` and
`agent_settled`. Run Python contract tests, Node extension tests, actual Pi
startup, live read-only smoke and a real container write/verify run. Tests with
`tests/fake_pi.py` check our protocol handling, not upstream compatibility.

A future different harness can implement `execute(context, prompt, profile=...)`
and the same capability/evidence contract. The production configuration has no
mock/unsafe driver selection. Test dependency injection remains in tests.
