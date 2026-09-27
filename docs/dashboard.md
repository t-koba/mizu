# Dashboard and operator I/O

This page standardizes the small common mechanism every project gets, and
where per-project presentation policy lives. No Slack, Discord, Telegram or
any other chat service is involved: the handoff is files plus the `mizu`
CLI over SSH or an access-controlled file transfer.

## Mechanism (fixed): `mizu dashboard`

`mizu dashboard PROJECT` collects **already-recorded facts only** and
publishes them as static JSON:

- `dashboard/<sha256>.json` — one UTF-8 document, content-addressed.
- `dashboard/latest.json` — atomic pointer `{dashboard, snapshot,
  published_at}`. The entry file is completed first, so a failure never
  leaves a half-replaced pointer.

Payload schema (v1, `src/mizu/dashboard.py`):

| Field | Content |
|---|---|
| `project`, `published_at` | Identity and pointer timestamp |
| `control` | `armed`, `paused`, short `reason`, `updated_at` |
| `snapshot` | `id`, `code_digest`, `created_at`, `outcome`, `summary`, `state`, recorded `verification`, `goal_digest`, `wake_at` |
| `needs_operator_input` | `true` exactly when the snapshot outcome is `blocked` |
| `pending_insights` | Up to 30 pending proposals (`id`, `source`, `title`, `created_at`, `base_snapshot`, slim decision) |
| `pending_count`, `answered_count` | Unanswered vs ever-decided proposals |
| `recent_decisions` | Last 10 decisions (`id`, `action`, `created_at`, truncated reason) |
| `health`, `active` | Per-role failure counters and in-flight markers |
| `latest_artifact` | Copy of `artifacts/latest.json`, or null |
| `budget` | `{used_requests, limit_requests, day, bytes}` — **request counts, not money** (`bytes` is the day-file size) |
| `usage` | Token facts (see below): `totals`, last 31 day-groups, per-run `recent_entries` with full timestamps |

`usage` groups assume one rate per (day, provider, model). Time-of-day
off-peak rates (or any finer structure) must use `recent_entries` (newest
200 runs: `run`, `finished_at`, `started_at`, provider, model, engine, token splits)
or the raw `runs/*/result.json` records — the day-group sums alone cannot
price those. Groups additionally list the `engines` observed for that
(day, provider, model); request counts compare only within one engine
(Pi counts provider requests, codex/claude count invocations). Raw records are never modified or deleted by this mechanism
(prune removes only reproducible inputs and old artifact documents), so no
information is irreversibly lost. Provider shapes that are not recognized
stay visible as `other_tokens` / `unrecognized_keys` / `unknown_shapes`;
partial scans are flagged with `truncated` / `entries_truncated` instead of
being presented as complete. Whether a dashboard shows estimated cost, and
at which operator-pinned rate table, is presentation policy, not mechanism.

Bounds: pending 30, recent decisions 10, decision prose truncated to 500
characters (flagged). No workspace file contents, no secrets, no model
calls, no network. Typical payloads are tens of KiB.

Trust: everything shown is recorded harness state plus agent-authored prose
(state, summaries, proposal titles, decision reasons). Treat prose as data,
not proof. Verification status is the configured-commands result bound to a
code digest, not a whole-goal oracle.

Retry/cancellation: collection is read-only and lock-free; publication is
idempotent (same facts → same ID, pointer rewrite is atomic). Concurrent
publishers serialize on atomic file replacement; every entry file stays
readable. Re-run the command to retry.

Evidence and failure behavior: the dashboard is a disposable projection, not
evidence. Snapshots, runs, decisions and artifacts remain the record. On any
collection/publish error the command fails loudly and the previous pointer
is kept; no partial HTML is ever emitted by the harness (HTML is operator
policy, below). Dashboard files are reproducible projections: backup and
prune intentionally ignore `dashboard/`; re-publish after restore.

## Policy (per project, outside mizu)

HTML dashboards are per-project policy and do not live in mizu. Each
project builds its own from zero against two stable fact sources:

- `dashboard/latest.json` → `dashboard/<sha256>.json` (status, questions,
  decisions, artifact pointer, budget counts, token facts), and
- `mizu usage PROJECT` (full token facts with per-run timestamps).

`examples/render-dashboard.py` is intentionally not a template: a bare
mechanical viewer that dumps the payload as escaped text in one file, so an
operator can eyeball raw facts in any browser. It defines no sections,
panels, styles per project, or workspace conventions. Copy nothing from it
into a real dashboard except the escaping discipline.

## Standard Q&A without chat apps

- Worker → operator: finish `blocked` with numbered questions in the state
  (what was tried, what input unblocks). Optionally grant the worker
  `submit_insight` in `config.toml` so it can also file proposals as
  questions; the default role set is unchanged.
- Operator → worker, from anywhere with SSH or file access:
  `mizu insight submit PROJECT --title ... --body -`,
  then `mizu wake PROJECT`. The worker triages via `insights`/`decide`.
- Freshness: `mizu report PROJECT && mizu dashboard PROJECT &&
  python3 examples/render-dashboard.py <project-root>`,
  then copy `dashboard/index.html` (or the JSON) through an
  access-controlled channel. A refresh timer is operator-owned; the harness
  ships no listener and no push integration.
