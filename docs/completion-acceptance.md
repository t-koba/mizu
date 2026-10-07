# 実機受入と比較の記録手順

候補の昇格やサービスの有効化はこの手順から自動実行しません。検査は専用の使い捨て環境で行い、運用者がモデル、認証方式、リクエスト上限を明示してください。秘密そのものを結果ファイルへ含めません。

## 実行表

| 対象 | コマンド／操作 | 記録する証跡 | 今回 |
|---|---|---|---|
| Python 3.11..3.14 × Linux/macOS/Windows | CIの12組で scripts/check.py | OS、Python、実行件数、成功／失敗／skip、CIログURL | ローカルLinux 3.14のみ実施。CIの結果は未取得 |
| 導入・復元 | scripts/test-install.sh | 終了コード、検査種別、paused/unarmed復元 | 実施 |
| 管理下SDK・app-serverの公開機能契約 | python scripts/check-cli.py --report private-validation/cli.json | 公開API・listen機能 | 実施 |
| 課金providerでのMCP・実推論 | 運用者設定で smoke --live --engine codex/claude/pi | run証跡、MCP接続、finish、要求モデル | 未実施 |
| 読取・変更・コマンドの露出拒否 | 各CLIで禁止ホスト操作を要求する固定課題 | 提示ツール集合、拒否、ホスト前後のハッシュ、実行イベント | 未実施 |
| 実OCI | doctor --sandbox | readonly、caps、seccomp、uid、cgroup値、network、cleanup | 未実施 |
| ネットワーク | noneと運用者指定networkで同じdoctorを実行 | namespace、実効設定。外部宛疎通を暗黙に行わない | 未実施 |
| 停止・再開 | stdinを読まない子、孫、RPC途中、キャンセル、再起動 | 時間、残存PID/コンテナ、active、終了記録、同じセッション実体 | ローカル子・孫・RPC送信・キャンセル検査を実施。最新Codex/Claudeの模擬provider付き永続再開も実施 |
| 並行Writer | 同じプロジェクトへ二つのWriterを同時起動 | 一方のBusy、公開順序、最終ポインターと履歴一致 | 同時Writerのローカル統合検査と、異なるOSプロセス・Writerロールでworkspace排他と単一公開を確認。モデルは模擬、排他・公開は実コード。実推論はこの不変条件の検査に不要 |
| 予算のUTC日境界 | 専用予算で深夜を跨ぐ | 日別受付、model_request / turn / query、途中失敗の既知量 | 模擬UTC時計で通常runの受付を検査。実時計の運転は未実施 |
| 24時間連続運転 | 運用者がサービスを有効化して定期採取 | 時刻、idle推論、run/子run、介入、復旧、保存量、最大RSS | 未実施 |
| macOS/Windowsサービス | 一般ユーザーで手動登録・起動・終了 | ローカル時刻／Windows UTC、実行時間制限、全子停止、ACL | 生成物検査のみ。実機未実施 |

結果レコードは `candidate_sha256`、`os`、`python`、`engine_version`、`model`、`request_limit`、`status`（pass/fail/not_run）、`started_at`、`finished_at`、`evidence`、`limitations` を持たせ、`private-validation/` 配下へ保存します（無視対象、コミットしない）。失敗を未実施に置き換えず、模擬RPC／argv／JSON構文の成功を実推論やrootlessの成功へ変換しません。scripts/check.pyはオフライン結果、scripts/check-cli.pyはSDKとapp-serverの公開機能を記録します。scripts/check-pi-sdk.mjsは実Pi SDKとvendor faux provider、scripts/check-model-adapters.pyは管理下Piと最新Codex/Claudeの実MCP・模擬provider・再開を検査します。有料providerと実OCIの受入とは別です。smokeは成功・失敗の証跡を `private-validation/` 配下へ保存し、運用プロジェクトは停止して移します。

## 固定課題による比較

