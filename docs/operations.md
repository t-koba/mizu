# 運用・停止・復旧

## 日常の運用操作と状態管理

すべての CLI 操作は一般ユーザー権限で実行します。出力は JSON 形式（標準出力）、エラーは標準エラー出力に出力されます（`--config PATH` を指定する場合はサブコマンドの前に指定）。

### プロジェクト制御コマンド

```sh
mizu status demo   # プロジェクトの制御状態、スナップショット、予算、ヘルスを表示
mizu pause demo    # タスクを一時停止（協調的キャンセルを通知、arm 状態は維持）
mizu drain demo    # 新規実行を止め、永続セッションは安全なリクエスト境界で wrap-up (active が空で静止)
mizu resume demo   # 一時停止・drain を解除してタスクを再開
mizu disarm demo   # arm 状態を解除して安全な停止状態にする
mizu wake demo     # completed や blocked 状態のタスクに対して目標の再検討を指示
mizu cleanup demo  # 終了後に残存したラベル付きコンテナを停止・削除
```

- **状態の永続化**: arm/pause/disarm の状態はディスク上の `control.json` に記録され、OS やプロセスの再起動で勝手に解除されることはありません。
- **実行権限の制約**: 自律モデル自身には停止状態を解除したり、自分自身を arm する権限はありません。
- **二重起動防止**: 単一 Writer ロックにより、同一プロジェクトへの重複書き込みは排他制御されます。
- **待機中の表示**: `mizu status` は `admission` で実行枠を待っているロールを示します（待ちのスロット番号、待っているロール、待ち始めた時刻）。実行中（`active`）とも、一度も受付されなかったロールとも区別できます。待ちの記録は読めない場合は誰も待っていないものとして表示します。
- **cleanup の排他**: `mizu cleanup` はロールの実行中排他ロック (`locks/run-<role>.lock`) の下でコンテナを列挙・削除します。実行中のロールのコンテナは削除されず、掃除は `{"removed": 0, "skipped": "role-active"}` を返します。ロールの実行が終われば、次の掃除が残存コンテナを削除します。プロジェクト全体の掃除は、設定されたすべてのロール、プロジェクト内に存在するすべての排他ロック（プロジェクトで有効化されていない委譲先ロールも含む）、および check runner が占有中の計算スロット (`locks/run-check-slot-<n>.lock`) の両方を除外します。実行中の check job はそのスロットの排他ロックを保持するため、掃除は check job のコンテナを削除せず `{"removed": 0, "skipped": "role-active"}` を返します。委譲された子も自ロールの排他ロックを保持するため、同一プロジェクトでは同じロールの実行は一つだけです。委譲先のロールが実行中の場合、その委譲は拒否されます。
- **drain と委譲された子**: 委譲された子は drain に応じ、安全な論理リクエスト境界で停止して中断を呼出側に報告します（放棄はせず、既におこなった変更は作業空間に残ります）。したがって drain 中の親は、完了できない子に待たされません。
- **drain の意味**: drain による bound は drain として記録される（request budget を使い切ったことではない）。共有ハンドシェイクが bound の種別を運ぶので、durable 側は `drain` として報告し、pi 側も drain 拒否を wrap-up として扱う（単位は finish を記録して yield し、admission failure として中断されない）。 新規受付を止めます。`[limits].drain_yield = true` (厳密な boolean、既定値) では、永続セッションかつ非 ephemeral の実行は、進行中の provider request・tool dispatch・join 済み子実行を終えた次の安全な論理リクエスト境界で既存の縮小権限 wrap-up に移ります。通常枠を追加で認めず、最大 3 requests / 5 wrap-up tools (日次 admission は継続) で finish を求めます。seal に成功すれば通常の outcome (通常 `continue`) で公開し、セッションと workspace を保ったまま実行・workspace ロックを解放します。seal なしの正常終了・wrap-up 枠消費は `status="bounded", bound="drain"` を永続記録し、未 seal の snapshot は公開せず、health を変更しません。この記録の storage 障害や provider/protocol/公開の障害は従来どおり障害です。非永続・ephemeral 実行はそのまま完了させます（ただし委譲された子は drain に応じ停止します。前の項を参照）。`drain_yield = false` では全実行が従来どおりそのまま完了します。通常の queued waiter では yield せず、FIFO admission や wall deadline も変えません。実行中の移管・再配置や即時停止ではなく、全体の完了待ち timeout はないため、`mizu status` の active が空になるまで待ってから promote/保守へ進みます。再開には `resume` を使います。協調的キャンセルが必要なら `pause`、arm 解除が必要なら `disarm` を使います。
- **自動停止**: 連続失敗回数が `max_failures`（既定値 3）に達すると自動的に pause されます（`0` で自動 pause 無効化）。`mizu resume` は counters を 0 に戻します（ADR-016、ADR-017）。日次予算・ディスク予約・ロック待ち（`Busy`/`InfraExceeded`）は数えず延期し、`error.json` と `run_deferred` に残ります。エンジン期限・証跡上限などの `LimitExceeded` は数えます。Pi 経路の実行中予算拒否も `admission_wait` により延期します。自動 pause 時は人間可読 `reason` を保ったまま、機械可読 `pause_cause`（`kind: failure_brake`、`error_class`（例外クラス名のみ）、`roles`（対象ロール名のみ）；エラーメッセージは含まない）を `control.json` に記録します。`paused` を伴う明示更新（手動 pause・resume）は古い `pause_cause` を消去し、`paused` を伴わない更新は保持します。

### タスクの実行

```sh
mizu run demo --role worker   # 単発実行（結果 JSON を標準出力に出力）
mizu daemon demo              # 継続ポーリング実行（JSON Lines をストリーム出力）
```

`daemon` 実行時は、タスク完了ごとに結果オブジェクトが 1 行、実行延期時は `{"event": "run_deferred", "error": <string>, "time": <ISO-8601>}` が 1 行出力されます。

## 常時稼働の設定 (systemd / launchd / タスクスケジューラ)

常時稼働させる前に、必ず `mizu run` による単発実行の正常動作を確認してください。

`mizu service PROJECT` は実行中 OS のサービス管理機構に応じた定義ファイルを生成（描画）します（自動起動や有効化は行いません）。出力される `enable_units` に記載されたユニットから必要なものを選択して有効化します。なお、`on_change` のみ設定された役割にはサービス定義は生成されません（明示的な `mizu run` で実行）。

Linux (systemd user units):

```sh
mizu service demo
# 生成された service や timer ユニットファイルの ExecStart、実行ユーザー、スケジュール時刻を確認
systemctl --user daemon-reload
systemctl --user enable --now mizu-4-demo-worker.service
# 4役編成の場合のみ追加で有効化:
# systemctl --user enable --now mizu-4-demo-searcher.timer mizu-4-demo-reviewer.timer mizu-4-demo-reporter.timer
systemctl --user list-timers 'mizu-*'
journalctl --user -u mizu-4-demo-worker.service -f
```

macOS (launchd):

```sh
mizu service demo
# 生成された plist ファイルの ProgramArguments やスケジュール時刻を確認
launchctl bootstrap "gui/$UID" ~/Library/LaunchAgents/mizu-4-demo-worker.plist
# 4役編成の場合のみ追加で同名の searcher / reviewer / reporter plist を bootstrap
launchctl list | grep mizu-
```

Windows (タスクスケジューラ、専用の一般ユーザーのコマンドプロンプトで実行):

```cmd
mizu service demo
rem 生成された XML ファイルの Command、Arguments、スケジュール時刻を確認
schtasks /Create /TN mizu-4-demo-worker /XML "%APPDATA%\mizu\tasks\mizu-4-demo-worker.xml"
rem 4役編成の場合のみ同名の追加タスクを登録
schtasks /Query /FO LIST | findstr mizu-
```

`mizu daemon` コマンドは、単一の JSON ではなく JSON Lines（改行区切りの JSON）形式でイベントを出力します。タスク実行が完了するごとに結果オブジェクトを1行出力し、実行が延期された場合は `{"event": "run_deferred", "error": <string>, "time": <ISO-8601>}` を1行出力します。一方、単発実行の `mizu run` コマンドは単一の JSON オブジェクトを出力します。実行枠・ロック待ち（`Busy`）の場合、`mizu run` は `limits.admission_wait_seconds`（既定 300 秒）に従って空きを待ちます（待機上限は `parallel_runs` と同数。drain・pause・unarmed 時は待機しません）。0 を指定すると実行または取消しまで待機し、正の値ではその期限切れで `Busy` を返します。待機せずに空きを取得できた実行は期限切れでは拒否されず、受理済みの実行も該上限で拒否されません。待機キュー満杯も `Busy` です。プロバイダー選択の復旧待ちには別の `limits.busy_retry_seconds` を指定します。独立プロセス間でも FIFO で受け付け、role/workspace ロックで進めないチケットは順位を保ったままスキップします。即時実行もロック待ちのチケットをスキップしますが、実行可能な先行チケットは追い越しません。待機キューが満杯でも、即時に実行可能なら空き枠を利用します。`daemon` は従来どおり即時 `run_deferred` として次回ポーリングで再試行します。

SSH ログインセッションの切断後も systemd のユーザーマネージャーを常駐させる必要がある場合は、管理者権限で linger を有効化します。

```sh
loginctl enable-linger "$USER"
```

※環境によってはシステム管理者の承認が必要です。なお、秘密情報を systemd ユニットファイルの `Environment=` に直接記述してはいけません。また、ログインシェルで手動 `export` した環境変数はユーザーサービスに自動的には引き継がれないため、設定ファイル内では絶対パスや明示的な値を指定してください。

