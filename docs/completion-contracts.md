# 完成に向けた契約修正と移行

この文書は現行の開発ソースの契約です。公開CLIの操作名、単一Writer、明示的な能力付与、運用者による有効化を維持します。実機受入は `VALIDATION.md` と `completion-acceptance.md` に分けます。

## 公開と耐久性

`current.json` は `{snapshot: SHA256, history: SHA256}`。`history` は `histories/<SHA256>.json` の不変なID配列を参照し、その最後のIDが `snapshot` と一致します。history参照のないポインターは拒否します。履歴の正本は不変世代だけです。スナップショット、オブジェクト、履歴世代はポインターより先に完成します。

書込みのデータfsync失敗は伝播します。POSIXのディレクトリfsyncは非対応を示す EINVAL/ENOTSUP/ENOSYS のみを区別し、それ以外を失敗とします。Windowsではディレクトリfsyncを提供していません。ポインターの置換後の同期障害では公開先が変わっている可能性があり、ロールバックや再試行による上書きを自動実行しません。呼出し側は実際のポインターを読み、公開済み・未公開・ポインター読取不能によるunknownと、耐久性未確認を記録します。レポートの失敗もコード公開の事実を取り消しません。

検証は前後のコードダイジェストと表現可能性に結び付きます。Writerはfinish後にも再キャプチャし、ダイジェスト不一致またはskipped項目があれば検証を無効化してdoneを拒否します。ファイル本文と実行属性は同一FDの前後メタデータを検査して取得します。リンク、ハードリンク、特殊ファイルは表現できません。信頼境界はWriterと制御機構の間であり、公開されたmanifestとオブジェクトは変更不可です。

スナップショット読取り時にはmanifest自体、コード・目標のダイジェスト、ファイルの型・長さ、ホストでのパス規則を検査します。ドライブ付きパス、パストラバーサル、ファイル／ディレクトリの衝突、Windowsでの大文字小文字の衝突を拒否します。Windowsのportable読取りはreparse pointと開いたファイルの同一性を検査しますが、POSIXのopenatによる全コンポーネントの固定と同等の競合耐性を主張しません。

## 実行とエンジン

実行の準備はactive記録から終了記録・後片付けまで一つの例外処理範囲です。実行・子相談記録は現行構造で、`run`、子の`parent_run`、`role`、`status`、開始・終了時刻、モデル証跡を持ちます。失敗や中断にも既に取得した利用量と受付回数を残します。Codex/Claudeの受信利用量を先にメモリ上の終了証跡へ登録し、その後にイベントログを保存します。ログ保存失敗でも利用量の取得済み事実を失いません。I/O障害で終了記録自体が保存できない場合は元のエラーを保持して伝播します。active削除の失敗が元の原因を上書きすることはありません。

子プロセスの入力、出力、キャンセルは同じ実行期限で管理します。生のパイプをポーリングし、期限・停止時は入力を中止して読取スレッドを回収します。管理下driverの後片付けはstdinを閉じ、通常終了を1秒待ち、process groupへ1秒の停止猶予を与え、読取スレッドを各0.5秒回収します。OSの起動・シグナル処理そのものに厳密な実時間保証はありません。POSIXの別グループへ逃れた子孫をプロセスグループだけで隔離したとは扱わず、OCI境界の実機確認に分けます。入力・出力エラーは正常終了へ変換せず、部分送信の結果をunknownとして再送しません。管理下driverではstdout証跡4MiB、stderr証跡256KiBを別々に制限します。PiのRPC入力も停止可能な送信を使います。部分送信を再試行しません。POSIXではプロセスグループ、Windowsでは起動時に停止状態からkill-on-close Job Objectへ所属させてから再開する構成です。通常終了でもJobを閉じます。ブリッジの終了は新規受付の停止、Contextの操作停止、接続の遮断、処理中ハンドラーの回収の順です。

Codexはapp-serverのinitialize、thread開始／再開、turn開始／中断、通知を使います。必須MCPの接続・ツール集合を確認してから入力を送り、turn/completedの成功とsealを照合します。providerやreasoning値の共通固定リストはありません。承認・入力要求に無人実行で回答できない場合は停止します。

Claudeは専用環境の公式Python Agent SDKを使います。権限コールバックとPreToolUse hookで明示された追加ツールを確認し、mizu操作は常にbridgeのrole権限で検査します。SDKの公開設定でsubagents・skills・plugins・MCP・構造化出力・effort・上限を構成します。terminal_reason、subtype、is_errorとsealを照合し、実モデルとmodel_usageを記録します。allowedToolsを利用可能ツール集合の制限とみなさず、bareへ置換しません。

永続セッションはモデル、方針、権限、有効設定、リソース・アダプターの内容ダイジェストを識別に含めます。相談とsmokeは非永続です。再開失敗や壊れた記録を新規会話へ置換しません。実行物の認証と信頼された拡張の副作用まで、模擬provider検査で隔離を証明したとは扱いません。

