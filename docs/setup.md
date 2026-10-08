# セットアップ

## 1. 共通の前提要件とプラットフォーム対応

Mizu は Linux / macOS / Windows で動作します。共通の前提要件は以下のとおりです:
- **Python**: 3.11 以上（標準ライブラリのみ使用）
- **Git**
- **Node.js**: 22.23.3 以上および npm
- **OCI コンテナランタイム**: Podman または Docker（コンテナ内部は常に Linux 環境で実行）

| OS | 推奨コンテナランタイム | サービス管理機構 | 設定/タスク配置先 |
|---|---|---|---|
| Linux | rootless Podman (推奨) または Docker | systemd user units | `~/.config/systemd/user` |
| macOS | Podman Desktop または Docker Desktop | launchd | `~/Library/LaunchAgents` |
| Windows | Docker Desktop または Podman Desktop | タスクスケジューラ | `%APPDATA%\mizu\tasks` |

設定ファイル `config.toml` の `sandbox.executable` に `podman` または `docker`（絶対パス可）を指定します。

> **スクリプトの対応状況**: `system-deps.sh` および `install-node.sh` は Linux 専用です。macOS / Windows では公式サイト等から Node.js を導入してください。`build-sandbox.sh` は環境変数 `MIZU_RUNTIME`（デフォルト `podman`）で全 OS に対応します。

## 2. Linux でのセットアップ手順

### (1) 前提条件とファイル配置

Linux では一般ユーザー権限で運用します。すべての操作で root 権限は不要です。

デフォルトのディレクトリ配置:

| 配置場所 | 格納内容 |
|---|---|
| `~/.local/share/mizu/releases/` | 検査済みの候補リリース |
| `~/.local/share/mizu/current` | 現在有効化されているバージョンへのシンボリックリンク |
| `~/.local/bin/mizu` | CLI コマンドの実行可能ファイル |
| `~/.config/mizu/config.toml` | 運用者が設定する権限・上限値・モデルプロファイル |
| `~/.config/mizu/policies/` | 運用者が定義する各役割の判断方針（Markdown） |
| `~/.config/mizu/credentials.env` | API キーなどの秘密情報（パーミッション 0600） |
| `~/.config/mizu/pi/` | Pi 専用の設定および認証情報領域 |
| `~/.local/state/mizu/` | プロジェクト管理データ、監査証跡、共有予算情報 |

元のソースコードを直接作業領域として使用せず、必ず専用の作業コピーを作成してください。また、インストール先、データ保存先、バックアップ先の各ディレクトリを、公開用の Git リポジトリ配下に置かないよう注意してください。

### (2) OS 依存パッケージの導入

スクリプトの内容を確認したうえで、必要な場合のみ管理者権限（sudo）で実行します:

```sh
sudo ./scripts/system-deps.sh --install
python3 --version
git --version
podman info --format json
```

Linux 上で rootless Podman を利用する場合、`/etc/subuid` と `/etc/subgid` の割り当て、cgroup v2 の有効化を済ませてください。

Mizu は rootless Podman 実行時に `--user <host uid> --userns=keep-id` を渡すため、空のユーザー `containers.conf`（`userns = "keep-id"` の追記なし）でもワークスペースが読み取り可能で `mizu doctor --sandbox` が通ります。`--userns=keep-id` は Podman 専用です。Docker では同フラグを渡さず `--user <host uid>` のみで実行します。

### (3) Node.js と npm の準備

Node.js 22.23.3 以上が必要です。

```sh
node --version
npm --version
```

未導入の場合は、公式パッケージまたは付属のスクリプトで導入します:

```sh
./scripts/install-node.sh --version X.Y.Z --sha256 VERIFIED_ARCHIVE_SHA256
```

### (4) ソース検査とセットアップ

```sh
python3 scripts/check.py
./scripts/lock-pi.sh       # ロックファイルを生成・レビュー
./scripts/setup.sh         # 候補版の構築・検査・有効化
export PATH="$HOME/.local/bin:$PATH"
mizu --version
```

初回実行時は自動的に有効化（シンボリックリンクの設定）されますが、サービスの自動起動やプロジェクトの有効化（arm）は行われません。

### (5) モデル設定と秘密情報の管理

`~/.config/mizu/config.toml` を編集し、利用する provider / model ID を設定します:

```toml
[profiles.primary]
provider = "EXACT_PROVIDER_ID"
model = "EXACT_MODEL_ID"
engine = "pi"   # または "codex", "claude"
session = "persistent"
[profiles.primary.options]
thinkingLevel = "off"
```

秘密情報は `~/.config/mizu/credentials.env` に `KEY=value` 形式で記述します（パーミッション 0600）:

```sh
chmod 600 "$HOME/.config/mizu/credentials.env"
```

また、`[limits] daily_requests` に意図した日次リクエスト上限（正の整数）を設定してください。

### (6) 隔離用コンテナイメージの準備

レビュー済みのベースイメージから実験用イメージをビルドします:

```sh
./scripts/build-sandbox.sh --base 'REGISTRY/REVIEWED_IMAGE@sha256:VERIFIED_DIGEST'
```

出力された `sha256:...` を `config.toml` の `[sandbox] image` に設定します。

### (7) 動作検査と最初のタスク実行

```sh
mizu doctor --sandbox    # 実際のコンテナ隔離動作（読み取り専用、非 root、ネットワーク遮断）を検証
mizu smoke --live        # 有料 API への接続と読み取り専用プロンプトの動作を確認

mizu init demo --source examples/demo --goal examples/PROJECT.md \
  --verify 'python3 -m unittest discover -v'
mizu arm demo
mizu run demo --role worker
mizu status demo
```

標準の 4 役編成（worker, searcher, reviewer, reporter）で動作させる場合は、`init` 時に `--roles worker,searcher,reviewer,reporter` を明示します。

## 3. macOS / Windows での注意事項

- **Node.js**: 公式インストーラーまたは nvm 等から Node.js 22.23.3 以上を導入してください（`install-node.sh` は Linux 専用）。
- **コンテナランタイム**: Podman Desktop (`podman machine`) または Docker Desktop を事前に起動しておきます。
- **設定ファイルとタスク**:
  - macOS: `~/.config/mizu/config.toml`、サービスは `~/Library/LaunchAgents/`
  - Windows: `%APPDATA%\mizu\config.toml`、タスクは `%APPDATA%\mizu\tasks\`
- **CLI の実行**: Windows では `bin\mizu.cmd` または PATH に追加して `mizu` として実行します。

### Claude SDK 専用環境

本体の Python 依存は標準ライブラリのみです。運用者が明示的に
`python3 scripts/install-claude-adapter.py --directory NEW_ENVIRONMENT`
を実行して SDK 専用環境を作り（事前に pip を 26.1.2 以上に更新のこと:
GHSA-wf93-45jw-7689 の entry-point traversal 対策、新規環境の pip が古い場合は拒否）、出力された interpreter を
`[engines.claude].command` に設定します。`adapters/claude/requirements.lock`
の全依存 hash を検証します。既存環境を置換せず、work unit では取得しません。
`[engines.claude].directory` は専用の Claude 設定・認証保存先です。
必要な login はその保存先を指定して運用者が行います。

公開機能の確認は `scripts/check-cli.py --claude-python SDK_INTERPRETER`、
実際の Pi SDK と公式模擬 provider の検査は `node scripts/check-pi-sdk.mjs` です。
有料推論・MCP 接続・OCI の証明とは区別します。
