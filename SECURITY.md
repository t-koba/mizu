# Security policy

Mizu 0.1.x is pre-acceptance software until the applicable deployment gates have
been completed. There is no guaranteed response SLA or independent security
audit. Security fixes target the maintained release line; unsupported old pins
must not be assumed safe indefinitely.

For a vulnerability in a public Mizu repository, use that repository's private
vulnerability reporting feature if enabled. Do not post credentials, exploitable
private infrastructure details or sensitive source in a public issue. If private
reporting has not been enabled, request a private contact channel without
publishing exploit details. Repository maintainers should enable it before
accepting external deployments.

Include the release and dependency-lock hashes, affected boundary, a minimal
sanitized reproduction, observed vs expected behavior and whether rootless
isolation was actually tested. Describe mock-only results as such. Do not run
adversarial experiments against systems you do not control.

Review `docs/security.md` for trusted components and exclusions: same-host UID
compromise, kernel exploits, model semantic correctness, monetary caps, total
volume quota and authenticated public hosting are not solved by this product.
