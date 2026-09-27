# 運用・停止・復旧

## 日常の運用操作

`--config PATH` オプションを指定する場合は、サブコマンドの前に配置します（デフォルトは XDG config ディレクトリ配下の設定が読み込まれます）。通常の CLI コマンドは結果を JSON 形式で標準出力（stdout）へ、エラーを標準エラー出力（stderr）へ出力します。出力をパイプ等で処理しても、編集系 API が暗黙に呼び出されることはありません。

```sh
mizu status demo
mizu pause demo
mizu resume demo
mizu wake demo
mizu disarm demo
```

- `pause`: arm 状態を維持したままタスクを一時停止し、実行中の処理に対して協調的キャンセル（cancellation）を通知します。
- `resume`: arm 済みのプロジェクトに限り、一時停止を解除して実行を再開します。
- `disarm`: arm 状態を解除して安全な停止状態にします。
- いずれの状態も、OS やサービスの再起動によって勝手に解除されることはありません。
- `wake`: 完了済み（completed）となったタスクを含め、目標の再検討を明示的に指示する操作です。
- 通常の運用作業において root 権限は一切不要です。

新規プロジェクトを作成した際、`init` の既定では役割が `worker` のみに設定され、unarmed（未承認）の状態で初期化されます。標準の4役編成で動作させるには `--roles worker,searcher,reviewer,reporter` を明示的に指定します。すでにレビュー済みのソースコードを即座に稼働開始する場合にのみ `init --armed` を使用します。組み込みのセーフティガード（連続失敗回数 `max_failures` による自動 pause、単一 Writer の排他ロックによる二重起動防止、unarmed プロジェクトの実行拒否）は、運用者の方針にかかわらず常に有効であり、ポリシー設定によって無効化することはできません。

緊急停止が必要な場合は、`pause` または `disarm` コマンドの実行と、バックグラウンドサービスの停止を併用してください。終了したはずの実験プロセスが残存している場合は `mizu cleanup demo` を実行して、該当プロジェクトのラベルが付与されたコンテナの停止・削除を行います。無関係な他のコンテナを巻き込んで削除することはありません。なお、モデル自身には停止状態を解除する権限はありません。

## 常時稼働の設定 (systemd / launchd / タスクスケジューラ)

常時稼働させる前に、まずは単発の Worker 実行と動作検査が正常に完了することを確認してください。`init` コマンドの既定では `worker` のみが unarmed で作成されるため、下記の例でも最初は worker 単体の設定を示しています。4役編成のサービス定義は、`init --roles worker,searcher,reviewer,reporter` で明示的に作成した場合にのみ生成されます。

`mizu service demo` は実行中の OS に合わせたサービス定義ファイルを生成（描画）するだけであり、自動での起動や有効化は行いません。コマンド実行時に返される `enable_units` が有効化対象の正確な一覧です。すべての役割を一律に有効化するのではなく、運用計画に合わせて必要なユニットを選択して有効化してください。タイマー設定において、interval 型は前回の実行完了からの相対的な間隔を示し、calendar 型は指定されたタイムゾーンでの絶対時刻を示します。なお、ジッター（小さなランダム遅延）やタイマー精度の影響があるため、秒単位での厳密な実行時刻が保証されるわけではありません。また、変更検知時のみ動く `on_change` のみの役割に対してはサービス定義は生成されません（明示的な `mizu run` コマンドでのみ実行されます）。

Linux (systemd user units):

```sh
mizu service demo
# 生成された service や timer ユニットファイルの ExecStart、実行ユーザー、スケジュール時刻を確認
systemctl --user daemon-reload
systemctl --user enable --now mizu-demo-worker.service
# 4役編成の場合のみ追加で有効化:
# systemctl --user enable --now mizu-demo-searcher.timer mizu-demo-reviewer.timer mizu-demo-reporter.timer
systemctl --user list-timers 'mizu-*'
journalctl --user -u mizu-demo-worker.service -f
```

macOS (launchd):

```sh
mizu service demo
# 生成された plist ファイルの ProgramArguments やスケジュール時刻を確認
launchctl bootstrap "gui/$UID" ~/Library/LaunchAgents/mizu-demo-worker.plist
# 4役編成の場合のみ追加で同名の searcher / reviewer / reporter plist を bootstrap
launchctl list | grep mizu-
```

Windows (タスクスケジューラ、管理者権限のコマンドプロンプトで実行):

