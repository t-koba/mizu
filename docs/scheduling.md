# Scheduling compatibility map (current behavior)

This is a record of current role scheduling behavior, not a target design.
It separates the code mechanisms (admission, locks, service rendering) from
the schedule settings (`daemon` / `interval_seconds` / `calendar`, event
subscriptions, `on_change`, `ci_wake`) that operator policy selects and
`src/mizu/config.py` validates.

## Schedule modes and additive subscriptions

At most one of `daemon`, `interval_seconds`, `calendar` may be set per role
(`src/mizu/config.py` rejects combinations). Event subscriptions
(`decision_events`, `work_item_events` with their `*_sources` routes) are
independent triggers: they may accompany a schedule or run event-only on
platforms with file triggers; they do not count as a second schedule mode.
`on_change` is a skip gate, not a trigger — `Engine.run` in
`src/mizu/runtime.py` returns `skipped: unchanged` when the code snapshot is
already observed and nothing else admits the run.

The shipped example (`config/config.example.toml`) exercises each pattern:

| Role | Workspace | Schedule | Additive gates |
|---|---|---|---|
| worker | write | `daemon = true` | writer-daemon `should_run` gate |
| searcher | read | `interval_seconds = 1800` | none |
| reviewer | read | `interval_seconds = 900` | `on_change = true`, optional `decision_events` / `work_item_events` (commented) |
| reporter | read | `calendar = ["07:00", "19:00"]` | none by default |
| consult | read | none (explicit-run only) | none |
| maintainer | write | `interval_seconds = 21600` | none |
| maintainer-searcher | read | `interval_seconds = 21600` | none |

A role with neither a schedule nor subscriptions stays explicit-run only
(`event_watches` in `src/mizu/services.py` returns no entry for it). An
event-only role uses file triggers on Linux/macOS; Windows currently renders
no task for an unscheduled event-only role, as noted below.

## Wake sources versus delivered inputs

`should_run` (`src/mizu/runtime.py`) is the writer-daemon readiness gate. It
first rejects unarmed/paused/draining control, then checks changed goal
digest and operator wake-generation. If the saved outcome is `done`, it returns false at this point: only a
changed goal or operator wake-generation can make a `done` writer daemon
ready. Inbox, routed events, CI, `wake_at` and progress are not checked for
that snapshot (covered by
`tests/test_runtime.py::test_done_does_not_reopen_for_news`).
For other outcomes, it checks inbox generation; role-routed decision/terminal
events and due structured waits (`_role_events_due`); due CI reds for opted-in
roles (`_ci_due_for`); expired `wait` `wake_at`; and finally the no-progress
self-admission guard for `continue`/`bounded` units
(`progress_disposition != parked_no_progress`).

Progress self-admission uses project `[limits].progress_signals` (example
default: `tool`, `code`, `decision`, `insight`, `input`; `state` is
optional), `no_progress_continuations`, and `no_progress_backoff_seconds`.
It bounds continuation of writer daemons; it is not a platform trigger.

Non-write daemon readiness is only control state: the daemon loop in
`src/mizu/runtime.py` runs a read role at its idle cadence when the project
is armed and neither paused nor draining, without consulting `should_run`.

A readiness signal does not itself name a delivered event. The normal
prompt projects pending insights; decision events, terminal work-item events,
due waits, and CI facts are fetched by separate readers with their own role,
route, and batch policies.

Scheduled timer dispatches are plain one-shot runs: `mizu run` without
`--event` (`src/mizu/cli.py` passes `event_trigger` only for `--event`).
Event-triggered dispatch (`mizu run --event`, invoked by the platform file
triggers below) admits only on a substantive decision event, a terminal
`work_item_events` row, a due structured wait, or a due CI red for that
role, and otherwise exits with `skipped: no-event` before allocating a run
directory or any model use. For `on_change` roles whose code snapshot is
already observed, `Engine.run` otherwise skips unchanged-code dispatch unless
there is a substantive decision, terminal event, due wait, or due CI red.
Writable roles additionally bypass that skip for changed goal, operator wake,
inbox generation, prior-wait expiry, or progress self-admission; read-only
on-change roles do not use those snapshot-wide wakes or progress. A changed
code snapshot bypasses the skip for either workspace mode. The recorded
`admission` label is `decision` / `work_item` / `wait` / `ci` / `change` /
`schedule`.

