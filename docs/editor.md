# Editor: human-triggered, genuinely readonly

Mizu does not replace an existing Editor harness. It exports one immutable
snapshot and supplies a minimal stdio MCP server. The Editor CLI, model and
provider are chosen by the operator. Remote-only Editors that cannot use this
stdio transport need their own reviewed adapter; support is not assumed.

## Export and capsule

```sh
mizu editor export demo /private-exports/demo-001
```

The export includes snapshot metadata, source files, the goal, compact state,
anchored changes, bounded history and effective search settings
(`editor-search.json`). It does not include live writable code, all private
conversations, operator credentials or control-plane APIs. Start a
new export for a new question that requires newer state. An export never changes
under an existing question.

Build an operator-reviewed image containing the chosen stock Editor CLI and
Python 3.11 or later. Pin its digest. Its own vendor-specific MCP registration
uses the contents of `examples/editor-mcp.json`; the common `mcpServers` shape is
an example, not a claim that every vendor accepts that configuration file.

```sh
./scripts/editor-capsule.sh \
  --image 'REVIEWED_EDITOR_IMAGE@sha256:VERIFIED_DIGEST' \
  --bundle /private-exports/demo-001 \
  --outbox "$HOME/.local/state/mizu/projects/demo/spool/editor" \
  --env-file /private-config/editor-credentials.env \
  -- CHOSEN_EDITOR_COMMAND ARGUMENTS
```

The outbox must be the dedicated, marked Mizu outbox, not a project/config/home
directory. The capsule source mount and root filesystem are readonly, with
private temporary storage and one writable proposal outbox. Editor inference
has explicitly enabled outbound networking; this differs from the networkless
experiment sandbox. Its provider may receive exported code.

Pass `policies/editor.md` to the Editor through its normal system/instruction
configuration. This guidance is not the filesystem permission boundary. The
capsule is that boundary. The Editor may write its own temporary files or create
proposal JSON; it cannot directly change the shared Worker code. It could send
invalid files or flood its writable outbox; ingestion is bounded and rejects
malformed entries, and a dedicated volume quota is needed for total bytes.

## MCP surface

| Tool | Function |
|---|---|
| `get_status` | Snapshot ID, time, goal, state, actual recorded verification |
| `list_files` | List exported paths |
| `read_file` | Bounded safe read with hash verification |
| `search_files` | Literal bounded text search; no executable regex or shell |
| `get_changes` | Anchored published diff |
| `submit_insight` | Append a proposal file to the dedicated outbox |

Search accepts a nonempty literal query up to 1,000 characters. Operator policy
in top-level `[editor]` is captured at export time, not read from live config:

```toml
[editor]
search_max_bytes = 8388608  # 8 MiB of exported file data per request
search_max_matches = 50
search_match_chars = 1000  # leading characters of each matching line
```

All three are positive integers. The byte and match counts accept the TOML
signed-64-bit maximum; excerpts accept at most 8,192 characters (the fixed
`MAX_EDITOR_SEARCH_MATCH_CHARS = READ_PAGE_CHARS` safety ceiling). Search visits
paths in sorted order, skips files above the remaining scan budget or the fixed
128 KiB per-file `PREVIEW_BYTES` ceiling, and verifies read-file hashes. Matching
lines retain their line numbers and are clipped to the configured excerpt size.
The search tool description states the effective policy for this export.

`editor-search.json` is a strict JSON object with exactly those three integer
fields; export writes it in the same atomic staging tree as the snapshot. Only
absence selects legacy defaults (8 MiB / 50 / 1,000). Present malformed, incomplete,
unknown-key, duplicate-key, out-of-range, oversized (above the fixed 4 KiB read
cap), linked or otherwise unsafe settings fail closed. Exports are immutable;
changing config requires a new export and does not change a running capsule.

Canonical search results, including `skipped` path names, are accumulated only
up to `MAX_FRAME // 4` (256 KiB). This fixed response safety reserve keeps both
the structured result and its escaped MCP text copy plus JSON-RPC envelope below
the 1 MiB frame. Skips, reaching the match limit or exhausting the response budget
set `truncated: true`; on response exhaustion search stops, so `skipped` is also
only a bounded prefix, not a complete inventory. There is no search continuation
cursor. All configured defaults and the query schema remain unchanged.

There is no `write_file`, shell, patch, commit, deploy, pause, budget-change or
harness-update tool. JSON-RPC writes only protocol records to stdout. MCP
lifecycle negotiation supports the declared 2025-06-18 protocol and compatible
older revisions; it does not implement HTTP/OAuth or every optional MCP feature.

After submission, the Worker daemon ingests proposals even while idling.
Explicit ingestion is also available:

```sh
mizu insight ingest demo
mizu insight list demo
```

Source identity is stamped by ingestion, not trusted from proposal prose. The outbox accepts only `title`/`body`/`base_snapshot`; any `origin`/`source` fields are quarantined. Trusted host callers may record their actual origin alongside the authority channel (`mizu insight submit --origin ...`); model submissions keep runtime-bound provenance with unknown origin. Origin never grants operator approval.
Worker accepts, changes, defers or rejects with a reason. A human goal change
belongs in the operator-owned `PROJECT.md` while paused, not disguised as an
Editor technical suggestion.

**Running this MCP server on the host and exposing other unrestricted Editor
shell/file tools is not equivalent to using the capsule.** Do not make a
readonly security claim for such a configuration.