```cmd
mizu service demo
rem 生成された XML ファイルの Command、Arguments、スケジュール時刻を確認
schtasks /Create /TN mizu-demo-worker /XML "%APPDATA%\mizu\tasks\mizu-demo-worker.xml"
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
systemctl --user stop mizu-demo-worker.service \
  mizu-demo-searcher.timer mizu-demo-searcher.service \
  mizu-demo-reviewer.timer mizu-demo-reviewer.service \
  mizu-demo-reporter.timer mizu-demo-reporter.service
# macOS の場合:
# launchctl bootout "gui/$UID" ~/Library/LaunchAgents/mizu-demo-worker.plist
# Windows の場合 (管理者権限):
# schtasks /Delete /TN mizu-demo-worker /F
mizu cleanup demo
```

## データ保存構造

各プロジェクトのデータディレクトリ（`data_dir/projects/NAME/`）には、以下のファイルおよびディレクトリが配置されます。
- `workspace/`: 管理対象の作業コピー
- `PROJECT.md`, `project.toml`: 運用者が定義した目標とプロジェクト設定
- `control.json`: プロジェクトの制御状態（arm / pause 等）
- `current.json`: 最新の確定状態を指すアトミックなポインタ
- `snapshots/`, `objects/`: 不変なスナップショットマニフェストとコンテンツオブジェクト
- `inbox/`, `decisions/`, `decision-history/`: 提案（Insight）の受付、採否判断、判断履歴
- `runs/`, `sessions/`: タスク実行記録とセッションログ
- `health/`: ヘルスチェック記録
- `observed/`, `active/`, `dashboard/`, `locks/`, `spool/`: 観測状態、実行中ステータス、ダッシュボード投影、排他ロック、キュー
- `artifacts/`: 生成された成果物（Markdown + 根拠データ）

各 run の記録には、開始条件、適用された policy や config の識別情報、実行コマンドや実験の結果、検証の証跡、モデルのトークン使用量、完了ステータスまたはエラー情報が保存されます。なお、run ステータスの `prepared` は公開（publish）が完了したことを意味するものではありません。`current.json` およびそれが参照するマニフェストのみがプロジェクトの「確定状態」として扱われます。`active/` は現在実行中の状態を示す一時的な表示領域であり、直前の確定状態とは厳密に分離されています。

## 障害・異常発生時の挙動

| 発生事象 | システムの挙動と確認事項 |
|---|---|
| モデルIDの不一致・拡張ハンドシェイク失敗 | 有料プロンプトを送信する前に実行を拒否。Pi のバージョン、設定値、拡張機能の導入状態を確認してください。 |
| API呼び出し失敗・日次予算超過 | 上限値を自動で緩和することはありません。すでに受け付けたリクエストの返金処理等も行いません。 |
| タイムアウト・コマンド時間超過・出力サイズ超過 | 関連プロセス群を強制終了し、コンテナを削除します。タスク結果は失敗として記録されます。 |
| Worker プロセスのクラッシュ・中断 | 作業ツリーの未コミット差分はそのまま保持されます。次回実行時に、実際のファイル実態と前回の実行結果を基に安全に再開します。 |
| 同一役割または Writer の二重起動 | 排他ロックにより後続の起動を拒否します。実行中のプロセス PID や重複して起動しているサービスを確認してください。 |
| 連続失敗（しきい値到達） | 設定された連続失敗回数に達すると、プロジェクトを自動的に一時停止（pause）します。原因を解消した後、運用者が手動で resume します。 |
| 成果物の生成失敗 | 直前に生成された有効な成果物をそのまま維持します。Worker は別プロセスで作業を継続可能です。 |
| ディスクの空き容量不足 | 新規タスクの実行を停止します。空き容量確保のために監査証跡などを勝手に自動削除することはありません。 |
| 外部入力データに含まれる悪意ある指示（プロンプトインジェクション等） | 外部データに権限を付与することはありません。入力内容の評価とは無関係に、実行権限境界を厳格に維持します。 |

同一コマンドの無制限な自動再送、作業ツリーの強制的なハードリセット（差分破棄）、応答しないモデルからの別モデルへの暗黙的なフォールバックなどは行われません。外部への副作用を伴うタスクをプロジェクトに含める場合は、あらかじめ冪等性（idempotency）、人間による承認フロー、失敗時の補償トランザクションを設計した上で導入してください。なお、本バージョンには本番環境への自動デプロイやリモートリポジトリへの自動 push 機能は含まれていません。

## 成果物 (artifacts)

