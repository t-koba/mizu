# Development instructions

This file guides contributors working on Mizu itself; it does not grant an
executing role extra permissions. Preserve mechanism/policy separation as an
architectural principle, not merely an implementation detail. Mechanism provides
capability without prescribing behavior. Keep mechanism minimal (KISS): do not
add code to enforce behavior, heuristics, or decisions that belong in operator
policy. When considering a restriction, ask: *does this provide a capability, or
enforce a behavior?* Never burden mechanism with unnecessary constraints.
Preserve the single-writer publication contract. Do not add automatic
deployment, secret access, host command execution or model-specific defaults.

Run the offline checks and add focused regression tests. Keep test-only drivers
out of production configuration. Do not change a failing test into a passing
claim by weakening a safety invariant. Distinguish fake-RPC tests from actual Pi
integration, argv mocks from actual rootless isolation, and request count from
money. Never include private environment paths or user data in public fixtures.

For new interfaces document schema, bounds, trust, retry/cancellation, evidence
and failure behavior. New policy is Markdown/TOML; new mechanism belongs behind
explicit grants. Executing Maintainer roles work in their own candidate projects. Repository
contributors update the current development source; deployment remains an
explicit operator action.

No external publication without recorded human approval: pushes and PRs require an accepted `GO <branch>` proposal bound to the exact code digest.