サービスの停止手順 (全OS共通の pause / cleanup に加え、登録したサービスを各OSの管理系で停止):

```sh
mizu pause demo
# Linux の場合:
systemctl --user stop mizu-4-demo-worker.service \
  mizu-4-demo-searcher.timer mizu-4-demo-searcher.service \
  mizu-4-demo-reviewer.timer mizu-4-demo-reviewer.service \
  mizu-4-demo-reporter.timer mizu-4-demo-reporter.service
# macOS の場合:
# launchctl bootout "gui/$UID" ~/Library/LaunchAgents/mizu-4-demo-worker.plist
# Windows の場合 (専用の一般ユーザー):
# schtasks /Delete /TN mizu-4-demo-worker /F
mizu cleanup demo
```

## データ保存構造

各プロジェクトのデータディレクトリ（`data_dir/projects/NAME/`）には、以下のファイルおよびディレクトリが配置されます。`mizu backup` の対象は `storage.py` の `MEMBERS`（下記のうち `★`）のみで、それ以外は稼働中の live 状態でありバックアップ・復元の対象外です。
- `workspace/`: 管理対象の作業コピー（backup対象外：未確定の作業内容）
- `PROJECT.md`, `project.toml`: 運用者が定義した目標とプロジェクト設定 ★
- `control.json`: プロジェクトの制御状態（arm / pause 等） ★
- `current.json`, `histories/`: 最新の確定状態を指すアトミックなポインタと不変の公開履歴 ★
- `snapshots/`, `objects/`: 不変なスナップショットマニフェストとコンテンツオブジェクト ★
- `inbox/`, `insight-archive.sqlite3`, `decisions/`, `decision-history/`: 提案（Insight）の現行作業集合、決定済み本文・証拠の永続アーカイブ、採否判断、判断履歴 ★
- `runs/`, `sessions/`: タスク実行記録とセッションログ ★
- `evidence/`: 不変な公開ID・dispatch marker、receipt-pinned CI wait の結果・ack、および `acceptance/` の正規化済み acceptance receipt（stdout/stderr と command log 本文は含まない） ★
- `check-jobs/`: 非同期チェックの上限付き live cache とコマンド証跡（backup対象外：job、raw log、wait 記録は eviction や restore 後に失われることがあります）
- `health/`: ヘルスチェック記録 ★（`capture_limit` は現在も適用されている
  snapshot 上限超過の原因（パスとサイズ）です。capture が成功すると解除され
  ます。実行の `error.json` にも同じ原因が記録され、次の実行の context に
  も渡されます）
- `observed/`, `spool/`, `maintenance/`: 観測状態、Editorキュー、保守監査記録 ★
- `artifacts/`: 生成された成果物（Markdown + 根拠データ） ★
- `active/`, `locks/`: 実行中ステータス、排他ロック（backup対象外：live状態）

各 run の記録には、開始条件、適用された policy や config の識別情報、実行コマンドや実験の結果、検証の証跡、モデルのトークン使用量、完了ステータスまたはエラー情報が保存されます。なお、run ステータスの `prepared` は公開（publish）が完了したことを意味するものではありません。`current.json` およびそれが参照するマニフェストのみがプロジェクトの「確定状態」として扱われます。`active/` は現在実行中の状態を示す一時的な表示領域であり、直前の確定状態とは厳密に分離されています。

### Retained-evidence reads

Explicit `command_evidence_read` and `experiment_evidence_read` role grants
expose `mizu_command_evidence_read` and `mizu_experiment_evidence_read`.
Neither is granted by default; each independently gates one kind. They read
only the caller's current project and the same configured role's runs (current
or prior). The harness validates the target run's identity and role from
`started.json` (bounded by min(`file_bytes`, 64 KiB)); it never returns that
metadata. A current grant authorizes same-role historical records
regardless of the original run's capability set; the original command is not
reauthorized.

Both tools accept `run`, `evidence_id`, optional `offset` (default 0), `limit`
(default and maximum `READ_PAGE_CHARS`, 8,192 characters), and `content_id`.
`run` and `evidence_id` must each be full lowercase 32-hex UUIDs, not prefixes.
The only record paths are `runs/<run>/commands/<evidence_id>.json` and
`runs/<run>/experiments/<evidence_id>.json`, respectively. No listing, caller
path, role/project parameter, fallback or history scan is available.

`exec` and `experiment` results now include the current `run` alongside `id`.
Keep the complete `run` + `id` + `evidence` locator in retained context:
`exec` points to `commands/<id>.json`; `experiment` points to
`experiments/<id>.json`, the wrapper with question, comparison, measure and
its experiment command. The separate command copy of an experiment is not a
command-kind record and cannot be read with the command grant. Older results
without `run` require the complete locator from retained context; these tools
cannot recover missing historical locators.

