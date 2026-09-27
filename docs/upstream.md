# Upstream references and compatibility provenance

Mizu's initial adapter targets **Pi v0.87.1**, not an unbounded latest version.
The following primary sources were inspected for its CLI, RPC, extensions,
TypeBox export and package requirements. Source inspection is distinct from
installing or executing the dependency.

- Pi CLI flags and sessions: https://github.com/earendil-works/pi/blob/v0.87.1/packages/coding-agent/docs/cli.md
- Pi package version, binary entry and Node requirement: https://github.com/earendil-works/pi/blob/v0.87.1/packages/coding-agent/package.json
- RPC framing and completion: https://github.com/earendil-works/pi/blob/v0.87.1/packages/coding-agent/docs/rpc.md
- Extension lifecycle and provider admission event: https://github.com/earendil-works/pi/blob/v0.87.1/packages/coding-agent/src/core/extensions/types.ts
- Agent tool termination contract: https://github.com/earendil-works/pi/blob/v0.87.1/packages/agent/src/types.ts
- TypeBox re-export: https://github.com/earendil-works/pi/blob/v0.87.1/packages/ai/src/index.ts
- Cache warming decision: https://github.com/earendil-works/pi/blob/v0.87.1/packages/coding-agent/src/core/cache-warmer.ts
- Environment variables: https://github.com/earendil-works/pi/blob/v0.87.1/packages/coding-agent/docs/environment-variables.md
- Podman run options: https://docs.podman.io/en/stable/markdown/podman-run.1.html
- systemd service semantics: https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
- systemd timer semantics: https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html
- MCP stdio transport: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- MCP lifecycle: https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
- Official Node downloads and verification instructions: https://nodejs.org/en/download

The adapter disables built-in/discovered tools and context, waits for a trusted
extension handshake, verifies exact model identity and listens for
`agent_settled`. `agent_end` alone can precede automatic continuation. The
extension's `finish` result uses the upstream terminate signal and must be the
only tool in its assistant turn. Per-request admission uses
`before_provider_request`; cache warming is stopped explicitly. These details
must be revalidated against any future pin.

GitHub CI action commits are pinned in the workflow. Their official upstream
commit pages are the provenance for those revisions; no assurance of perpetual
safety or maintenance is implied by pinning.
