# 検証記録 — ソース検査と運用証跡

## 開発ソースのオフライン検査（2026-10-03）

以下は2026-10-03時点の開発ソース検査記録です。稼働環境への反映、課金providerの推論、実OCI隔離の受入とは区別します。

| 検査 | 結果 | 範囲 |
|---|---|---|
| Python | **381件成功、失敗0、skip0** | Linux / Python 3.14.4。公開・耐久性・停止・保存・権限・予算・現行driver・選択子/分類の模擬契約 |
| Node | **18件成功、失敗0、skip0** | ローカルbridge通信、有限フレーム、構造化結果、拡張・ModelRuntimeの模擬契約 |
| Python / JavaScript / Bash構文 | **成功** | scripts/check.py |
| Pi 1.0.0 公開SDK | **成功** | 実SDK＋vendor faux provider。通常会話、分類、画像生成、仮想モデル、並列予約、拒否前のdispatch抑止、finish＋agent_settled |
| Pi管理下launcher | **成功** | 実SDK・公開拡張登録・実bridge・模擬モデル。1 model_request、seal、利用量 |
| Codex CLI 0.160.0 app-server | **成功** | 実実行物・実MCP・ローカル模擬Responses provider。1 turnに対し2 HTTP要求、seal・利用量・同一セッション再開 |
| Claude Code 2.1.288 / Python SDK 0.2.163 | **成功** | 実SDK・実MCP・ローカル模擬Anthropic provider。初回1 queryに対し3 HTTP要求（再開queryは2要求）、terminal_reason・seal・利用量・同一セッション再開 |
| オフライン導入・再導入・backup/restore | **成功** | core-only、設定保持、空白を含むパス、manifest、paused/unarmed復元 |
| 配布package | **成功** | release tar/zipの内容・lock・manifest検証 |

これは2026-10-03時点の記録です。後続ソースの状態は各Mizu verification記録にある `code_digest` と結果で確認し、この表の成功を後続スナップショットへ外挿しません。旧CLI経路に対するテストを現行SDK/app-server契約に置き換えたため、従来の件数とは比較しません。single-writer、finish後拒否、検証ダイジェスト、restore unarmed、予算不確定時のunknownなどの不変条件は維持しています。

## 運用者報告（2026-10-03 13:07 UTC–2026-10-15 11:30 UTC）

以下は運用者が報告した値で、受入判定やこのworkspaceでの独立検証を意味しません。レシートは添付されていません。

| 対象 | 運用者報告 | この記録での評価 |
|---|---|---|
| run数 | 6プロジェクトで6,177 run。実provider完了は5,713 run（pi/opencode-go 3,088、pi-durable/opencode-go 1,923、pi/openai 400、pi-durable/openai 302） | 集計の運用者報告のみ。元レシートなし |
| 実行基盤・継続 | 全報告runがrootless Podmanとsystemd user service上。複数日にわたる運転 | 運用者報告のみ。host/runtime証跡なし |
| 更新・再起動 | drain/switch/resumeを伴う約55回のrelease install、サービス復帰を確認したhost reboot 1回 | 運用者報告のみ。運用receiptなし |
| provider失敗 | 402件のInsufficient casesに加えて、429、503、stream切断を含むfailure/failover | その他の個別件数は未提供。分類・failover receiptなし |
| offline testで見逃した不具合 | commit 727dce4eとaed48dfdに関する2件 | 運用者報告のみ。該当run/artifactの添付なし |
| 未確認coverage | 実推論中のstop/resume、既存CIを超える複数OSのlive acceptance | このworkspaceでは未検証 |

Mizuは現在、完全なlive-validation集計を生成しません。運用の報告をrelease受入として扱わず、drain/switch/resumeを実推論中のstop/resumeの証明に読み替えません。

### デプロイ運用者向けの再現可能な証跡チェックリスト

- 対象期間と、期間内に実行した正確なcode SHAを記録する。
- run記録からengine/provider別の集計とfailure class別の集計を作る。
- rootless OCI runtime名・versionとservice managerの状態を記録する。
- upgrade/drain/switch/resumeおよびrebootの結果を記録する。
- サニタイズ済み成果物のdigestを添付する。private path、username、unredacted run ID、認証情報、prompt、providerのraw bodyはレシートに含めない。

機械可読receiptは再生成物で、コミットしません。実環境パス・認証値を公開fixtureへ含めず、再現コマンドには運用者の明示した実行物を渡します。

```sh
python3 scripts/check.py --report private-validation/offline.json
python3 scripts/check-cli.py --codex /path/to/codex --claude-python /path/to/adapter/python3 --report private-validation/contracts.json
node scripts/check-pi-sdk.mjs
python3 scripts/check-model-adapters.py --codex /path/to/codex --claude-python /path/to/adapter/python3 --claude-cli /path/to/claude --report private-validation/mock-provider.json
./scripts/test-install.sh
python3 scripts/package.py --release --output dist
```

Pi依存は各adapterのpackage-lock.jsonで固定します。上の2026-10-03記録にあるPi 1.0.0はその時点の版で、後続ソースの依存版を示しません。Claude SDK依存は専用環境のhash付きrequirements.lockで固定します。doctorは機能を検査し、エンジンの旧バージョンを比較・拒否しません。外部のMCP wire識別子と実行物のバージョン証跡は保持します。

運用者設定の `doctor --sandbox` と `smoke --live --engine ...` は別途明示実行します。smokeは予算を引き上げず、証跡を無視対象のprivate-validationへ保存します。候補や稼働環境を自動昇格しません。契約・信頼範囲・上限・失敗は [completion-contracts.md](completion-contracts.md)、上流API調査は [upstream.md](upstream.md) を参照してください。
