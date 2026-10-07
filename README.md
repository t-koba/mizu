# Mizu

**ミズ（水） — 小さな機構、明示的な方針、継続する仕事。**

Mizu is a model-configurable autonomous work harness for Linux, macOS and Windows.

Worker が継続的に目標に取り組み、Searcher・Reviewer・Editor がそれぞれ独立して情報を供給し、Reporter が根拠のある成果物を生成します。Maintainer は別の候補コピー上で改善を行いますが、稼働中のバージョンを直接更新することはありません。

本システムは Python 標準ライブラリ、推論エンジン（Pi / Codex / Claude）、Git、OCI コンテナランタイム（Podman / Docker）、OS 標準のサービス管理機能（systemd / launchd / タスクスケジューラ）で構成されており、外部データベースやメッセージブローカー、常時稼働するWebアプリケーションなどは使用しません。

> **提供状態: 0.1.0 / 実機受入前のソース配布。** オフラインの単体・契約テストの通過と、実環境（推論エンジン・コンテナ・API接続）での検証は検証段階（ゲート）が異なります。本配布物のテスト通過のみを根拠として本番稼働や完全な隔離を判断しないでください。実施済みのテスト結果は [検証記録](docs/VALIDATION.md) に、実機での検証手順は [テスト](docs/testing.md) に記載されています。

## 何を固定し、何を交換するか

| 実行機構: プログラムが保証する境界 | 方針: 運用者が明示して変更する内容 |
|---|---|
| 権限（capability）の検査、読取専用マウント、単一Writer | 役割への権限付与、目的、禁止事項 |
| アトミックな状態確定、コンテンツハッシュ、再開時の実態確認 | 次に取り組むべき仕事、提案の採否、完了判断 |
| リクエスト受付数・実行時間・プロセス数・出力サイズの上限制限 | 各種上限値、調査周期、成果物生成のタイミング |
| 推論エンジン（Pi / Codex / Claude）、モデルIDの一致確認 | プロバイダー、モデル、思考レベル、相談先 |
| 隔離環境内でのコマンド実行と結果の記録 | 実験内容、検証コマンド、採用条件 |
| 候補版の検査・昇格（promote） | どの改善をいつ稼働版に適用するか |

判断基準や手順は `policies/*.md` に、権限と上限は非公開の `config.toml` に、プロジェクトの目的と検証コマンドは非公開のプロジェクト管理領域に配置します。モデル自身はその管理領域に書き込むことはできません。なお、固定される依存バージョンは接続インターフェースとしての要件であり、利用するLLMモデルが固定されるわけではありません。

## 役割

```text
人間 ──質問──> Editor ──Insight───────┐
外部情報 ────> Searcher ──Insight─────┼─> Worker ─> コード・実験・確定状態
                           Reviewer ─┘       │                │
                               ^             └────────────────┘
                               └──確定差分             │
                                                     Reporter ─> 成果物
ハーネス情報 ─> 専用Searcher ─> Maintainer ─> 候補コピー ─> 人間の明示的な昇格
```

Worker はタスクの実装を継続するだけでなく、待機（wait）・ブロック（blocked）・完了（completed）を自ら判断できます。待機中のポーリングや監視に LLM を消費することはありません。MoA（Mixture of Agents）は独立した相談結果を Worker が主導して統合する仕組みであり、単純な多数決ではありません。また、Editor が行えるのは提案（Insight）の送信のみです。

## クイックスタート

開発・運用ともに Linux / macOS / Windows で実行可能です（要件: Python 3.11以上、Git、Node 22.19.0以上とnpm、OCI コンテナランタイム）。コンテナ内部は常にLinux環境であり、隔離検証の仕組みは全OSで共通です。**すべての操作は一般ユーザー権限で実行します。** OS固有の注意点、Nodeの導入、秘密情報の管理、隔離環境の検査を含む完全な手順については [セットアップ手順](docs/setup.md) を参照してください。以下のコマンド例は POSIX シェル（bash）向けです。Windows では同等の操作を各自のシェルで行ってください。

```sh
# ソースコードのディレクトリで実行（オフラインテストのため外部ネットワークは不要）
python3 scripts/check.py

# Piの正確な直接依存バージョンからロックファイルを生成し、差分を確認
./scripts/lock-pi.sh

# 初回セットアップ: Piを含む候補版を構築・検査し、有効化（この時点では稼働は開始しません）
./scripts/setup.sh
export PATH="$HOME/.local/bin:$PATH"
```

`~/.config/mizu/config.toml`（Windows では `%APPDATA%\mizu\config.toml`）で `profiles.primary` および `profiles.alternate` に正確な provider / model ID、意図した日次リクエスト上限（`daily_requests`）、隔離イメージのダイジェストを設定します。APIキーなどの秘密情報は別ファイル `credentials.env` に保存します。初期状態ではモデル未選択かつ予算がゼロに設定されているため、意図しない自動起動は発生しません。