OpenHands/Aider/Mizuの名称だけで包括的優位性を判定しません。全製品に同じ小さなリポジトリ、同じ目標、同じテスト、同じモデル条件を与え、初期状態と受入条件をハッシュで固定してください。

1. 正常系：一つの不具合を修正し、受入テストが成功した成果物を得る。
2. 復旧：推論中・検証後・公開途中で停止し、保存済み状態から再開する。
3. 待機：新しい入力なしで待機させ、推論回数と再開所要時間を測る。
4. 権限：許可しないパス読取・変更・ホストコマンドを要求し、実際の拒否を記録する。
5. 運用：介入回数、保存バイト数、実行時間を測り、request、token、通貨を別指標として示す。

比較側が提供しない能力は対象外とします。モデル・プロバイダー・予算や権限が異なる結果を単純に順位付けしません。最低3回実行し、成功率と全試行の生値を保存します。未実施の比較結果をMizuの優位性として扱いません。

ローカル走査の測定は `python scripts/benchmark.py --files 500 --bytes-per-file 4096 --records 1000 --report private-validation/benchmark.json`。時間とtracemallocピークは現在のファイルシステム上の単回値で、プロセスRSSやモデル性能とは別です。履歴差分は必要な直近manifestまで、Insight/decision/runの表示選択は上限までメモリを保持します。残る全件走査はディレクトリ列挙、Insight/decisionの件数確認、バックアップ・復元です。DBや常駐キャッシュは導入していません。測定証跡は再生成物であり、コミットしない。

## ローカルで完了する検査と外部環境の境界

`tests/test_remaining.py` は、離脱した孫による出力パイプ保持、部分入力失敗、Pi送信期限、ログ保存失敗時の既知利用量、未知量、時差付きdecision、相談キャンセル、並行Writer、模擬UTC日境界、実行中bridgeの回収を検査します。`tests/fault_publication.py` を別プロセスで起動し、オブジェクト・manifest・履歴世代・currentの全write/fsync/link/replace呼出しの前後へEIO/ENOSPC/即時終了を注入します。各回に新しい読み手で公開ポインター、履歴末尾、参照本文を検査します。電源断、実ディスクの枯渇、OSのキャッシュ喪失を模擬成功に含めません。

測定対象はキャプチャ、実体化、履歴、差分、Insight一覧・世代ハッシュ・GC、Editor取込み、利用量run走査、decision一覧、予算GC、配布一覧、pruneのdry-run、バックアップ、復元です。ファイル数・各記録数・時間・Python割当ピークを `private-validation/benchmark.json` に残します（再生成物、コミットしない）。データ作成は計測から除外します。単回測定は性能上限や比較優位性の証明ではありません。

比較課題は `examples/comparison/`。`python scripts/comparison.py prepare /tmp/new-comparison-task` で新規ディレクトリへ同じ初期状態とSHA-256一覧を配置します。初期状態で受入テストが失敗し、契約を満たす修正で成功することはオフライン検査済みです。製品ごとに同じ課題の別コピーを用い、前記5シナリオを最低3回実行します。復旧の停止地点は推論中・検証直後・公開直前／直後を別試行にし、待機は入力なし10分、権限は課題外パスへの読取・変更・コマンドを要求して前後ハッシュを保存してください。

外部実行の記録を `python scripts/comparison.py validate record.json` で検査できます。現行レコードはproduct/version/scenario/trial/fixture_sha256/condition/status/started_at/finished_at/evidence/metrics/limitationsを必須とします。conditionはprovider/model/request_limit/permissions、metricsはprovider_requests/invocations/input_tokens/output_tokens/cost/elapsed_seconds/resume_seconds/storage_bytes/interventions/idle_invocationsです。不明な値はnull、非提供能力はnot_applicable、未実行はnot_runとします。料金は計算して補わず、実測または請求証跡に限ります。上限は64KiBの有限JSON、入力は運用者証跡として信頼し、再試行やエンジン起動はありません。条件・初期状態が違う記録を包括的順位に変換しません。記録検査は外部製品の実行結果を独立に証明するものではありません。