Deferrals ride along in `own`-source mode but do not admit a run on their
own; with `decision_sources = "any"` a matching action — deferrals
included — admits one focused run (`Engine.run` filters `substantive_events`
by source mode, and `_role_events_due` applies the same rule). Terminal
routing is filtered independently by `work_item_sources`. Delivered
obligations (`pending_obligations` in `Engine.run`) ride along when the role
runs for other reasons and never admit a run on their own.

CI-wake default: `ci_wake` defaults to true for `write` roles and false
otherwise (`src/mizu/config.py`). When VCS polling is enabled, red results on
the configured CI wake branch set can wake opted-in roles when a failure/check
or published head changes, or the configured `ci_rewake_seconds` elapses;
status remains separately available to prompts. **Current behavior to revisit,
not a future guarantee:** for outcomes other than terminal `done`, the generic
inbox-generation check in `should_run` runs before the CI-specific gate, so a
writable daemon can still wake on a VCS-sourced insight through the generic
inbox signal even with `ci_wake = false`. Do not read `ci_wake = false` as
excluding every VCS-derived input from a writable daemon's generic wake.

## Platform triggers

One schedule model is rendered per platform by `src/mizu/services.py`
(`event_watches`, `_render_systemd`, `_render_launchd`, `_render_windows`;
covered across hosts with `system=` in `tests/test_platform.py`). `event_watches`
selects non-daemon roles with event subscriptions and scheduled writable roles
(the latter also need to reevaluate their own structured waits); daemon roles
recheck their wake policy on idle ticks instead of file triggers.

- Linux (systemd user units): timers (`OnBootSec`/`OnUnitInactiveSec` for
  intervals, `OnCalendar` for calendar entries) invoke plain `mizu run`;
  daemon roles get a service with restart policy instead of a timer. Every
  watched role additionally gets a `-event.service` (oneshot,
  `mizu run --event`) plus a `.path` unit with `PathChanged` watches on
  `decision-history`, `waits`, and — when `work_item_events` is set — the
  `work-items` parent.
- macOS (launchd): `StartInterval` / `StartCalendarInterval` plists invoke
  plain `mizu run`; daemon roles get `KeepAlive` + `RunAtLoad`. Every watched
  role additionally gets an `-event.plist` with `WatchPaths` on the same
  directories and `mizu run --event` arguments.
- Windows (Task Scheduler XML): schedule triggers only — `TimeTrigger` with
  repetition for intervals, `CalendarTrigger` per `calendar` entry,
  `LogonTrigger` for daemons. There is no file trigger in the current
  schema: scheduled subscribers still see event inputs at the next timer
  firing (admission applies on every dispatch), but **an event-only role
  with no schedule currently gets no Windows task at all**. This gap is
  current behavior to revisit, not a desired guarantee.

Path/WatchPaths notifications are coarse reevaluation hints, not a
durable transactional queue. If a write is coalesced or a dispatch is busy,
there is no trigger record to replay; eventual pickup depends on a later
evaluator call (for example, a daemon tick, a scheduled run, another path
notification, or an explicit run; `src/mizu/services.py` and
`docs/configuration.md` terminal-subscription section).

## Workspace and lock isolation

`Engine._admit` (`src/mizu/runtime.py`, lock resources asserted in
`tests/test_admission_queue.py`) holds dispatch locks for the whole run:
every role takes `locks/run-<role>.lock`; `write` roles additionally take
the project `locks/workspace.lock`. FIFO slot admission itself lives in
`src/mizu/admission.py`.

Workspace selection (`Engine.run` in `src/mizu/runtime.py`): `write` roles
run on the project workspace; any other role runs on `runs/<run-id>/input`,
which is created empty for `workspace = "none"` and otherwise filled by
materializing the published snapshot (`project.snapshots.materialize`).
No role gets an empty run-local input by accident: read roles observe the
same content-addressed snapshot the writer published, mounted read-only by
the sandbox — never the live workspace, never an unseeded directory.