```sh
# 運用者が確認済みのDebian系Pythonイメージを基に、ローカルの実験用イメージをビルド
./scripts/build-sandbox.sh --base 'YOUR_REVIEWED_IMAGE@sha256:YOUR_DIGEST'
# 出力された sha256:... を config.toml の sandbox.image に設定

mizu doctor --sandbox
mizu smoke --live               # 有料APIへの明示的な接続確認テスト（実プロジェクトは変更されません）

mizu init demo --source examples/demo --goal examples/PROJECT.md \
  --verify 'python3 -m unittest discover -v'
mizu arm demo
mizu run demo --role worker     # 最初は単発実行で動作と結果を確認
mizu status demo
```

動作を十分に確認した後、`mizu service demo` で生成されたサービス定義（OS ごとのサービス管理機能用）をレビューして有効化します。SSH切断後も実行を継続させる設定（Linux では linger の有効化が必要）、成果物のタイムゾーン、停止手順などについては [運用ドキュメント](docs/operations.md) を参照してください。

APIやPodmanを使わずにソースインストーラーの動作を検査したい場合は `./scripts/test-install.sh`（POSIX シェル用）を使用します。なお `setup.sh --core-only` はオフライン検証専用であり、Pi導入済み環境の代替にはなりません。

## ディレクトリ構成

```text
bin/                 JSON形式で標準出力するCLIツール
src/mizu/           標準ライブラリのみで書かれた制御コア（状態管理・権限・隔離・MCP・成果物）
adapters/            推論エンジン（Pi, Codex, Claude）のアダプターと互換性定義
config/              コメント付きのTOML設定テンプレート
policies/            役割（ロール）ごとの判断方針（Markdown）
scripts/             依存関係のセットアップ、Piの導入、検査、更新、パッケージング用スクリプト
containers/          実験用コンテナイメージの最小構成定義
examples/            実行可能なデモ、目標設定例、保守目標例、Editor用MCP設定
tests/              単体テスト・障害注入テスト・契約テスト（外部ネットワーク不要）
docs/                設計、導入、運用、境界、拡張、検証、公開手順などのドキュメント
.github/             最小権限に絞ったCIワークフロー、依存関係更新、Issue/PRテンプレート
```

## 設計上の重要な制限事項

リクエスト予算（`daily_requests`）は、通貨や消費トークン数に対する厳密な制限ではありません。予期せぬ課金を防ぐため、各LLMプロバイダー側でも必ず利用料金上限（Spending Limit）を設定してください。コマンド実行時には CPU、メモリ、PID 数、実行時間、出力サイズなどの制限が適用されますが、作業ボリューム全体のディスク容量制限にはファイルシステム側のクォータ（quota）設定が必要です。また、rootless コンテナによる隔離は仮想マシン（VM）と同等の完全な分離を提供するものではありません。

Editor用のMCPサーバーは読み取り専用のAPIを提供しますが、**MCPを追加しただけでEditor全体が自動的にサンドボックス化されるわけではありません**。`editor-capsule.sh` による読み取り専用エクスポートと専用の outbox ディレクトリを使用してください。Editor側の推論用ネットワーク接続は許可されますが、実験環境側からのネットワーク接続は禁止されます。

バックアップ機能は、未コミットの変更を含むコードのチェックポイントと監査証跡を保存するものです。元のGit履歴、秘密情報ファイル、システム全体の設定、プロジェクト間で共有される予算情報はバックアップに含まれません。なお、モデルの対話履歴や生成コード内に秘密情報が紛れ込んでいる可能性があるため、作成されたバックアップファイルを不用意に公開しないでください。

推移的依存関係を含む完全な npm ロックファイルは `adapters/pi/package-lock.json` に含まれており、直接依存する Pi の現行 SDK は `1.0.0` に固定しています。公開前にロックファイルの内容をレビューし、再現可能なリリースを作成する際は `scripts/package.py --release` を使用してください。インストーラーはロックファイルのハッシュ値を導入記録に保存して検証します。

## ドキュメント

[設計](docs/architecture.md) · [セットアップ](docs/setup.md) · [設定](docs/configuration.md) · [運用](docs/operations.md) · [Editor](docs/editor.md) · [セキュリティ](docs/security.md) · [拡張機能](docs/extensions.md) · [検証](docs/testing.md) · [公開](docs/releasing.md) · [設計判断 (ADR)](docs/adr/README.md)

本プロジェクトが依存する外部プロダクトの名称・ライセンス・商標は、それぞれの権利者に帰属します。本リポジトリには、製品名の法的な使用可否や商標登録に関する調査結果は含まれていません。