成果物をどのように提示・閲覧するか（HTML への変換や Web 配信を含む）は運用者側の表示方針（プレゼンテーション）であり、Mizu のコア機構は汎用的な静的 `artifact`（Markdown 本文 + 根拠 JSON）の保存と公開のみを担当します。Reporter が生成するデータは、`artifacts/<id>/` ディレクトリ内の Markdown 本文（`artifact.md`）、根拠データ（`evidence.json`）、および最新の成果物を指すアトミックなポインタ（`artifacts/latest.json`）です。これにより、LLM が生成した文章記述と、システムが実際に記録した客観的な実行結果・検証証跡とを明確に分離します。なお、LLM を介さずに客観的事実のみをまとめた成果物を生成することも可能です。

```sh
mizu report demo
python3 examples/render-paper.py ~/.local/state/mizu/projects/demo
```

HTML へのレンダリングは運用者側で行う処理であり、ハーネス本体が自動実行することはありません。スクリプト `render-paper.py` は、最新の成果物を静的な HTML ページとして描画するサンプル実装です（モデルが生成したテキストは適切にエスケープ処理されます）。
処理の流れ: `src/mizu/report.py:publish` が `artifact.md`、`evidence.json`、`latest.json` をアトミックに出力し、`render-paper.py` がそれら3つのファイルを読み取って閲覧用の `index.html`（いつでも再生成可能な使い捨ての表示用ファイル）を生成します。Web 配信サーバーの構築、ユーザー認証、TLS 暗号化なども Mizu の管轄外です。成果物にはソースコードや内部の課題情報が含まれ得るため、公開の Web サーバー上に直接置くことは避け、アクセス制限された静的配信や安全なファイル転送手段を利用してください。

成果物に含まれる文章（narrative）はいつでも再構成可能な表示用の投影に過ぎず、正式な根拠データはスナップショットや run 記録として永続化されています。古い成果物の整理・削除は、運用者が明示的にコマンドを実行して行います（デフォルトでは最新30件を保持）:

```sh
mizu prune demo                                # 既定動作: 削除対象のプレビュー表示（dry-run）
mizu prune demo --apply --keep-artifacts 30   # 実際に最新30件を残して古い成果物を削除
```

`--keep-artifacts N` は、`latest.json` が参照している最新の成果物を含めて合計 N 件を保持します（現在参照されている最新の成果物が削除対象になることはありません）。`--apply` による実際の削除履歴は `maintenance/prune-<epoch>-<rand>.json` に監査ログとして記録され、バックアップの対象となります。なお、オプションなしのプレビュー表示（dry-run）ではファイルシステムの変更は一切行われません。

## ダッシュボードと人間による質疑応答 (チャットツール非依存)

Mizu のコア機構は確定した事実データを JSON 形式で公開する役割のみを担い、それをどのように表示するかはプロジェクトごとの方針に委ねられます。常時接続のチャットツール（Slack、Discord、Telegram など）への依存はありません。詳細については [ダッシュボード](dashboard.md) を参照してください。

```sh
mizu dashboard demo
python3 examples/render-dashboard.py ~/.local/state/mizu/projects/demo
# dashboard/index.html は記録された生データを素朴に可視化するサンプルです（配布前の事前確認用）
# チームで共有するダッシュボードは、プロジェクトの要件に応じて dashboard/latest.json を基に構築します
# 機密保護のため、安全な社内ネットワークやプライベートな転送手段で共有し、公開Web上に露出させないでください
```

人間からの回答や指示は、SSH 経由などで `mizu insight submit demo --title ... --body -` コマンドを実行して提案（Insight）として投入し、`mizu wake demo` でプロジェクトの作業を再開させます。Worker から人間への質問事項がある場合は、タスクが `blocked`（ブロック中）状態で終了した際の実行状態（state）内に番号付きリストとして記録されます。なお `dashboard/` 配下の HTML 等はいつでも再生成可能な表示用の投影であるため、バックアップや削除（prune）の対象外となっています。バックアップから復元した後は、`mizu dashboard` コマンドを実行して再生成してください。

## バックアップと復元

プロジェクトを一時停止（pause）し、すべての役割の実行が完全に終了したことを確認してからバックアップを取得します。

```sh
mizu pause demo
mizu backup demo /secure-backups/demo-checkpoint.tar.gz
mizu restore demo-restored --archive /secure-backups/demo-checkpoint.tar.gz
mizu status demo-restored
```

復元先には、既存のプロジェクトと重複しない新しいプロジェクト名を指定する必要があります。復元直後のプロジェクトは、安全のため必ず unarmed かつ paused の状態で初期化されます。復元処理時には、パストラバーサル（path traversal）、危険なシンボリックリンクや特殊ファイル、重複エントリ、既定値（1 GiB）を超えるサイズのアーカイブ展開を安全対策として拒否します。大容量アーカイブを復元する場合は、`--max-bytes` オプションを明示的に指定してください。また、復元された目標ファイル（`PROJECT.md`）や検証コマンドの内容が信頼できるものであるかを人間がレビューした上で、`mizu arm` を実行して承認してください。

