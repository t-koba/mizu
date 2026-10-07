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
anchored changes and bounded history. It does not include live writable code,
all private conversations, operator credentials or control-plane APIs. Start a
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
