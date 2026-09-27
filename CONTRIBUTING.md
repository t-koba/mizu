# Contributing

Keep mechanisms small and policy explicit. Prefer a TOML/Markdown adjustment to
new runtime machinery when it preserves the required boundary. Add a failing
regression test before fixing a defect. Avoid provider-specific defaults,
implicit network calls, host-shell fallbacks and automatic update promotion.

Run `python3 scripts/check.py` and `scripts/test-install.sh`. Changes to Pi,
container flags, filesystem publication or Editor permissions require relevant
real integration evidence as well; local mocks alone are not sufficient.
Do not run paid model calls automatically on untrusted pull requests.

Use descriptive names, type hints on public interfaces and small modules.
Preserve JSON stdout and error exit codes. Bound all model-originated inputs,
outputs, execution and concurrency. Treat Unicode, cancellation, partial writes,
retries and restart behavior as ordinary test cases. A capability must have both
allow and deny tests. Documents must state limitations and migration impact.

Commit no private configuration, model transcripts, code from private projects,
credentials, dependency caches, generated backups or workstation paths. New
dependencies require justification, license review and a reviewed lock update.
Source-only Python core currently depends on the standard library; introducing
an install framework or service needs a demonstrated benefit.

Contributions are under the repository's MIT license. Do not sign on behalf of
another person or add invented maintainers, testimonials or compatibility claims.
