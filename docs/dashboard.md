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

Payload structure ( `src/mizu/dashboard.py`):

| Field | Content |
|---|---|
| `project`, `published_at` | Identity and pointer timestamp |
| `control` | `armed`, `paused`, short `reason`, `updated_at` |
| `snapshot` | `id`, `code_digest`, `created_at`, `outcome`, `summary`, `state`, recorded `verification`, `goal_digest`, `wake_at` |
| `needs_operator_input` | `true` exactly when the snapshot outcome is `blocked` |
| `pending_insights` | Newest pending proposals up to `[limits] pending_insights` (`id`, `source`, `title`, `created_at`, `base_snapshot`, slim decision) |
| `pending_count`, `pending_total`, `pending_truncated`, `answered_count` | `pending_count` is bounded by `[limits] pending_insights` (counting newest undecided and deferred proposals); `answered_count` is the total count of decision records on disk. These are not mutually exclusive: deferred proposals remain pending and are counted in both |
| `recent_decisions` | Latest 10 decisions selected by `created_at` (`id`, `action`, `created_at`, truncated reason) |
| `health`, `active` | Per-role failure counters and in-flight markers |
| `latest_artifact` | Copy of `artifacts/latest.json`, or null |
| `budget` | `{used_requests, limit_requests, day, bytes, reaped}` — **request counts, not money**. `bytes` is the day-file size; `reaped` is the count of expired day-files collected under `retention_days` |
| `usage` | Token facts: `totals`, `recent_groups` (trailing calendar days selected by `[limits] retention_days`; renamed from `groups`), and newest 200 per-run `recent_entries` with timestamps, provider, model, engine, and token breakdowns |

`usage` aggregates provider-reported token metrics across runs. In `usage.recent_groups` (renamed from `summarize()`'s `groups`), counts are summarized by `(day, provider, model)` alongside observed `engines` (Pi counts provider requests; codex/claude count driver invocations), retaining groups in the trailing `retention_days` calendar days. Finer-grained analysis (e.g. time-of-day pricing) can use `recent_entries` or raw `runs/*/result.json` files. Unrecognized fields and token shapes remain visible under `unrecognized_keys` / `unknown_shapes`; arbitrary numeric fields do not become tokens, and overflow scans are explicitly flagged with `truncated` / `entries_truncated`. Currency pricing and dashboard presentation remain operator policy.

Bounds: `pending_insights` and `pending_count` follow `[limits] pending_insights`, `recent_decisions` selects the newest 10 records by timestamp, decision prose truncated to 500
characters (flagged), `recent_groups` uses `[limits] retention_days` calendar days. No workspace file contents, no secrets, no model
calls, no network. Typical payloads are tens of KiB.

Lifecycle: generations are disposable. `[limits] dashboard_keep` (default
30, 0 keeps all) keeps the newest N generations including the live
`latest.json` target, which is never a candidate; older content-addressed
generations are `mizu prune` candidates (paused project, dry-run preview by
default, `--apply` writes a `maintenance/prune-*.json` audit). Symlinks,
`latest.json`, `index.html`, and non-digest names are never candidates.
`mizu storage` reports dashboard capacity (documents, bytes, live ID, keep,
candidate count/bytes plus a bounded sample) alongside session and web-cache
capacity. Re-publish after restore; capacity without bound is reported, not
silently kept.

Trust: everything shown is recorded harness state plus agent-authored prose
(state, summaries, proposal titles, decision reasons). Treat prose as data,
not proof. Verification status is the configured-commands result bound to a
code digest, not a whole-goal oracle.

Retry/cancellation: collection is read-only and lock-free; publication is
idempotent (same facts → same ID, pointer rewrite is atomic). Concurrent
publishers serialize on a publication lock; every entry file stays
readable. Re-run the command to retry.

Evidence and failure behavior: the dashboard is a disposable projection, not
evidence. Snapshots, runs, decisions and artifacts remain the record. On any
collection/publish error the command fails loudly and the previous pointer
is kept; no partial HTML is ever emitted by the harness (HTML is operator
policy, below). Dashboard files are reproducible projections: backup
intentionally ignores `dashboard/` and `prune` reclaims generations beyond
`dashboard_keep`; re-publish after restore.

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
