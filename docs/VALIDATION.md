# 検証記録 — 現行ソース

2026-10-03の開発ソース検査です。稼働環境への反映、課金providerの推論、実OCI隔離の受入とは区別します。

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
| 課金provider / 実OCI / rootless / OSサービス運用 | **未実施** | 模擬provider・OCI argv検査から成功を推測しない |
| 24時間運転 / 実推論中の停止・再開 / 複数OS CI | **未取得** | ローカルLinuxの成功とは別の受入 |

旧CLI経路に対するテストを現行SDK/app-server契約に置き換えたため、従来の件数とは比較しません。single-writer、finish後拒否、検証ダイジェスト、restore unarmed、予算不確定時のunknownなどの不変条件は維持しています。

機械可読receiptは再生成物で、コミットしません。実環境パス・認証値を公開fixtureへ含めず、再現コマンドには運用者の明示した実行物を渡します。

```sh
python3 scripts/check.py --report private-validation/offline.json
python3 scripts/check-cli.py --codex /path/to/codex --claude-python /path/to/adapter/python3 --report private-validation/contracts.json
node scripts/check-pi-sdk.mjs
python3 scripts/check-model-adapters.py --codex /path/to/codex --claude-python /path/to/adapter/python3 --claude-cli /path/to/claude --report private-validation/mock-provider.json
./scripts/test-install.sh
python3 scripts/package.py --release --output dist
```

Pi依存は1.0.0のレビュー済みlockとnpm ciで固定し、Claude SDK依存は専用環境のhash付きrequirements.lockで固定します。doctorは機能を検査し、エンジンの旧バージョンを比較・拒否しません。外部のMCP wire識別子と実行物のバージョン証跡は保持します。

運用者設定の `doctor --sandbox` と `smoke --live --engine ...` は別途明示実行します。smokeは予算を引き上げず、証跡を無視対象のprivate-validationへ保存します。候補や稼働環境を自動昇格しません。契約・信頼範囲・上限・失敗は [completion-contracts.md](completion-contracts.md)、上流API調査は [upstream.md](upstream.md) を参照してください。
