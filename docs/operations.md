# 運用・停止・復旧

## 日常の運用操作と状態管理

すべての CLI 操作は一般ユーザー権限で実行します。出力は JSON 形式（標準出力）、エラーは標準エラー出力に出力されます（`--config PATH` を指定する場合はサブコマンドの前に指定）。

### プロジェクト制御コマンド

```sh
mizu status demo   # プロジェクトの制御状態、スナップショット、予算、ヘルスを表示
mizu pause demo    # タスクを一時停止（協調的キャンセルを通知、arm 状態は維持）
mizu drain demo    # 新規実行だけを止め、実行中はそのまま完了させる (mizu status の active が空で静止)
mizu resume demo   # 一時停止・drain を解除してタスクを再開
mizu disarm demo   # arm 状態を解除して安全な停止状態にする
mizu wake demo     # completed や blocked 状態のタスクに対して目標の再検討を指示
mizu cleanup demo  # 終了後に残存したラベル付きコンテナを停止・削除
```

- **状態の永続化**: arm/pause/disarm の状態はディスク上の `control.json` に記録され、OS やプロセスの再起動で勝手に解除されることはありません。
- **実行権限の制約**: 自律モデル自身には停止状態を解除したり、自分自身を arm する権限はありません。
- **二重起動防止**: 単一 Writer ロックにより、同一プロジェクトへの重複書き込みは排他制御されます。
- **drain の意味**: 新規受付だけを止める cordon/quiet 相当であり、実行中の移管 (eviction) や再配置はしません。完了待ちの期限 (timeout) もないため、`mizu status` の active が空になるまで待ってから promote/保守へ進みます。協調的キャンセルが必要なら `pause`、arm 解除が必要なら `disarm` を使います。
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

`mizu daemon` コマンドは、単一の JSON ではなく JSON Lines（改行区切りの JSON）形式でイベントを出力します。タスク実行が完了するごとに結果オブジェクトを1行出力し、実行が延期された場合は `{"event": "run_deferred", "error": <string>, "time": <ISO-8601>}` を1行出力します。一方、単発実行の `mizu run` コマンドは単一の JSON オブジェクトを出力します。

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
- `inbox/`, `decisions/`, `decision-history/`: 提案（Insight）の受付、採否判断、判断履歴 ★
- `runs/`, `sessions/`: タスク実行記録とセッションログ ★
- `health/`: ヘルスチェック記録 ★
- `observed/`, `spool/`, `maintenance/`: 観測状態、Editorキュー、保守監査記録 ★
- `artifacts/`: 生成された成果物（Markdown + 根拠データ） ★
- `active/`, `locks/`: 実行中ステータス、排他ロック（backup対象外：live状態）

各 run の記録には、開始条件、適用された policy や config の識別情報、実行コマンドや実験の結果、検証の証跡、モデルのトークン使用量、完了ステータスまたはエラー情報が保存されます。なお、run ステータスの `prepared` は公開（publish）が完了したことを意味するものではありません。`current.json` およびそれが参照するマニフェストのみがプロジェクトの「確定状態」として扱われます。`active/` は現在実行中の状態を示す一時的な表示領域であり、直前の確定状態とは厳密に分離されています。

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

`mizu storage` は履歴・run・成果物・提案が参照するスナップショットと、未参照マニフェストおよび孤立オブジェクトの件数・バイト数・ bounded sample を報告します。あわせて共有 web-cache（件数・バイト・`cache_seconds` に対する期限切れ）の容量、および durable なセッション証跡（`session.json` 件数・ファイル数・バイト数、削除対象外）の容量を報告します。既定（`--apply` なし）は何も削除・移動・書換えしないため、writer 実行中でも安全に再試行できます。`--apply` は pause したプロジェクトでのみ実行でき、未参照マニフェストと孤立オブジェクトだけを削除します（履歴世代・run・成果物・セッション・判断・提案は保持）。削除内容は `maintenance/storage-*.json` に記録され、バックアップ対象となります。2 回目の実行で新規の孤立オブジェクトまで収束します。

## 人間からの提案 (Insight)

確定した事実は `mizu status`・`mizu usage`・`mizu insight list/read`・`mizu report` と run 記録から読み取ります（外部チャットツール非依存）。表示は各プロジェクト側で作ります。

人間からの回答や指示は、Insight として投入し、`wake` で再開させます:

```sh
mizu insight submit demo --title "方針の指示" --body -  # 標準入力から Markdown を投入
mizu wake demo                                         # 作業再開を指示
```

Worker が `blocked` で終了した場合、人間への質問事項が状態（state）内に番号付きリストとして記録されます。

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
