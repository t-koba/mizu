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
- **drain の意味**: 新規受付を止めます。`[limits].drain_yield = true` (厳密な boolean、既定値) では、永続セッションかつ非 ephemeral の実行は、進行中の provider request・tool dispatch・join 済み子実行を終えた次の安全な論理リクエスト境界で既存の縮小権限 wrap-up に移ります。通常枠を追加で認めず、最大 3 requests / 5 wrap-up tools (日次 admission は継続) で finish を求めます。seal に成功すれば通常の outcome (通常 `continue`) で公開し、セッションと workspace を保ったまま実行・workspace ロックを解放します。seal なしの正常終了・wrap-up 枠消費は `status="bounded", bound="drain"` を永続記録し、未 seal の snapshot は公開せず、health を変更しません。この記録の storage 障害や provider/protocol/公開の障害は従来どおり障害です。非永続・ephemeral 実行はそのまま完了させます。`drain_yield = false` では全実行が従来どおりそのまま完了します。通常の queued waiter では yield せず、FIFO admission や wall deadline も変えません。実行中の移管・再配置や即時停止ではなく、全体の完了待ち timeout はないため、`mizu status` の active が空になるまで待ってから promote/保守へ進みます。再開には `resume` を使います。協調的キャンセルが必要なら `pause`、arm 解除が必要なら `disarm` を使います。
- **自動停止**: 連続失敗回数が `max_failures`（既定値 3）に達すると自動的に pause されます（`0` で自動 pause 無効化）。`mizu resume` は counters を 0 に戻します（ADR-016、ADR-017）。日次予算・ディスク予約・ロック待ち（`Busy`/`InfraExceeded`）は数えず延期し、`error.json` と `run_deferred` に残ります。エンジン期限・証跡上限などの `LimitExceeded` は数えます。Pi 経路の実行中予算拒否も `admission_wait` により延期します。

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

`mizu daemon` コマンドは、単一の JSON ではなく JSON Lines（改行区切りの JSON）形式でイベントを出力します。タスク実行が完了するごとに結果オブジェクトを1行出力し、実行が延期された場合は `{"event": "run_deferred", "error": <string>, "time": <ISO-8601>}` を1行出力します。一方、単発実行の `mizu run` コマンドは単一の JSON オブジェクトを出力します。実行枠・ロック待ち（`Busy`）の場合、`mizu run` は `limits.busy_retry_seconds`（既定 300 秒、0 で無効）の範囲で空きを待ってから実行し、期限切れ、または待機キュー満杯で `Busy` を返します（待機上限は `parallel_runs` と同数。drain・pause・unarmed 時は待機しません）。独立プロセス間でも FIFO で受け付け、role/workspace ロックで進めないチケットは順位を保ったままスキップします。即時実行は既存チケットを追い越しません。`daemon` は従来どおり即時 `run_deferred` として次回ポーリングで再試行します。

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
- `health/`: ヘルスチェック記録 ★
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
| 連続失敗（`max_failures` 到達） | 自動的に一時停止（pause）。原因解消後に手動で `mizu resume`（counters を 0 に戻す） |
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
  `description_preview` (1024 UTF-8 bytes maximum) and `description_truncated`,
  never the full text; v1 seeds omit both fields. By-ID reads retain the full
  immutable description, as do history records. Descriptions are untrusted
  content. Rows are at most 8192 canonical bytes and the response at
  most 1 MiB.
- `closed` holds the newest seeded terminal items first (`updated_at`
  descending with a stable ascending ID tie-break), plus `total` seeded closed
  count, `overflow_count` and `truncated`. Each row carries `id`, `title`,
  `status` (`done` or `dropped`), `origin`, `rev`, `updated_at`, `actor`, a
  bounded `reason_preview` with `reason_truncated` when a reason is stored, and
  `snapshot_id` (the verified snapshot for `done`, null for `dropped`). Description
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
  override when set, else the project `[limits]` default). The 10-ID samples
  here are display bounds on a read-only diagnostic, not dispatch batches. Open, closed and event-delivery pages are
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
