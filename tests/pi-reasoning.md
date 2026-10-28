# Pi reasoning replay upgrade gate

Run offline against both adapters' operator-provisioned dependencies:

```sh
node --test tests/pi-reasoning.test.mjs
python3 scripts/check.py
```

The normal check runs this as `node-pi-reasoning-replay-tests`, alongside the
existing `scripts/check-pi-sdk.mjs` gate. Both adapters' installed versions must
match their manifests. Missing dependencies fail closed
unless that exact gate is explicitly waived with `--allow-not-run`; a version
mismatch cannot be waived. The test never downloads dependencies.

## What runs for real

- Mizu's production `register()` loaded as a coding SDK extension, with only
  bridge I/O injected; the SDK's `createAgentSession`, agent/tool loop, model runtime,
  `session.compact()` and SessionManager context projection.
- Mizu's production `buildBridgeTools()` shared with the durable launcher, with
  only bridge I/O injected; the durable SDK's Harness, tool loop, file-backed SQLite close/reopen,
  `resume()`, completed-unit request-ID deduplication, subsequent submission,
  `compact()`, summary placement and context projection.
- Each adapter's **own installed pi-ai** Anthropic Messages response parser,
  transcript normalization, message transformation and HTTP request serializer.
  A test-only injected provider delegates to the real `stream`/`streamSimple`
  functions. Only their `fetch` transport is replaced by a faux endpoint; no
  serializer, history builder or persistence implementation is mocked.

The endpoint emits synthetic reasoning-bearing SSE responses and rejects replay
requests unless **every retained assistant's** reasoning blocks match the
original independent fixture, in order: whitespace/Unicode text, opaque
signatures, signed empty thinking, and redacted thinking payloads. A tool-call
response must survive into the next actual tool-result HTTP request, where both
adapters must deliver the exact distinctive host-result JSON text under the
matching tool-call ID, without an error flag. Tool arguments and bridge operation
are also checked. This exercises Mizu's result mapping, not generic probe tools:
Pi coding uses `content`; Pi durable 1.3 uses `output`. A mere tool-result block
with empty text is insufficient. A completed durable unit's
history must survive a real store reopen and reach the provider on the next unit
without rerunning its tool. Both SDKs perform real compaction, demonstrably remove
some assistant history and keep some, then send two further turns. Surviving
message identity is taken from the real compacted context; expected reasoning
bytes are always taken from the original response fixtures, never copied from
that context. Negative controls reject removal, altered text/signatures/redacted
data and reordered reasoning.

## Distinct serialization contracts

`serializeConversation()` formats old history as text for compaction summaries
(and, in the coding SDK, branch summaries and bug reports). That is **not** the
model-request replay serializer. The test lets Pi generate these real summary
requests and verifies they are user text requests, but imposes no thinking
preservation requirement on their text. In particular, an upgrade that
intentionally omits thinking from compaction-summary text must not fail this
gate. Summarized-away assistant messages do not need verbatim replay; retained
assistant messages do.

## Limits

The replay coverage above is a real installed-SDK integration test, not a
live-provider or installed CLI test. It uses a test-only model/provider, no production profile
changes, no network and no inference. Signatures are opaque fixtures, not
cryptographically validated. Coverage is same-model Anthropic Messages replay;
it does not claim other providers' serialization, cross-model reasoning
conversion, crash-mid-stream recovery, branch/bug-report text formatting, or
Mizu adapter RPC/extension wiring. Existing bridge and durable tests cover
separate adapter contracts. No persistent-exec-session mode is implemented here.


## Native provider catalog control path

The same offline SDK gate also exercises a fake reviewed provider extension
through the production catalog launcher and ordinary Pi RPC `get_state` (no
prompt/inference). It lists an absent-from-bundle model with provider-filtered
refresh, restarts from the provider-published cache, then explicitly pins that
model and resolves it in the ordinary launcher. A second registered provider
records that it never receives a network-enabled refresh. Offline/auth failures
retain cached native rows; overlay-only and missing hooks are unsupported, and
unknown freshness is not inferred from the shared store's timestamp. Separate
Python control-plane tests cover explicit `--refresh`, bounded single dispatch,
selection preservation and pi-durable's offline, no-launch/no-credentials catalog
inspection: it matches Pi's unsupported native-listing result when no extension is configured, or
`extension_provided_model_not_supported_by_engine` with a configured extension
resource. The latter diagnoses an unsupported source, not proof that the pinned
model requires it; ordinary bundled/explicit pinned resolution is unchanged.
These fixtures prove adapter wiring, not a live provider or OS sandbox boundary.