独立したobserver daemonはWriterのdone/wait/blockedを待機条件として流用しません。arm/pause、既存のOSスケジュールとon_changeが適用されます。子相談には親と別の実行枠が必要です。parallel_runs=1は子の消費前にBusyを返し、隠れた再試行をしません。

## Insight・利用量・通信

`insight-ids/<id>.json` は `{id, sha256, created_at}` の不変なID記録です。ハッシュの対象はid/source/title/body/base_snapshot/run。本文がGCされても保持します。同一内容は冪等、異なる内容は拒否します。本文もID記録もなくdecisionだけあるIDは内容を推測せず再利用を拒否します。GCは確定判断のcreated_atを基準にし、deferを保持します。排他順序はeditor-ingest → insights → decisionsです。バックアップはingestを停止して `.ingest` も保存します。Editorの送信はJSON化後UTF-8サイズで受理上限を確認します。

利用量集計の正本は各子runのconsultation.jsonです。親は子run IDだけを参照します。同じrunのresult/errorを重複加算しません。受付直後の保存失敗でも、Contextへ登録した受付数を残します。予算同期の結果を確定できない場合はrequests_known=falseとunknown_request_runsを記録し、既知の0回と区別します。`request_unit` はPiのmodel_request、Codexのturn、Claudeのqueryを区別します。`requests` の合計を料金とは扱いません。未認識または不正な利用量形状も `usage_known=false` とunknown_usage_runsに含め、認識できた部分量は別途残し、未知の数値をトークンへ変換しません。input/output/cache_read/cache_writeを分離し、cacheがinputの部分集合の場合は追加して総トークンを作りません。Claudeはmodel_usageの再開前との差分を使い、messageやsubagent内訳を重複加算しません。レコードの不正JSON、読取エラー、非有限値は個別に隔離します。最新runの選択は5000、表示は200に制限し、切り詰めを明示します。decisionはUTCへ正規化した時刻から最新10を選びます。時差付きISO-8601を文字列順に比較しません。pendingには表示件数・総件数・切り詰めを持たせ、利用量保持は実際の日付で判定します。

両MCPサーバーは共通JSON-RPCループを使用します。要求IDは256 UTF-8バイト以下の文字列または符号付き64ビット整数（bool/null不可）、paramsはオブジェクト、非有限JSONは拒否します。IDのないtools/callは実行しません。要求・応答はLFを含め1MiBです。Python/JavaScriptのブリッジはloopbackと整数ポート、booleanのok、objectのresultを検査します。通信失敗を成功へ変換せず、副作用要求を再送しません。

files/list_filesの省略時はoffset=0/limit=1000。offsetは0..2147483647、limitは1..1000。結果は決定的な順序、offset/next_offset/truncatedを持ち、ページをUTF-8 JSONバイト上限に収めます。変更されるWriterの一覧はページ間の不変性を保証しません。長い読取結果は追加の小さいページを要求します。書込み応答は既存の小さい固定形または64KiB以下のInsight契約を用い、送信側もJSONエスケープ後のフレームを検査します。

## サービス・バックアップ・配布

サービス名は `mizu-<プロジェクト名の文字数>-<プロジェクト名>-<ロール名>`。所有一覧 `.mizu-<project SHA256先頭24文字>.json` がプロジェクト・OS・生成ファイルのハッシュを保持します。所有不明の定義を上書き／削除せず。生成・検証は一時ディレクトリで行い、検証失敗時に既存定義を変更しません。単位ごとの配置は原子的ですが、複数定義の配置全体を一つの原子的トランザクションとは扱いません。失敗時は所有一覧とファイルを点検してください。有効化・起動は運用者の操作です。

macOSのcalendarはOSローカル時刻です。timezone=localを明示してください。WindowsはlocalとUTCに対応し、UTCをStartBoundaryの+00:00で表現します。他のIANA指定を黙って無視せず拒否します。Windowsの繰返しは1分..31日、再起動はPT1M、daemonのExecutionTimeLimitはPT0S。Python実行ファイルとMizuのPython入口を明示し、引数はWindows標準引用規則で構築します。上限は対象OSの生成時だけ検査します。POSIXのchmodとWindowsのACLは同等ではなく、一般ユーザーの専用アカウントで秘密保持を確認してください。

コンテナマウントはホスト側が各OS、内部がPOSIXのパスです。対象の正規化後にシステム領域とworkspace領域の完全一致・子孫・重なりを拒否します。doctorは各モードに同じネットワーク条件、capability/seccomp、資源検査を渡します。一般ユーザーのホスト実行とランタイムrootlessは別の証跡です。cgroup v2の証跡が取得できない環境を資源制限成功として扱いません。

バックアップの通常コピーは排他的作成です。復元は新規ステージで目標、設定、checkpoint、履歴、参照オブジェクトを検査し、失敗したプロジェクトを配置しません。成果物の同一ID再公開で不変本文を上書きせず、再公開日時はlatestポインターへ記録します。