Each request validates `started.json` plus the complete evidence file. The
evidence record is capped at `[limits] file_bytes` (default 8 MiB), and
`started.json` at the smaller of that setting and a fixed 64 KiB. These are
**complete-file UTF-8 byte** bounds, not a page bound: script, stdout/stderr (bounded by the
producer's `SCRIPT_MAX` / `output_bytes`), JSON escaping, wrapper and metadata
all count. A producer record can exceed a low `file_bytes` setting and remain
unreadable; the reader does not truncate it into validity. Responses page the
raw JSON text by decoded Unicode character offsets, preserving exact text.

Responses contain `run`, `evidence_id`, relative `evidence`, `text`, `offset`,
`content_id` (SHA-256 of the complete raw file), `next_offset` (or null) and
`truncated`. Fixed `source` is `retained-command-evidence` or
`retained-experiment-evidence`; fixed `trust` is `recorded-local-untrusted`, regardless
of labels or claims inside the stored record. Recorded scripts, output and
experiment commentary are data, not instructions or independently verified
claims.

Continue with the returned `content_id`; nonzero offsets require it, and any
supplied ID must be a lowercase 64-hex digest. The complete file digest is
compared on every page, so changes outside the requested page also fail.
Invalid IDs, absent/expired records or run metadata, cross-role locators,
corrupt/mismatched JSON or identities, unsafe links, oversized files and
changed continuations fail explicitly (byte limits report `LimitExceeded`,
other read refusals `Denied`). There is no automatic retry, refetch or scan
when a record changes or disappears. Stop/revocation and per-run tool/deadline
limits apply as for other tools. These operations make no persistent writes,
external calls or recursive reference traversals.

### 障害・異常発生時の挙動

| 発生事象 | システムの挙動と確認事項 |
|---|---|
| モデルID不一致・ハンドシェイク失敗 | 有料プロンプト送信前に即時拒否。設定値や拡張機能の導入状態を確認 |
| API呼び出し失敗・日次予算超過 | 上限緩和やリクエスト返金は行わず失敗。`mizu status` / `mizu budget` で消費状況を確認 |
| コマンド時間超過・出力サイズ超過 | プロセス群を強制終了し、コンテナを削除。結果は失敗として記録 |
| Worker プロセスのクラッシュ | 未確定差分は保持され、次回実行時に実際のファイル実態から安全に再開 |
| 同一役割または Writer の二重起動 | 排他ロックにより後続の起動を拒否 |
| 連続失敗（`max_failures` 到達） | 自動的に一時停止（pause）し、`control.json` に `pause_cause`（`failure_brake`／例外クラス名／対象ロール）を記録。原因解消後に手動で `mizu resume`（counters を 0 に戻し、`pause_cause` を消去） |
| 成果物の生成失敗 | 直前の有効な成果物を維持。Worker の作業には影響なし |
| ディスク空き容量不足 | 新規タスクの実行を停止（監査証跡などの自動削除は行いません） |

無制限な自動再送、作業ツリーの強制リセット、他モデルへの暗黙フォールバックは行われません。外部への副作用を伴う処理は、運用者側で冪等性や承認フローを設計してください。

## 成果物 (artifacts) とクリーンアップ

Reporter は静的成果物（`artifact.md` + `evidence.json` + `latest.json`）を `artifacts/<id>/` 配下に保存します。

```sh
mizu report demo                               # 静的成果物の手動生成（LLM 不要）
python3 examples/render-paper.py ~/.local/state/mizu/projects/demo  # 閲覧用 HTML の描画例

mizu prune demo                                # 削除対象のプレビュー（dry-run）
mizu prune demo --apply --keep-artifacts 30   # 最新30件を残して古い成果物・再現可能入力を削除
```

Workspace cache reclamation is separately opt-in via `[workspace_cache]`
(see [configuration](configuration.md#regenerable-workspace-caches)). Empty
roots keep it disabled. Due cleanup runs only at a quiescent execution boundary;
it skips busy locks without pausing/draining and retains its latest result in
`maintenance/workspace-cache.json`. Workspace-cache policy remains separate from operator `mizu prune`.

Prune does not require pause, but refuses a busy workspace, configured/existing
role, editor-ingest or insights exclusion. Backup and `storage --apply` still
require pause. `[limits].automatic_prune_interval_seconds` (default 86,400; 0 disables both
automatic run pruning and snapshot GC; manual `mizu prune`/`mizu storage` stay) applies that same validated prune
(`keep_artifacts=0`, so no count-based artifact deletion) at safe execution
boundaries before admission/execution locks, alongside workspace-cache maintenance.
There is no background timer. Busy contention never consumes the cadence; an
incomplete scan or I/O failure never fails the requested run nor deletes uncertain
state. The latest bounded result is `maintenance/automatic-prune.json`; shared
smoke bundles use their independent cadence. Snapshot GC shares this per-project
due check but has a separate retention policy (below).
`[limits].run_retention_days` defaults to 31 (integer 0–3650;
0 keeps indefinitely). Only terminal, valid, old and unreferenced connected
run components expire. Current inbox/evidence, active-role, selector-wait and
blocked WorkItem target-decision references pin entire parent/child components.
Permanent ledgers remain, but historical run IDs and usage may lose full expired
run output. Preview reports bounded IDs/counts/digests and scan completeness;
apply refuses an incomplete scan. The full plan and successful completion are
recorded under `maintenance/`. Interrupted quarantine staging is restored before
the next apply rescan; committed quarantine deletion resumes instead. Dry-run
only reports leftovers. See [the retention contract](configuration.md#operator-invoked-run-retention)
for bounds, exact typed fields and interruption semantics. Shared smoke evidence has a separate data-dir-owned lifecycle (below); project
run retention never prunes it.

`[limits].snapshot_retention_days` defaults to 31 (integer 0–3650; 0 disables
snapshot GC only). At the same due boundary, without pause, GC keeps `current.json`,
its active bounded history window, continuations/fences and all durable citations
(runs, artifact evidence, live/revised/archived insights, queued/claimed editor
submissions, and every historical work-item delivery/done binding). Superseded
history generations use immutable file mtime; manifests use timezone-aware
`created_at`. Only strictly older, unreferenced entries expire. Objects are removed
only if no retained manifest uses them. The manual `storage` preview/apply still
does not expire history generations and apply still requires pause.

The automatic snapshot scan fails closed on incomplete/malformed references,
manifest timestamps, unsafe storage roots or unavailable coordination. It takes
workspace/role/check-slot/editor/outbox/publication/insights exclusions nonblocking;
busy skips without consuming the cadence. Capsule writers share the outbox
`.submit.lock`; provision access for both accounts in separated editor setups and
never unlink its live inode. CLI reference writers hold publication through their
read-to-write interval. Requested work never fails because GC refused. Inspect the
bounded `snapshots` subrecord in `maintenance/automatic-prune.json` for status,
removed history/manifest/object counts and path-set digest (or an error class),
separately from the existing run-prune result. Deletion order is histories →
manifests → unused objects; an interrupted/refused sweep may be partial and the
next due sweep rescans. External bundles remain self-contained; newly submitting
an already-expired ID cannot resurrect its project manifest. See the
[full contract](configuration.md#automatic-snapshot-retention) for roots, bounds
and age semantics.


### Shared smoke evidence retention

`mizu smoke --live` still requires explicit paid-probe consent and keeps its
4-request / 5-tool / 120-second caps. New outputs are a complete bundle at
`data_dir/validation/pairs/<32-lowercase-hex smoke_id>/`: `manifest.json`,
`report.json`, and `project/` when initialization succeeded. The CLI report
retains the probe result and adds `smoke_id`, `pair`, and `report` paths. A
failed probe also publishes a terminal fail bundle before raising the probe
error. Publication stages the bundle and renames its directory once; interrupted
unpublished staging is diagnostic evidence, never an expiry candidate.

```sh
mizu smoke-retention                 # project-free, read-only advisory preview
mizu smoke-retention --set-days 31   # operator CLI only; one shared data-dir policy
mizu smoke-retention --set-days 0    # keep indefinitely (recovery still available)
mizu smoke-retention --apply        # recompute under the global lock, then retire
```

No project pause is needed. Apply is nonblocking on
`data_dir/locks/smoke-retention.lock`; a busy lock refuses without changing
pairs. A bounded automatic sweep also runs without pause: when
`[limits].automatic_smoke_retention_interval_seconds` (default 86,400; 0 disables,
0–2,678,400) is due, the next top-level role execution attempts one
best-effort sweep using the same implementation as manual apply before it acquires admission/execution locks,
alongside workspace-cache and run-retention maintenance. Cadence state is global per
`data_dir` (`maintenance/automatic-smoke-retention.json`) serialized with expiry under
the existing nonblocking shared smoke lock, so at most one sweep runs due across projects and
busy contention never consumes the cadence. The automatic path changes no policy,
takes no project locks, never fails the requested run, never deletes on malformed,
incomplete or uncertain reference state, and leaves snapshots/objects untouched; the
latest bounded result is `data_dir/maintenance/automatic-smoke-retention.json`. Manual
preview, `--set-days` and `--apply` are unchanged. Refused plans consume the interval;
busy does not. Malformed/unsafe cadence state returns `unavailable` without a sweep
or automatic repair; inspect and repair it explicitly. The fixed-size result stores
only timestamp, status, completion counts/policy or an error class, never error text.
A write failure can leave no new result; normal quarantine recovery still applies to
an interrupted sweep. There is no background timer: the next safe boundary retries
when due. Reference writers take this lock before their record-store lock (using
the existing bounded writer wait); provider execution never holds it. Preview
neither creates lock files nor recovers quarantine and is advisory: apply never
trusts a preview. Each output list is sampled to 100 IDs with its full count
and digest; the internal scan and apply are complete, not sampled.

Only current open/deferred Insight evidence or nonterminal WorkItems/waits with
explicit `smoke_refs` pin bundles. Attach IDs with the host `attach_evidence`
API bound to the Insight revision read, or the WorkItem create/transition/wait
APIs. Insight decision seeds inherit evidence refs, including task-group seeds;
direct Insight-origin creation also inherits them. Omitting refs on a WorkItem transition inherits the item's pins; `[]` releases
its item-level pins. A blocked wait carries its own refs.
Terminal owner history retains the IDs but no longer retains the payload. A
path, prose citation, or provider/VCS run ID is **not** a pin. Restore validates
imported live refs under the same lock before exposing the new project; backups
do not include shared smoke payloads, so restore requires those pairs to exist.

Apply refuses all new retirement on unsafe, unreadable, missing, malformed,
unknown or over-bound relevant metadata in any project. Only complete terminal
pass/fail pairs strictly older than the window expire. `0` skips the new expiry
scan, not recognized quarantine recovery. Whole directories move atomically to
`validation/quarantine/<id>.pending`; pending means rollback to `pairs/<id>`.
Renaming to `<id>.committed` commits deletion. Recovery validates the retained
manifest and any remaining known members; deletion keeps that manifest until
last, so unknown or unmarked contents fail closed. A fully removed bundle with
an empty committed directory is also recoverable. Dry-run only reports these
states.
Partial I/O failure returns `complete:false`, the error class and the IDs that
actually completed; it stops before later pairs. Retry apply after resolving
the fault. `validation/retention-audit.json` retains the latest 128 successful
pair-level deletion/recovery events, never a success for a partial deletion.
Project audit/history is unaffected and permanent.

Legacy flat `validation/smoke-<16-hex>.json` and `.project` artifacts remain
untouched indefinitely: there is no migration or inferred citation scan. The
shared fixed `validation/smoke-probe-policy.md` is outside retention too. See
[configuration](configuration.md#shared-smoke-retention) for schemas and bounds.

`--keep-artifacts N` は最新の成果物を含め N 件を保持します。`[web] cache_seconds` を過ぎた共有 web-cache（`data_dir/web-cache/*.json`）の期限切れエントリも候補となり、次回 fetch で再取得されるため削除は安全に再試行できます。セッション（`sessions/`）は監査証跡として保持され、削除対象になりません（`mizu storage` で件数・バイト数を報告）。削除ログは `maintenance/prune-*.json` に記録され、バックアップ対象となります。

```sh
mizu storage demo                             # スナップショット/オブジェクトの参照別集計（読み取り専用プレビュー、削除なし）
mizu storage demo --apply                   # 未参照マニフェストと孤立オブジェクトを削除（要 pause、監査記録あり）
```

`mizu storage` は履歴・run・成果物・提案が参照するスナップショットと、未参照マニフェストおよび孤立オブジェクトの件数・バイト数・ bounded sample を報告します。あわせて共有 web-cache（件数・バイト・`cache_seconds` に対する期限切れ）の容量、および durable なセッション証跡（`session.json` 件数・ファイル数・バイト数、削除対象外）の容量を報告します。既定（`--apply` なし）は何も削除・移動・書換えしないため、writer 実行中でも安全に再試行できます。`--apply` は pause したプロジェクトでのみ実行でき、未参照マニフェストと孤立オブジェクトだけを削除します（履歴世代・run・成果物・セッション・判断・提案は保持）。存在するが読めない・壊れた・1 MiB 超・形不正の参照や走査失敗は `reference_records_skipped` に数え、既存の `snapshots.skipped`（マニフェスト）および `work_item_events_skipped` と同様に `--apply` を削除前に拒否します（不完全な走査を参照不在の証明にしない）。`reference_records_skipped` は一意のレコード/走査パス数です（active 履歴世代の重複計上なし）。JSON `null` レコード、不正な非 null スナップショット ID、履歴の名前・要素・ダイジェスト・公開先との終端不一致も不完全とします。公開ポインターと履歴の読取りにも 1 MiB 上限と symlink 非追従を適用し、読めた有効な ID はプレビューに残します。任意の run 記録や `artifacts/latest.json` が存在しないことは正常で数えませんが、成果物ディレクトリに `evidence.json` がない場合は不完全として数えます。削除内容は `maintenance/storage-*.json` に記録され、バックアップ対象となります。2 回目の実行で新規の孤立オブジェクトまで収束します。

## 人間からの提案 (Insight)

確定した事実は `mizu status`・`mizu usage`・`mizu insight list/read`・`mizu report` と run 記録から読み取ります（外部チャットツール非依存）。表示は各プロジェクト側で作ります。

人間からの回答や指示は、Insight として投入し、`wake` で再開させます:

```sh
mizu insight submit demo --title "方針の指示" --body -  # 標準入力から Markdown を投入
mizu wake demo                                         # 作業再開を指示
```

Worker が `blocked` で終了した場合、人間への質問事項が状態（state）内に番号付きリストとして記録されます。

### Explicit insight pages

```sh
mizu insight page demo --scope live --limit 1000
mizu insight page demo --scope archive --limit 1000
mizu insight page demo --scope archive --limit 1000 --cursor '<next_cursor>'
```

`Insights.list_page(scope="live", limit=30, cursor=None)` and the explicit CLI
return the same JSON envelope:

```json
{"items": [], "generation": "<sha256>", "next_cursor": null,
 "has_more": false, "cursor_expires_at": "<UTC ISO-8601>"}
```

- Scope defaults to `live`; limit defaults to 30 and must be an integer 1–1000.
  `insight list` is unchanged: a bare JSON array of at most the newest 1000 live
  summaries. `read` and `history` retain their existing behavior.
- Live pages enumerate **all** projected inbox rows, not just pending proposals,
  in ascending `(created_at, id, filename)` order. Items have the same summary
  fields as `list` (including effective decision and evidence count/timestamp).
  Generation hashes the complete projected live set, full bodies/evidence and
  effective decisions, including rows outside this page. A changed generation
  fails continuation as **stale**; restart without a cursor. Meaningless no-op
  retries do not invalidate it. Pages hold the insight lock during projection;
  live work depends on inbox size, never archive size.
- Archive pages enumerate every immutable archived `(id, rev)` row, including
  older archived revisions of a subsequently revised insight, plus explicit
  identity-only legacy tombstones (described below). Body items are bounded
  summaries with the same shape as `insight list` (id, rev, source, title,
  timestamps, `decision`, evidence count/timestamp); they carry no `body` or
  full evidence text. Use `insight read` for the exact full body. `decision`
  is the original terminal decision captured at archival (not a current
  mutable decision). Order is ascending archival sequence. The first page
  captures a sequence high-water mark; indexed keyset reads exclude later
  appends until a fresh traversal. No total count or full archive scan is
  needed per page.
- `next_cursor` is null exactly when `has_more` is false. Carry it unchanged to
  continue. Replaying the same cursor/limit returns the same page and continuation
  while the generation is valid; changing limit is allowed. Cursors have a fixed
  expiry from the first request (`limits.insight_cursor_ttl_seconds`, default one
  day), never extended by continuation. `cursor_expires_at` reports that deadline
  even on the final/empty page; it does not expire any stored insight.
- Malformed, expired, foreign-scope or foreign-project cursors fail with `Denied`
  (CLI: nonzero exit, JSON error on stderr, no partial page). Stale live cursors
  and unavailable/corrupt archives also fail explicitly. Restart a stale/expired
  enumeration; retry transient storage failures after repair. Tokens are bounded,
  checksummed opaque bookmarks, **not authorization or tamper-proof credentials**;
  project access remains the caller's responsibility. They bind to the project
  storage location, so restart after restoring/moving to a different location.
  Cancellation is normal CLI interruption: no continuation is consumed, so it can
  be replayed. No model, provider, network or external publication is involved.
- The first archive page or archive write lazily migrates the original SQLite
  `(id, rev)` schema in an atomic transaction to an AUTOINCREMENT sequence and
  unique `(id, rev)` index. Existing record/decision/identity bytes are preserved;
  legacy rowid order establishes the initial sequence. Ordinary ID reads and
  snapshot-reference scans work both before and after migration. Migration and
  page failures do not unlink inbox records. The database remains a single-file
  DELETE-journal archive at quiescence, covered by normal backup/restore.

The archive retention policy is **permanent**, for both bodies and tombstones.
`retention_days` governs decided insight live-inbox residence, and cursor TTL
only governs continuation validity. Neither deletes archive rows; there is no
archive expiry/deletion setting or API.

Closed bodies deleted by old retention before archive storage was introduced
cannot be reconstructed. On the first archive page, a **one-time identity
migration** runs under `insights.lock`, before capturing the page high-water:

- It streams `insight-ids/` once. Each filename must be a valid insight ID plus
  `.json`; each identity must be a regular, non-hardlinked, non-symlink file of
  at most 4,096 bytes. Pinned reads reject unsafe path components. JSON must be
  an object without duplicate keys or non-finite values, with matching `id`, a
  SHA-256 `sha256`, a timezone-aware `created_at`, and (if present) a positive
  integer `rev` within SQLite's signed 64-bit range. Unknown fields are not
  projected into tombstone summaries.
- An identity produces a tombstone only when its ID has neither a live inbox
  body nor any archived row. Existing live candidates are read safely (at most
  1 MiB, allowing full attached evidence) and checked as bodies; existing
  archived candidates are also checked. Unsafe directories/files, malformed
  identities/bodies, unexpected identity-directory entries, disappearing
  candidates and enumeration/I/O failures fail the page explicitly, rather
  than silently dropping a candidate. Missing identity/inbox directories are
  empty sets, as for a project that has never received an insight.
- All tombstones and the completion marker commit in one FULL-synchronous
  SQLite transaction. Cancellation, a killed writer or a failure before commit
  rolls back the import. Repair malformed/unsafe local state, then retry the
  page without a cursor. A failure after commit is also safe to retry: existing
  rows are never replaced, and the durable marker prevents rescanning. The
  schema migration above can already have completed independently; its rows
  remain intact even if identity migration fails.
- Tombstones append to the existing sequence; their import order is not
  chronological. They participate in the same indexed keyset/high-water
  pagination as bodies. After migration, pages do not scan identity files.
  Pre-migration cursors retain their old high-water and exclude imported rows;
  start a fresh traversal to include them. The database, tombstones and marker
  travel together through normal backup/restore.

A tombstone summary contains `id`, `body_unavailable: true`, validated
`created_at`, and `rev` **only if explicitly recorded in the identity** (no
assumed revision 1). It has no invented title, body, source, origin, evidence,
decision, snapshot or work-item reference. Existing decisions/revision files
are not used to reconstruct these fields. Exact ID/prefix reads keep reporting
`Denied: Insight body unavailable` (not `not found`), even if the original
identity file is later absent; ambiguous prefixes still fail. A tombstone does
not authorize submit/revise/evidence/decision/withdraw mutations. Existing
work-item origin projections use only a revision explicitly present in the
identity; otherwise the current origin revision is unknown and the item is
reported stale, not treated as a missing-work-item error. Default `insight list`,
live pages and routine model context remain live-only.

### Work-item view (read-only)

The CLI is the canonical task-list reader, including for dashboards. Within one
invocation it reuses validated snapshots and task records across the open,
closed and delivery sections. Serialized retained records are bounded to 48 MiB
(with additional Python object overhead), and path validation retains at most
8192 names. Eviction only causes another validated read. The next invocation
reads again, so changes and corruption remain visible. No persistent cache,
writer lock, recovery operation or release-specific alternate reader is used.


```sh
mizu work-items demo
mizu work-items demo --limit 25 --cursor '<next_cursor>' --closed 10
mizu work-items demo --limit 2 --closed 0
```

`mizu work-items PROJECT [--limit N] [--cursor TOKEN] [--closed N]` prints one JSON
document with `open`, `closed`, and `event_delivery` fields and never
writes, locks, recovers or deletes. The project is opened with
`Project(..., read_only=True)`.

- Options and defaults: `--limit` is the open page size (default 25, exact int
  1–100); `--cursor` is the opaque open-page continuation (default none);
  `--closed` is the number of newest closed items to include (default 10, exact
  int 0–100, where 0 reports the closed `total` with no rows). Invalid
  limits/cursors fail closed with no partial page.
- `open` is the exact `page_open` shape: items ordered by status
  (doing, blocked, open), then oldest `created_at` and ID; the exclusive input
  `cursor`, `next_cursor` (non-null exactly when another matching item follows),
  `overflow_count` (matching rows after the cursor beyond this page),
  `truncated` (`overflow_count > 0`), `unseeded_decisions` and
  `coverage_complete`. Rows carry the current origin projection and bounded
  reason previews. When a seed has a description, rows also include
  `description_preview` (at most 1024 UTF-8 bytes; open-row previews may be
  shortened further to fit the fixed row bound) and `description_truncated`,
  never the full text; v1 seeds omit both fields. By-ID reads retain the full
  immutable description, as do history records. Descriptions are untrusted
  content. Rows are at most 8192 canonical bytes and the response at
  most 1 MiB.
- `closed` holds the newest seeded terminal items first (`updated_at`
  descending with a stable ascending ID tie-break), plus `total` seeded closed
  count, `overflow_count` and `truncated`. Each row carries `id`, `title`,
  `status` (`done` or `dropped`), `origin`, `rev`, `updated_at`, `actor`, a
  bounded `reason_preview` with `reason_truncated` when a reason is stored, and
  `snapshot_id` (the verified snapshot for `done`, null for `dropped`) and
  `delivered_tree` (the digest of the tree that carries the delivery, null for
  `dropped` and for records written before the claim existed). Description
  preview fields have the same bounds/presence as open rows and are included in
  terminal work-item event payloads without changing routing or exact receipts.
  Each row also has `event_acknowledgements`: a role-name-sorted mapping of **every**
  project role whose `work_item_events` includes that status and whose effective
  `work_item_sources` route matches, to a boolean exact receipt state. `own`
  matches the origin insight submitter, never the transition actor; unknown
  origins match only `any`. Nonmatching/unsubscribed roles are absent, and no
  matches produces `{}`. True means the exact `work_item:<id>:<rev>` receipt
  exists for that role; false means it does not. This is independent of the
  ten-ID summary sample, including for older rows outside that sample.
  Reads reuse validated immutable terminal chains/payloads and fail closed on
  malformed receipts, unsafe origins or excess bytes, rather than omitting
  matching roles. Rows including the map are at most 8192 canonical bytes and
  the response at most 1 MiB.
  Drop-only legacy sidecars without a seed stay in the `work_items_legacy_read`
  audit, not here.
- `event_delivery` reports up to 100 roles subscribed to `work_item_events`
  (non-subscribers are omitted), in name order. Each has `role`, `subscribed`
  terminal types, `sources` (effective `work_item_sources`, not decision routing),
  exact `pending` and `acknowledged` counts for the
  current subscription/route, up to 10 oldest `pending_event_ids` and up to 10
  most-recent `acknowledged_event_ids`, plus truncation flags, `unknown_routing`,
  `routing_complete`, `unseeded_decisions` and `coverage_complete`. It contains
  no event payload rows. `total` and `truncated` report role coverage; the
  summary is at most 1 MiB. Counts scan validated seeds/chains and exact
  receipts, not a timestamp watermark. Unseeded legacy decisions make seeded
  coverage incomplete; unknown routing is reported independently and excluded
  in `own` mode (only explicit `any` opt-in receives it). A matching retained
  event with no per-role receipt is pending even if it predates enabling that
  subscription; receipts persist across unsubscribe/re-subscribe and backup /
  restore. These are current exact receipt counts, not lifetime totals across
  subscription settings. Dispatch batches are independent per source: the
  role-effective `work_item_events_limit` bounds terminal delivery while the
  role-effective `decision_events_limit` bounds only decisions (each a role
  override when set, else the project `[limits]` default). Delivered decision
  and terminal work-item rows, structured insight waits and dependency obligations
  carry `ack_token`; select only handled rows in `finish.ack_events`. Finishing
  alone acknowledges none of these sources. Unselected rows replay oldest-first,
  including deferrals; batch limits bound prompt size, not handling. Structured
  wait consumption matches exact registration IDs, preserving mid-run
  re-registrations. Per-role exact obligation receipts suppress only handled
  observations, never assign work or fulfill the requirement. Both apply only
  after durable result/publication; failures leave unwritten/unselected rows
  pending. Corrupt/unsafe obligation receipts fail closed without repair. CI
  waits sharing `wait_events` have no token and retain their current behavior.
  The 10-ID samples here are display bounds on a read-only diagnostic, not
  dispatch batches. Open, closed and event-delivery pages are
  individually bounded to 1 MiB (whole document at most 3 MiB plus field names).
  The view is live/non-atomic, not a scheduler verdict. Reading never
  acknowledges, repairs, recovers, rotates or writes a cursor. See
  [terminal subscriptions](configuration.md#terminal-work-item-subscriptions)
  for delivery/receipt/retry semantics.
- Live cursor semantics: the open cursor is a last-key bookmark over a live
  view. Closing earlier items does not offset-skip later rows, but membership
  is not frozen: items created, closed or transitioned between calls can move
  rows. Restart without a cursor for a fresh traversal.
- Read-only, no recovery: the view never acquires `insights.lock`, never
  creates the lock file, and never removes atomic-write leftovers. Validated
  `.new-*` orphans/aliases are ignored (never promoted); unsafe or unexpected
  names, malformed records, incomplete chains and bound violations fail closed.
  Runtime prompt projection and the `work_items_read` / `work_items_legacy_read`
  operations keep their existing locked, recovering `page_open` / `page_legacy`
  behavior.

### Waiting on one publication's CI

A blocked work item can use `wait = {kind:"ci", publication_id, checks}` through
`work_items_transition`. Obtain `publication_id` from a completed publication
receipt; do not guess a SHA from the current branch head. Supply 1–64 unique
exact check names (1–256 characters each; the serialized set is at most 2048 UTF-8 bytes). This is not an insight deferral wait.
Automatic VCS polling and its trusted adapter must be enabled before
registration; run the polling daemon to observe results.

Both all-green and terminal red results wake the current holder and deliver
bounded `wait_events` evidence with the exact receipt/SHA, check states/run IDs
and conclusion. When the provider reports them, the evidence keeps each
declared check's start, its completion and the derived job time, so the
wall time of the wait is readable from the durable record; the times stay
external-untrusted and the product recomputes the job time itself. Neither changes work-item status. Missing/pending/ambiguous
checks remain unresolved even if the branch has moved. After a successful run
the exact evidence is acknowledged once; for a new CI attempt or changed
predicate, use the normal CAS transitions to leave `blocked` and block again.

For a read-only explanation, call `vcs_read` with `op:"status"` and the exact
`publication_id`. With both `vcs_read` and `work_items_read` grants, `result.ci_waits`
explains only the caller's current blocked waits for that receipt (up to 10,
by item ID). Each declared check reports `absent`, `duplicate`, `wrong_sha`,
`nonterminal` or `terminal`, plus exact-name and wrong-SHA counts. A supplied
branch mismatch reports `branch_mismatch`; the default adapter wrapper echoes
the requested branch, so its equality is not proof of an observed branch.
`observed_checks` lists up to 32 validated check/SHA/state/run identities
from the same response, including other SHAs; both lists have truncation flags.
A selected item with safely read malformed or invalid acknowledgement content
reports `"acknowledgement":"corrupt"`; valid and absent acknowledgements omit
that field. This does not say that an event was acknowledged. The projection
reads and validates local result and acknowledgement records without returning
their bytes or changing them. Unsafe paths, oversized or unreadable records,
invalid result evidence and ledger failures refuse the status projection instead
of being classified as acknowledgement corruption. The item bound and
truncation flag apply to this diagnostic too.
Names are exact: a raw job name never aliases a workflow-qualified name.
Absence describes only this response, not failure: the exact name may appear
on a later poll. Normal `checks` stay filtered to the receipt SHA. Provider observations
are external-untrusted invocation evidence only: they neither persist poll state
nor wake, acknowledge or transition work, and status reads make no extra adapter
call. Callers without either grant and ordinary branch/SHA status reads get no
diagnostics. Existing status validation bounds and refusals still apply.

See [the full wait contract](scheduling.md#exact-sha-work-item-ci-waits) for
bounds, restart/replay and failure behavior.

### 外部公開の記録 (operator-run publisher)

The operator's own harvest publishes approved commits to the development branch
outside Mizu. Record what it published so the project keeps a durable
source-to-published identity and a CI wait can be pinned to that exact SHA:

```sh
mizu publication record demo \
  --branch mizu \
  --published-sha 0123456789abcdef0123456789abcdef01234567 \
  --code-digest 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --projection-schema 1 \
  --projection-algorithm mizu-public-source-v1 \
  --projection-digest fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210 \
  --approval 'GO mizu (insight id)'
```

`--code-digest` names the approved whole capture, not the filtered Git tree.
All three `--projection-*` flags are required: obtain them from the approved
export's `public_source.json` and independently compare the Git tree's curated
source manifest to that projection before recording. These example digests
are placeholders. A schema-3 receipt binds both identities; historical
schema-1/2 receipts remain read-only and are never rewritten.

`--source-commit` and `--run` are optional references to the published tree and
the publisher run. The record is create-only and idempotent: repeating the same
command replays the stored receipt, and a request that disagrees about the
approval or the run is refused. The published SHA and the approval reference
are the operator's assertion; Mizu never infers a published SHA from a branch
head, and CI evidence must still confirm that exact SHA.

List the recorded receipts of a project (read-only):

```sh
mizu publication list demo
mizu publication list demo --branch mizu --code-digest 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
mizu publication list demo --published-sha 0123456789abcdef0123456789abcdef01234567
```

The listing returns one bounded page: `publications` (at most 100 summaries),
`total` (the matching receipts the scan found), `truncated` (whether the page
dropped matches) and `scan_stopped` (whether a scan bound cut the search short,
so `total` is a lower bound and a receipt may exist beyond it). A record that
cannot be read is skipped, never listed unvalidated.

A role discovers the same receipts with the `vcs_read` op `publications`
(`{"op":"publications"}`, optionally with `code_digest`, `branch` or
`published_sha`); it reads local durable evidence and contacts no provider.
Use the returned `publication_id` as the `publication_id` of a `{kind:"ci",
publication_id, checks}` work-item wait.

## バックアップと復元

プロジェクトを一時停止（`pause`）した状態で取得します:

```sh
mizu pause demo
mizu backup demo /secure-backups/demo-checkpoint.tar.gz --verify
mizu restore demo-restored --archive /secure-backups/demo-checkpoint.tar.gz
mizu status demo-restored
```

- 復元先には新しいプロジェクト名を指定します（常に unarmed かつ paused で初期化）。
- パストラバーサル、危険なリンク、1 GiB（既定値）超の展開は安全対策として拒否されます（大容量時は `--max-bytes` を指定）。
- バックアップ対象は `storage.py` の `MEMBERS`（目標、設定、スナップショット、提案、成果物等）です。Git の完全な履歴、運用者共通設定（`config.toml`）、認証情報、共有日次予算は含まれません。復元内容をレビュー後、`mizu arm` で承認します。
- `insight-archive.sqlite3` も対象です。バックアップは静止境界で SQLite の未完了 rollback journal を復旧してから DB 本体を保存します。`retention_days` は決定済み Insight の inbox 滞在期間であり、本文・証拠の削除期限ではありません。`insight list` は live inbox のみ、ID 読み取りはアーカイブも参照します。全件列挙には `insight page --scope live|archive` を使います（前述）。

## バージョン更新とロールバック

新しいソースコードツリーの内容をレビューした後、`./scripts/setup.sh --stage-only` を実行します。コマンドの出力に表示された候補版の絶対パスを記録してください（初回セットアップ時を除き、通常の既定動作もこのステージング処理となります）。

更新を適用（昇格）する前に、稼働中の全プロジェクトを一時停止（pause）し、すべてのサービスおよびタイマーを停止します。各役割の実行時刻や周期を変更した場合は、`mizu service` コマンドでサービス定義を再生成し、`daemon-reload` を実行してください。

```sh
./scripts/setup.sh --promote /absolute/path/to/releases/CANDIDATE
mizu service demo  # スケジュール設定を変更した場合は再生成 (OS既定のディレクトリへ出力。--directory で指定も可能)
systemctl --user daemon-reload
mizu doctor --sandbox --sandbox-image <digest-pinned-candidate>
mizu smoke --live
# 各種テストの成功を確認した後、必要なサービスを起動してプロジェクトを resume します
```

昇格（promote）処理時には、導入時に記録されたソースコードのマニフェスト、ファイルのハッシュ値、実行権限ビット、および依存パッケージロックファイルのハッシュ値が検証されます。これは同一運用者によるステージング後の意図しない変更を検知するための仕組みであり、暗号署名の検証や悪意ある別ユーザーによる改ざんを防ぐものではありません。また、インストール済み依存ライブラリの全バイナリ検査や脆弱性スキャンの代わりになるものでもありません。

有効化が完了すると置換対象の旧コードは削除します。過去形式のデータを現行契約へ自動変換しません。

既存のポリシーファイル（`policies/*.md`）は自動更新されないため、新バージョンのテンプレートとの差分を手動で確認して反映してください。

自律ロールである Maintainer にこの昇格権限を与えることはありません。Mizu 本体の保守作業は、独立した隔離コピー環境で実施します:

```sh
mizu init harness-candidate --source /path/to/reviewed/clean/source \
  --goal examples/MAINTENANCE.md --roles maintainer,maintainer-searcher \
  --verify 'python3 -m unittest discover -s tests -q'
```

Git リポジトリからソースツリーを取り込む場合、作業ツリーがクリーンな状態（未コミットの変更がない状態）である必要があります。通常のディレクトリを取り込む場合は、不要な一時ファイルなどを除外した上でコピーされます。Maintainer による改善提案や修正結果は必ず人間がレビューし、公式リポジトリおよび新版ソースツリーに反映した上で、改めて通常のセットアップ手順に従って導入してください。

サービス名は `mizu-<プロジェクト名の文字数>-<プロジェクト名>-<ロール名>` です。旧名の定義は所有を確認して手動移行します。macOS/Windowsのcalendarには `timezone = "local"` を明示します。所有一覧と時刻基準は [契約と移行](completion-contracts.md) を参照してください。

### 記録済み根拠の説明

```sh
mizu explain demo                 # プロジェクトに有効な役割が1つの場合
mizu explain demo --role worker   # 複数役割の場合は対象を明示
```

`mizu explain` は単一役割の JSON 根拠ビューを標準出力に返します。トップレベルは `schema_version` / `generated_at` / `project` / `role` / `observation` / `project_facts` / `role_facts` です。`project_facts` は保存済み control（reasonは512文字まで）、プロジェクト全体の現在snapshot、読み取り専用budget peekです。`role_facts` は対象roleのhealth/active記録、設定済みprofile/selector、daemon/interval/calendar/on-change/decision-event/source/CI-wake設定、選択制約、最終run結果（`runs/<id>/result.json`または`error.json`からの`kind`/`status`/`finished_at`のみ）とprobe待機（`selection/<role>-wait.json`からの`next_evaluation_at`と監査run）を含みます。保留中InsightやInsight構造化waitはinbox走査が必要なため対象外とし、`unknowns`に残します。選択制約ではblock key、sponsor project/role、rule、probeのnext timeと`until`、観測時刻での`probe_due`を示します（実際のprobe起動やsponsor認可を確認した意味ではありません）。selectorが設定されたroleでは、run履歴を走査しないため`current_choice`は`unknown`です。selectorがなければ設定済みprofileを示します。出典はローカルのbounded current recordと静的role設定に限り、任意のerror textやInsight本文は出力しません。control/health/activeは各64 KiB、selection stateは既存の1 MiB上限、budget peekは当日ファイル4 MiB上限で、budget peekは`gc`を行いません。コマンドは状態を書き換えず、engine/modelやVCS、service managerを呼びません。欠損・不正な個別記録は`absent` / `unavailable`として値を捏造せず示します。設定やproject/role指定が無効ならstderrにエラーを出して失敗します。修復後はコマンドを再実行してください。

選択結果の予測、run 履歴の走査、Insight 本文の読み込み、queue や process/service-manager の確認は行いません。正確な次回起動、外部サービス状態、queue 順位、active process の生存・yield 可否、および pending Insight/structured wait は未検査であるため `unknown` として明示します（役割の最終記録 outcome は `role_facts.last_outcome` で報告します）。snapshot の outcome はプロジェクト全体の outcome であり、この役割の outcome ではありません。この出力は複数ファイルをまたぐ非 atomic な記録時点の観測で、scheduler verdict ではなく、役割が実行されるかどうかを示すものではありません。既存の `mizu status` は従来どおりです。

### Local driver-dispatch measurements

`runs/<run>/dispatch-attempts.json` is best-effort measurement evidence (schema
1), not admission or cache-affinity policy. Each physical driver call made by
`Engine._execute` has a record keyed by that context's `execution_attempt`:
initial dispatch, selector fallback/retry, wrap-up and missing-finish
continuation. Consult/delegate children retain their own records and parent-run
reference; join by `(run, execution_attempt)`, not attempt number alone (children
can start at zero). Calls directly to a driver outside the Engine are not covered.

The record includes the actual selected role/profile and configured adapter
engine, class (`normal`, `consult`, `delegate`, `ephemeral`), phase, UTC start/end,
monotonic elapsed seconds and `started`/`returned`/`raised` status. Returned means
the driver returned, not that the unit published successfully. Timing includes
local evidence overhead, not just provider inference. Missing profile/engine
identity is null with an unknown reason. No prompts, native session identifiers,
private paths, provider errors or arbitrary provider payloads are copied here.

Usage totals and distinct observed provider/model pairs come **only** from
`runtime_usage`/`runtime_models` entries for that attempt, never the cumulative
unit result or configured model name. Each token total is null unless every
reported observation has a valid recognized value for that field; explicit zero
is retained. Usage status is `unknown`, `partial` or `known` (all five fields
reported); these describe reported observations, **not completeness of provider
reporting**. Unknown keys are bounded, missing model attribution is marked, and
missing/unrecognized usage is never synthesized as zero. Adapters that supply
only final result usage, not runtime observations, therefore remain unknown.
This ledger does not replace the existing usage dashboard or accounting.

A passive observer of the driver's local `session_record` selection retains an
opaque content-bound key, local rotation generation, prompt mode and rotation
reason only if profile/key/generation match the captured prompt projection.
`prompt_projection.json` now names its selected `profile`. Selector fallback
keeps its actual selected profile in the dispatch record but cannot borrow the
original profile's projected session: session/prompt fields are null with
`prompt_profile_mismatch`. Other unknown reasons cover nonpersistent sessions,
missing projection/driver observation and session mismatch. Matched continuation
prompts use mode `continuation` and do not repeat an earlier rotation reason.
These are local selections, **not proof of provider conversation/cache residency,
a cache hit, or a TTL guarantee**. Native provider state can still change.

Retention is a tail of at most 64 attempts and 256 KiB of strict finite JSON per
run; oldest records are removed to satisfy either bound and `dropped_attempts`
reports the loss. Model pairs are capped at 16, provider/model text at 256
characters, unknown usage keys at 20 (256 characters each). This per-run
measurement log is compressed/dropped by `mizu prune` according to
`event_log_compress_days` and `event_log_retention_days` (defaults 7/31 days;
0 disables each stage). Atomic fsynced
replacement writes a start before calling the driver, updates local session
selection when observed, and settles timing/attempt-only facts in `finally`.
A crash can leave `started` without an end; later observations may still exist in
`runtime-usage.json`, joined by attempt. Write/measurement failures are swallowed
and do not retry, cancel or change model execution; evidence can consequently be
missing or stale and is not an audit-completeness guarantee. Scheduling remains
FIFO with unchanged slot/role/workspace locking, provider TTL and session policy.

`mizu usage` also exposes a read-only shadow view `cache_affinity` built only from
those same `dispatch-attempts.json` files: at most the 200 most recently modified
run directories are scanned (`runs_scanned`, with `runs_truncated` marking a
larger candidate set), and each file is capped at the dispatch-evidence byte
bound. Symlinks are refused; malformed or unreadable files are counted as
`cache_affinity.skipped_records` without failing the command. `total` counts
eligible attempt rows in this scanned window; at most 200 are returned, with
`truncated` marking omitted rows. Each row keeps only
run id, role, execution-attempt index, start/end timestamps, engine, the dispatch
`profile` (a nonempty string of at most 256 characters, otherwise null), a single
`provider`/`model` pair, `model_attribution_unknown`, the local
session key/generation and prompt mode when the key is a 64-character lowercase
hex digest and the generation is a nonnegative exact integer (otherwise null),
usage status, raw input/cache-read/cache-write token counts, the derived
`prompt_input_tokens`, and `cache_read_share_of_input`. The denominator follows
the built-in engine usage contracts: Pi/pi-durable and Claude sum mutually
exclusive uncached-input, cache-read, and cache-write buckets; Codex input counts
include cached input, so its reported input total is used directly (see the
[Codex usage type](https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs)).
The Pi formula matches the pinned Pi 1.3.0 [cache-stat implementation](https://github.com/earendil-works/pi/blob/v1.3.0/packages/coding-agent/src/core/cache-stats.ts);
Anthropic documents the same sum for Messages input in its [rate-limit usage
fields](https://platform.claude.com/docs/en/api/rate-limits). Unsupported
engines, invalid input totals, nonpositive/overflowed prompt totals, or
missing/invalid Pi/Claude buckets produce a null `prompt_input_tokens`; for
Codex, `input_tokens` alone is sufficient, while missing or invalid cache-read
detail leaves its share null. For every engine, a cache read greater than the
derived denominator also produces a null share.
`cache_read_share_of_input` divides cache-read by
`prompt_input_tokens`, not by the raw `input_tokens` field when that field is only
uncached input. Only a single known model is attributed: `provider`/`model` are
copied only when the attempt reports exactly one valid pair (each a nonempty
string of at most 256 characters) with `models_unknown` false and
`models_truncated` false; otherwise both are null and
`model_attribution_unknown` is true. Native provider session identifiers,
arbitrary model lists, and arbitrary usage payloads are never copied. Rows are ordered
most-recent-run first (newest attempt first within a run), capped at 200 rows with
`total`/`truncated` marking the loss. This is observed cache-read share and local
session grouping only, not proof of provider cache residency or TTL, and it does
not affect scheduling.

### 監視処理の資源使用

POSIX のエンジン出力は pipe readiness で待機し、入力がない間の読み取りを
繰り返しません。出力・EOF・既存のレコード上限は従来どおり処理します。
汎用子プロセスの終了・出力エラーは監視を起こし、停止権限・動的期限は
最大50ミリ秒周期で再確認します。pipe 待機は100ミリ秒以下で解除可能で、
Windows の匿名 pipe は既存の PeekNamedPipe 経路を使います。
イベント証跡の公開は保持済みバッファから直接行い、同量の bytes コピーを
作りません。保持上限・切り詰めの記録・原子的公開は変更しません。
スナップショットの除外照合は同じ fnmatchcase パターンを結合した式を
最大16ポリシー分だけ保持し、パス名や検証結果をキャッシュしません。


### Active work holds and review age

`[limits].work_items_doing` defaults to **1** active `doing` item per role;
`roles.<name>.work_items_doing` may override it (integer **1–100**, the existing
page-row ceiling). Entering `doing` or transferring an existing `doing` hold
to a different role counts validated current holders across the ledger atomically
with the transition CAS under `insights.lock`. The actor comes from the role,
never a tool argument. A denied claim appends nothing; re-read and release
(`open`), block, complete or drop existing work before retrying. Blocked waits
consume no doing slot. Lowering the cap never invalidates history: existing
over-limit holds remain readable and can be released, blocked, completed or
dropped. Same-holder scope/delivery revisions consume no new slot; a revision
that transfers the hold checks the recipient's capacity as a claim.

Open-work rows (including read-only CLI status), by-ID explanations, submitter
status and ready-work prompt rows expose `doing_since`, `doing_age_seconds` and
`doing_stale`. The first is the timestamp of the start of the current uninterrupted
`doing` run, derived from the validated append-only chain; same-status revisions
do not reset it. Non-doing records have null start/age and false `doing_stale`.
Age is nonnegative whole seconds, separate from `updated_at` / ready-work
`age_seconds` and origin `stale`. `[limits].work_items_stale_seconds` defaults to
**86400** (24 hours; integer 1–31536000); review becomes stale at exactly that
elapsed duration. A future start after clock rollback has age zero and is not
stale. Malformed history fails closed, never an invented age. No stored records,
page/history limits, ready-set signatures or wake rules change. These fields are
review visibility only: no automatic expiry, reassignment, writer-authority
change or admission bypass.

### Bind delivery before closing a work item
With `work_items_transition`, first move the item to `doing`, then append a
same-status `doing` revision with `snapshot_id` naming the exact retained
published snapshot that delivers its current accepted scope (the binding event
stores `snapshot_id` as null and persists the delivery object; `snapshot_id`
is refused for other statuses). Use the returned
revision for the next CAS. Bind before history eviction; a manifest existing
on disk alone is not sufficient. Binding and publication serialize under the
publication lock, before the insights lock.

Once bound, ordinary `done` names that exact ID as `snapshot_id` and requires
valid embedded verification or matching late retained acceptance. Independent
workspace changes and subsequent history-index eviction do not invalidate a
binding. Scope changes require another explicit binding before closure; bindings
are never silently cleared. A dropped-to-done correction requires an already
recorded binding and independently retained acceptance, leaving the earlier
dropped revision untouched. No other terminal reopening is available.

On stale CAS, re-read; on missing/corrupt evidence, stop and repair through
operator evidence recovery rather than editing the ledger or snapshot. Retry
after cancellation by reading the current revision first: an append may already
have committed. These operations publish nothing and schedule no work.


## Open-work capacity

`[limits].open_work_items` caps new nonterminal WorkItems per project (provisional
default **100**, **0 disables**). Open, doing, and blocked items count; done and
dropped do not. Lowering the cap below the current backlog preserves all items
and their lifecycle operations, but refuses new seeds until enough work closes.
The cap is independent of prompt page size and FIFO order; producer admission can also hold explicitly opted-in roles at this cap (see Producer admission below).

On `OpenWorkItemsFull`, inspect the reported `current_count`, `limit`, and
`requested_delta`; the refused decision/create/reconcile committed no new work.
Use the paginated `work_items_read` view to review existing work. Defer or reject
the input, or accept with `work_item_intent="none"` and merge its scope into an
existing item through the usual CAS transition. Multi-task decisions are
all-or-nothing: do not assume any subset committed. An exact retry of an already
committed request is safe even at capacity. Completing or dropping items releases
capacity immediately for subsequent transactions; no separate quota reset is
needed.

## Producer admission

`[limits].producer_inputs_per_source` holds congested producers (provisional
default **20**, **0 disables**). Both numbers are congestion thresholds chosen
from current project evidence, not measured optima: the default per-source
bound 20 matches the highest observed per-source count among the current 53
unresolved live proposals, and the open-work default 100 stays above the
current 56 seeded open/doing/blocked items. Tune either to project capacity.

Only configured non-writer project roles that hold `submit_insight` are gated;
the writer remains free to resolve, merge, defer, or reject input at capacity.
The gate runs before any model or run-slot use, when their role-name source's current unresolved live
input count (undecided plus deferred; resolved and archive-only history
excluded) reaches the per-source limit or the authoritative open/doing/blocked
count reaches `open_work_items` only for a role with the explicit
`roles.<name>.held_at_capacity = true` opt-in (default false). The per-source
threshold remains independent; disabling the open-work hold does not disable
it, and the work-item seed cap still applies to all roles. `mizu status`
reports each held role in `backpressure.held` with its reason and a `thresholds`
map containing the exact configured bound(s). Operator, vcs, and
editor input channels are never role names, so they are never held. The check applies to direct,
scheduled, event-triggered, and daemon admission, and runs before any event or
cursor is acknowledged; a held attempt (`skipped: producer-held`) claims no
run directory, slot, or lock, consumes no event, and leaves every pending
input durable. Fail closed: when the required load cannot be read, the
producer is held. A run already in progress may finish; this is an admission
threshold, never a cancellation.

Release follows the documented wake-semantics default: held producers resume
only through existing schedules or subscriptions. A skipped run consumes no
decision, terminal-work-item, wait, CI, or operator event and advances no
cursor, so repeated state-based triggers coalesce through the existing durable
stores (decision history receipts, work-item events, waits, CI wake state)
instead of a duplicate queue or an implicit schedule. The next ordinary
schedule tick, interval/calendar firing, event trigger with pending input, or
daemon readiness re-evaluation admits the producer once its counts drop below
the limits (after inputs are decided or open work closes) or after an operator
raises or disables a limit. No separate wake is added and none is needed.

`mizu status` reports the congestion view under `backpressure`: both
configured limits, the current open-work count, per-producer unresolved input
counts, held producers, and hold reasons (`per_source_full`, `open_work_full`,
`load_unavailable`). Counts come only from the current project's Insights live
inbox and WorkItems ledger; no private or ignored state is read. Status
attempts both current counts even when their bound is 0. An unreadable metric
is reported as `null` with `load_ok: false`; it holds a producer only when
that metric's bound is enabled (invalid limits also hold fail-closed).
Admission counting rejects unsafe, unreadable, oversized or malformed live
rows and decisions, including ID/filename mismatches and invalid revisions
or decision actions; tolerant prompt/list projections are unchanged.

### Failed cooperative safe-point seals

When configured safe-point preemption attempts its finish-only seal, the host
first durably creates `safe-point.json` under the writer/workspace exclusions.
After the checkpoint claim succeeds, a seal, continuation-preparation, or
publication failure leaves that fence and all workspace files intact; the
original run's terminal evidence is retained. If stop, authority, or a deadline
wins before the claim succeeds, the host removes only that unsealed marker and
keeps the existing path. Ordinary failures outside this optional handoff do not
create the fence.

Every later writer still takes ordinary FIFO admission. Before any dispatch or
workspace use, an unresolved fence rejects a different writing role. The original
role can run a single bounded recovery dispatch with only its currently granted
`files`, `read`, `diff`, and `finish` operations: no mutation or engine tools,
selector inference, fallback, or missing-finish follow-ups. It uses the existing
workspace, not a materialization of the previous snapshot. The prior attempt's
finish intent, if present, is supplied as data, not a prescribed outcome. Current
stop, authority, budget and deadline checks still apply. A disabled quantum does
not erase an existing fence or authorize ordinary work in its place.

Recovery publishes the model's normal finish outcome; existing run-bound policy
may record `bounded` instead. Only `continue` prepares a new continuation through
existing exactly-once admission; `wait`, `blocked`, `done`, and `bounded` do not
force re-entry (`done` still requires passing verification). An exact
unpublished prepared continuation can be retired while starting recovery; a
previously reserved successor is first reconciled by its existing admission
owner, never replayed. It may therefore take a subsequent ordinary wake to begin
recovery. No failed attempt's original evidence is rewritten by recovery.

The fence clears only after the exact run's retained seal/snapshot is published
and its `continue` intent, if any, is atomically committed. On restart after
publication but before clearing, admission validates the current pointer/history, exact run,
input parent, durable finish intent and continuation commitment before clearing;
a result file or an unreferenced history generation alone is not proof.
Malformed/ambiguous evidence, a moved head, revoked profile/finish authority, or
an unsuccessful recovery leaves the fence unresolved. Run retention pins its
source/current attempt, snapshot reclamation pins its base/target, and workspace
cache cleanup skips fenced workspaces. Backup retains the marker. A restore
publishes a separate operator checkpoint and remains paused; it does not
implicitly resolve an old fence against that different head. If normal recovery
is impossible, preserve the workspace and evidence for explicit operator review;
do not delete the marker or reset the workspace merely to admit another writer.

The local marker has a fixed version-1 schema (`version`, `role`, `profile`,
`source_run`, `run`, `base`, nullable `snapshot`), a 4096-byte link-safe read bound,
and no model-write API. Its owner serializes mutations under workspace exclusion
then publication lock; creation/replacement and removal use durable filesystem
primitives. No new configuration or external provider call is introduced.

### Shared check cache and status

A running check may hold the configured shared check-cache lock across checkout,
execution and return. Status then reports `effective_verification: null` and
`effective_verification_diagnostic: {status: "uncomputable", reason:
"cache_owned", owner_job: null}`. The null owner is unknown, not proof of a
particular owner. Historical snapshot verification is separate. Retry status or
cache-aware joins after the lock is released; they do not wait for the running
check.
Malformed evidence, unsafe paths and I/O errors are not cache-busy results.
See [check jobs](check-jobs.md) for the cache and proof contract.

Verification requests made while the configured check cache is owned queue as
explicit unbound demand, never as proof or a shared result. The runner binds the
actual cache under its exclusive lock before staging; unsafe inputs and legacy
missing bindings are refused. Identical unresolved requests in the same run
coalesce, including after start. Terminal requests are addressed by their job ID;
a subsequent submission is new demand, and distinct identical concurrent
demand requires a token the interface does not offer, or waiting until terminal.
Normal queue limits and cancellation still apply.

### Publication lineage observations

The optional VCS-adapter `lineage` object on a successful `push`/`pr` response
is captured create-only with the publication receipt when valid. The adapter
observes the project's operator-owned `project.toml` `[vcs.lineage_target]` when present; the product
compares the response repository, remote and ref to the expected values
independently and binds the descriptor identity and version into the new fact
(schema 2). Missing or invalid facts do not turn a successful push into an
error. Existing receipts without lineage are never rewritten, and legacy
unbound receipts are never upgraded retroactively. Neither `publication_id`
nor `code_digest` incorporates lineage.

An operator-run publisher may record the same bounded observation with
`mizu publication record --lineage '<json>'`. The JSON value is capped at
4096 UTF-8 bytes, parsed as finite JSON, validated with the same shape and
published-SHA binding as the product path, and stored create-only. When
the selected project's `project.toml` `[vcs.lineage_target]` is configured the CLI binds the current descriptor
into the new fact and the observation must match it; without configuration
the fact stays unbound legacy. With lineage the external receipt carries
schema 4 with exactly the schema-3 fields plus `lineage`; without it the
receipt stays schema 3 with no lineage. The publication ID never incorporates
lineage, so the same publication keeps one ID. A conflicting lineage on retry
is refused, and a receipt without lineage stays `unknown`.

For `vcs_read` `status` addressed by `publication_id`, the adapter receives
the established status request containing the receipt's branch and published
SHA; when the selected project's `project.toml` `[vcs.lineage_target]` is configured the request additionally
carries `lineage_target` (`repository`, `remote`, `target_ref`, `version`)
naming the expected promotion target, and `push`/`pr` requests carry the same
descriptor. There are no other lineage-specific request fields. In the same
response as ordinary CI checks it may return one fresh `lineage` object
(otherwise omit it or return null):

* `schema`: integer `1`, the raw adapter observation schema. Mizu adds the
  configured target binding when it stores a new fact (schema 2).
* `repository`: 64 lowercase hex digits, a stable **non-secret** repository
  identity digest shared between push and status observations. It identifies
  the repository behind the expected remote; do not return URLs containing credentials.
* `remote`: the observed remote name, which must equal the configured
  `lineage_target.remote` when a target is configured (no implicit `origin`);
  `target_ref`: the observed full ref, which must equal the configured
  `lineage_target.target_ref` (for example `refs/heads/main` or
  `refs/heads/release-1`; no implicit `main`).
* `target_oid` and `published_oid`: full lowercase 40- or 64-hex Git object
  IDs of the freshly observed destination and the receipt's published commit,
  in the same object format.
* `merge_bases`: an array containing exactly one full OID, the complete result
  of the unique merge-base calculation, in that same object format; an absent,
  empty, or multiple result is not evidence. `target_is_ancestor` is a boolean
  and must agree with whether that sole merge base equals `target_oid`.
* `observed_at`: an aware ISO-8601 timestamp (at most 40 characters), taken
  during that adapter invocation after observing the remote target and
  ancestry. An outside-invocation timestamp is not fresh proof.

The operator-owned adapter is responsible for observing the actual remote ref,
resolving the exact published object, obtaining **all** merge bases and
checking ancestry against the same observation; Mizu validates the bounded
shape and bindings, not the adapter's Git claims. If the remote, history, or
unique result cannot be observed, the adapter must omit the object, not guess.
The regular adapter size and timeout limits and cancellation behavior apply;
there is no extra call or automatic retry. An unavailable lineage field does
not suppress otherwise valid CI status. If the whole status call fails, it
still fails normally rather than inventing CI or lineage facts.

The exact-publication status `result.lineage` contains current `status` and
`reason`, historical `receipt_status` and `receipt_reason`, the validated
receipt `receipt` and current `observation` (null when unavailable). These
facts are also retained in the normal run-local VCS read evidence. The
project-specific `project.toml` `[vcs.lineage_target]` descriptor selects the promotion target:
without it readiness is `unknown` / `unconfigured` and ordinary publish and
CI still succeed. With it, current `ready` / `target_ancestor` requires a
valid bound receipt (schema 2 carrying `expected_repository` /
`expected_remote` / `expected_target_ref` / `expected_version` equal to the
configured descriptor) and a fresh valid bound current observation with the
same binding, the same repository, remote and target ref, and published OID,
with current positive ancestry. The observation time must fall within the
adapter invocation. The target OID may have moved; then `receipt_status` /
`receipt_reason` is `stale` / `target_moved`, whether or not the new target
is an ancestor. Otherwise historical standing matches current standing. For
the same target OID, contradictory saved and current ancestry is `unknown` /
`ancestry_conflict`, never ready. A changed descriptor makes prior bound
facts unusable without rewriting them: `unknown` / `descriptor_mismatch`
(receipt `stale` / `descriptor_mismatch`); first publish a new receipt under
the new descriptor. Legacy unbound receipts (schema 1, `origin` /
`refs/heads/main` without a descriptor binding) stay readable history but
report `unknown` / `unbound` and never become ready retroactively.

`unknown` also uses `receipt_missing_or_invalid`, `observation_missing`,
`observation_invalid`, `repository_mismatch`, `target_mismatch`,
`unconfigured`, `unbound`, `descriptor_mismatch`, or `not_ancestor`. Without a
valid, matching current observation the historical standing is unknown too:
movement has not been established. Wrong repositories, remotes or refs
against the configured target, wrong published OIDs, ambiguous merge bases
and invalid or nonfresh times are invalid, never ready. The same projection
applies to external receipts: schema-2/3 receipts without lineage and
schema-4 receipts with invalid nested observations stay `unknown`; only
schema-4 receipts whose bound lineage matches the configured descriptor can
become ready. New product and external facts bind the configured descriptor
identity and version when it is present. Without configuration, a retained
schema-1 fact remains unbound and can never become ready retroactively merely
by later configuring values that match its response. The publication ID never
incorporates lineage. These are external-untrusted observations at a point in
time, not a promotion gate: they do not prevent subsequent remote movement,
invoke promotion, gate any external consumer such as `dash-server.py`, or
enforce any condition on an external consumer.