バックアップ処理は作業ツリーの未コミットコードをチェックポイントとして保存しますが、このチェックポイントが稼働中プロジェクトの正式な確定状態として公開（publish）されるわけではありません。一方、復元先のプロジェクトでは、このチェックポイントが最初の最新確定状態として設定されます。なお、Git の完全なコミット履歴やリモート設定、運用者全体の共通設定（`config.toml`）、認証情報（`credentials.env`）、共有の日次消費予算などはバックアップの対象に含まれません。Git リポジトリ全体の履歴を保持したい場合は、別途 Git 側のバックアップ手順を実施してください。また、バックアップを復元しても消費済み予算のカウントが巻き戻ることはありません。

```sh
mizu prune demo          # 既定動作: 削除対象のプレビュー表示（dry-run）
mizu prune demo --apply  # 終了済み run のうち、再構成可能な入力データのみを安全に削除
```

監査証跡、提案（Insight）、スナップショット、コンテンツオブジェクト、対話ログなどの重要データが自動的に削除されることはありません。長期的な運用においては、専用ボリュームに対するディスククォータの設定、容量監視、暗号化された定期バックアップ、および運用者が定めるデータ保持ポリシーを組み合わせて管理してください。なお、対話ログや生成コード内には秘密情報が含まれている可能性があるため、バックアップファイルの取り扱いには十分注意してください。

## バージョン更新とロールバック

新しいソースコードツリーの内容をレビューした後、`./scripts/setup.sh --stage-only` を実行します。コマンドの出力に表示された候補版の絶対パスを記録してください（初回セットアップ時を除き、通常の既定動作もこのステージング処理となります）。

更新を適用（昇格）する前に、稼働中の全プロジェクトを一時停止（pause）し、すべてのサービスおよびタイマーを停止します。各役割の実行時刻や周期を変更した場合は、`mizu service` コマンドでサービス定義を再生成し、`daemon-reload` を実行してください。

```sh
./scripts/setup.sh --promote /absolute/path/to/releases/CANDIDATE
mizu service demo  # スケジュール設定を変更した場合は再生成 (OS既定のディレクトリへ出力。--directory で指定も可能)
systemctl --user daemon-reload
mizu doctor --sandbox
mizu smoke --live
# 各種テストの成功を確認した後、必要なサービスを起動してプロジェクトを resume します
```

昇格（promote）処理時には、導入時に記録されたソースコードのマニフェスト、ファイルのハッシュ値、実行権限ビット、および依存パッケージロックファイルのハッシュ値が検証されます。これは同一運用者によるステージング後の意図しない変更を検知するための仕組みであり、暗号署名の検証や悪意ある別ユーザーによる改ざんを防ぐものではありません。また、インストール済み依存ライブラリの全バイナリ検査や脆弱性スキャンの代わりになるものでもありません。

以前のバージョンに戻す場合:

```sh
./scripts/setup.sh --rollback
```

ロールバック処理は実行コードの参照先を以前のバージョンに切り替えるだけであり、実行されたタスクデータや外部への副作用を巻き戻すものではありません。データベースやデータ形式のスキーマ変更を伴うアップデートを行う際は、適切な移行手順の策定と事前のバックアップなしに実行しないでください（なお、バージョン 0.1.0 はスキーマバージョン 1 のみを使用します）。既存のポリシーファイル（`policies/*.md`）は自動更新されないため、新バージョンのテンプレートとの差分を手動で確認して反映してください。

自律ロールである Maintainer にこの昇格権限を与えることはありません。Mizu 本体の保守作業は、独立した隔離コピー環境で実施します:

```sh
mizu init harness-candidate --source /path/to/reviewed/clean/source \
  --goal examples/MAINTENANCE.md --roles maintainer,maintainer-searcher \
  --verify 'python3 -m unittest discover -s tests -q'
```

Git リポジトリからソースツリーを取り込む場合、作業ツリーがクリーンな状態（未コミットの変更がない状態）である必要があります。通常のディレクトリを取り込む場合は、不要な一時ファイルなどを除外した上でコピーされます。Maintainer による改善提案や修正結果は必ず人間がレビューし、公式リポジトリおよび新版ソースツリーに反映した上で、改めて通常のセットアップ手順に従って導入してください。