installer/packageは `distribution.py` の明示した製品ディレクトリと公開メタデータを共有します。未知のトップレベルファイル、ローカル設定、認証、検証記録、キャッシュは除きます。コピー後に実体のmanifestを再計算して元と照合し、検査と昇格対象を結び付けます。導入記録にはPython/Node/構文の個別結果を残します。Piの >= は更新を許す宣言であり、再現性はレビュー済みlock＋npm ciが担います。最低バージョンだけでは互換性を証明できません。

Windowsのリンク昇格は置換に失敗すると復旧用リンクを作ってから旧リンクを外し、二度目の置換失敗時に復元します。このfallbackには一時的な欠落期間があり、原子的ではありません。プロセス断で残った.recoveryは次回の操作時、currentが欠落していれば復元してから切替条件を確認します。既存の通常ファイルに復旧を上書きしません。電源断でファイルシステムの耐久性まで損なわれた場合は運用者が確認します。

Codex providerの設定契約は [公式設定リファレンス](https://developers.openai.com/codex/config-reference/) に照合しました。文書上の対応と、記録した実CLIバージョンの受入結果を混同しません。

### ツール応答のサイズ契約

exec/experimentはid/kind/exit_code/reason/seconds/writableと、script/stdout/stderrの各8KiB UTF-8プレビュー、各truncated、commands/<id>.jsonの証跡参照を返します。完全な結果は保存したcommand記録が正本です。verifyも完全な検証記録を保存し、応答の各scriptは512バイトプレビューとtruncatedにします。切り詰めは実行結果や検証結果を成功へ変更しません。相談は最大8件×12000文字の回答を保持し、例外文字列だけ2KiBに制限して完全な子runの証跡を残します。エスケープを含むMCP応答の1MiB境界をテストします。

insights/searchの一覧はoffset/limit（filesと同じ型・上限）を受理し、既存呼出しは省略できます。insightsは設定した最新N件の選択内をページングし、総件数とselection_truncatedも返します。searchは応答行をtitle 300、URL 4096、summary 1500のUTF-8バイトプレビューと各truncatedにし、完全な結果をsources/search-<SHA256>.jsonへ記録します。ページは128KiB以下の項目集合、offset/next_offset/truncatedを持ちます。source_truncatedは検索元の切り詰めと区別します。fetchはoffset（0..2147483647文字）とlimit（1..8192文字、省略8192）で本文をページングし、全文は既存の取得receiptに残します。取得元や検索元が変わるページ間の不変性は保証しません。これらは既存のread/fetch/search/insights権限内であり、新しいホスト読取権限を与えません。通信失敗や部分書込みを再送しません。

## 現行エンジンの接続契約

`engine_channel.Channel` は起動、有限 JSONL の送信、イベント受信、停止、証跡保存を
共通化します。1 run の元イベントは最大 4 MiB、stderr は最大 256 KiB、待機 queue
は 128 件。partial input は再送せず delivery unknown として失敗します。
handshake は 30 秒と run 期限の短い方。run の取消・期限・不正 JSON・上限超過・
terminal 前の EOF は成功になりません。停止時は公開 interrupt を要求し、stdin を
閉じ、process group を回収してから結果を返します。親の bridge は seal 以降の
操作を拒否し、single-writer 公開は既存の verify と immutable snapshot 契約に従います。

セッションは `ephemeral` / `persistent` を明示選択します。保存先は設定・方針・
権限・モデル・ローカル resource・アダプター内容のダイジェストを含みます。
既存データを変換・削除しません。再開不能は失敗であり、新規会話に置換しません。
ファイル・directory resource は最大 64 MiB、tree は最大 4096 エントリー、選択は
最大 64 件。symlink と内容ダイジェスト不一致は起動前に失敗します。

Pi の `_budget` は一意な論理要求 sequence を予約し、`_model_usage` は有限で
最大 64 KiB の要求ごとの利用量・実モデル証跡を受け取ります（最大 4096 件）。
後者は seal 後も取得済み事実の記録として受け付けます。
`_engine_tool` は明示許可と seal / tool 上限を検査します。native 承認・表示・
ツール検索で権限は増えません。拡張や plugin は運用者が信頼したローカルコードで、
adapter と同じ権限範囲です。モデルが選ぶ command / stdio MCP は OCI floor を使います。

Codex の turn と Claude の query の受付は内側の全 HTTP / LLM 要求を計測する
フックではありません。native token 累積値の差分、native 費用推定、未知量を分離し、
HTTP 要求数へ換算しません。観測不能な補助請求や料金総額の保証を記録しません。

追加MCPは最大64サーバーです。stdio MCPはrun固有のOCIコンテナ名を保持し、
アダプター停止後にその名前だけを既存runtimeの削除APIで回収します。回収失敗は
成功へ変換せず停止します。回収は各runtime操作15秒の上限で、OSやruntime自体の
厳密な実時間保証ではありません。
CodexのwebSearch・collabAgentToolCallは公開item/started通知を監視し、roleの
engine_tools、seal、tool上限に違反する観測でturnを停止します。これは既に始まった
ネイティブ操作の通知であり、要求前フックや外部副作用の巻戻しではありません。
子threadのMizu操作は同じbridgeの親role権限を使います。
