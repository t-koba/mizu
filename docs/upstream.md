# 現行エンジンの調査と実装契約

2026-10-03 に Pi 1.0.0、Codex CLI 0.160.0、Claude Code 2.1.288、
Python Agent SDK 0.2.163 の公開仕様と配布物を確認した。実行物のバージョンは
証跡であり、Mizu が旧実行物を判定・変換・拒否するための条件ではない。
内部の設定・bridge・保存記録には世代番号を持たせない。

## Pi

[1.0.0 の変更履歴](https://github.com/earendil-works/pi/blob/v1.0.0/packages/coding-agent/CHANGELOG.md)、
[公開 SDK](https://github.com/earendil-works/pi/blob/v1.0.0/packages/coding-agent/docs/sdk.md)、
[拡張 API](https://github.com/earendil-works/pi/blob/v1.0.0/packages/coding-agent/docs/extensions.md)、
[RPC](https://github.com/earendil-works/pi/blob/v1.0.0/packages/coding-agent/docs/rpc.md)
を参照した。実際の npm 配布物の公開型定義も確認した。

管理下の launcher が `ModelRuntime`、`createAgentSession`、`runRpcMode` を使用する。
`stream` / `streamSimple` / deferred fetch、分類、画像生成の公開要求入口に
予約を置く。`complete` と `completeSimple` は stream へ委譲するので二重予約しない。
仮想モデルのルーティング自体は要求として数えず、補助推論と物理モデルの
呼出しが同じ runtime を通る。計測単位は論理モデル要求であり、provider 内部の
HTTP retry を HTTP 要求ごとに計測したとは扱わない。

codemode、tool search、MCP は専用設定で選択し、実際のツール実行には
`engine_tools` の明示許可を必要とする。表示・非表示・active 状態は権限ではない。
`finish` は model-only。構造化結果を返し、seal と `agent_settled` が揃って完了する。
運用者拡張は信頼された同一プロセスのコードであり、OCI で隔離されたものではない。

## Codex

[app-server](https://learn.chatgpt.com/docs/app-server)、
[設定仕様](https://learn.chatgpt.com/docs/config-file/config-reference)、
[変更履歴](https://learn.chatgpt.com/docs/changelog) と 0.160.0 の
`generate-json-schema` 出力を確認した。必須 bridge は通常の MCP を使い、
実験的 dynamic tools に依存しない。

initialize → initialized → thread/start または thread/resume →
thread を指定した mcpServerStatus/list → turn/start の順に処理する。
MCP 未接続では入力しない。`turn/completed` の状態と seal を照合する。
開始応答より通知が先に到着する順序にも対応する。
要求された入力に無人で答えられなければ明確な失敗として停止する。

累積 token usage は開始時点との差分で扱い、重複・遅延通知を加算しない。
計測単位は turn。内側の全 LLM 要求を要求前予約したという証跡は作らない。
custom provider、reasoning、web search、skills、実験機能は native config へ渡す。
model/provider の固定 allowlist はない。native host shell は無効、Mizu command と
stdio MCP は既存 OCI floor を使い、終了時にrun固有コンテナを回収する。
webSearch / collabAgentToolCall の公開通知もgrantsとsealに照合するが、
通知は要求前フックではなく、既に発生した副作用を巻き戻す保証はない。

実配布物で `approval_policy=untrusted` が現行設定エラーになることを確認し、
標準の要求処理には on-request を使う。Mizu が明示許可した bridge ツールは
native の `approve` に設定する。native `auto` は明示承認と同義ではない。

## Claude

[Python SDK](https://code.claude.com/docs/en/agent-sdk/python)、
[CLI](https://code.claude.com/docs/en/cli-reference)、
[headless](https://code.claude.com/docs/en/headless)、
[変更履歴](https://code.claude.com/docs/en/changelog) と実際の SDK 型を確認した。
コアの依存は標準ライブラリのまま。SDK は専用 interpreter と全依存 hash lock を使う。

公開 `ClaudeSDKClient`、権限 callback、PreToolUse hook、MCP status、interrupt を使う。
PreToolUse は自動承認済みの要求も検査する。`allowed_tools` は実行権限の代用に
しない。`--bare` は subscription 認証に影響するので使わない。
MCP の pending を待ち、connected を確認してから query を送る。
子の tools は親の明示許可集合の部分集合でなければ設定エラー。

`terminal_reason=completed`、success subtype、error なし、seal の全てを要求する。
API 失敗が success subtype を持つ場合も成功にしない。
`model_usage` のモデル別累積値から再開前の値を引く。message や subagent の
内訳をさらに足さない。native 費用推定は別項目であり、分類や token counting
など観測外の補助要求を含む完全な請求額ではない。

## 検証の区分

オフラインの模擬 peer、実配布物の公開 API 検査、実 SDK と模擬 provider の推論、
有料 provider への実推論、実 OCI 隔離は独立した証拠として記録する。
取得・インストール・認証・デプロイは運用者操作であり、work unit では行わない。
MCP が要求する protocolVersion と依存 lock / 実行物 version は外部契約なので残す。
