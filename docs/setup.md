# セットアップ

## 0. 3大OS共通の運用基盤

Mizu の運用（自律作業の実行やバックグラウンド常駐）は Linux / macOS / Windows のいずれでも動作します。共通の前提要件は Python 3.11以上、Git、Node 22.19.0以上と npm、および OCI コンテナランタイムです。コンテナ内部は常に Linux 環境であり、読み取り専用マウント、非 root 実行、ネットワーク切断、秘密情報の非継承、終了時のコンテナ削除といった検証項目は全 OS で共通です。OS ごとの差異は `src/mizu/platform.py`（プラットフォーム機能の判定）と `src/mizu/services.py`（サービス定義のレンダラー）に集約されており、各機能の実装には分散していません。

| OS | コンテナランタイム | サービス管理機構 |
|---|---|---|
| Linux | rootless Podman (推奨) または Docker | systemd user units (`~/.config/systemd/user`) |
| macOS | Podman Desktop (`podman machine`) または Docker Desktop | launchd (`~/Library/LaunchAgents`) |
| Windows | Docker Desktop または Podman Desktop | タスクスケジューラ (`%APPDATA%/mizu/tasks` のXMLを `schtasks /Create` で登録) |

設定ファイル `config.toml` の `sandbox.executable` には `podman` または `docker`（絶対パスも可）を指定します。`mizu doctor` コマンドは、単なるバージョン文字列の確認にとどまらず、ランタイムとの疎通、イメージの存在確認、さらには実際のコンテナを用いた振る舞い検証（読み取り専用、非 root、秘密情報の非継承、ネットワーク遮断）を実施します。`mizu service` コマンドは実行中の OS に合わせたサービス定義ファイルを生成（描画）するだけであり、サービスの自動起動や有効化までは行いません。

ホスト環境向けセットアップスクリプトの対応状況:
`system-deps.sh` および `install-node.sh` は Linux 専用です。macOS や Windows では nodejs.org から手動で Node をインストールし、チェックサムや署名を確認してください（詳細は [上流資料](upstream.md) を参照）。`build-sandbox.sh` と `editor-capsule.sh` は環境変数 `MIZU_RUNTIME`（デフォルトは `podman`）を通じてすべての OS で動作します。なお、`idmap/mizu-userns`（単一UID/GID環境向けのレガシー機構）は Linux 専用です。

# Linuxセットアップ

## 1. 前提条件とファイル配置

本手順は、一般ユーザー権限で運用する Linux 環境を対象としています。必要なソフトウェアは Python 3.11以上、Git、Bash、Node 22.19.0以上と npm、rootless Podman（または Docker）、および systemd ユーザーマネージャーです。本配布物はすべての Linux ディストリビューションで動作検証されているわけではありません。また、ディストリビューションにパッケージが存在していても、要求バージョンを満たしているとは限らない点にご注意ください。

デフォルトのディレクトリ配置:

| 配置場所 | 格納内容 |
|---|---|
| `~/.local/share/mizu/releases/` | 検査済みの候補リリース |
| `~/.local/share/mizu/current` | 現在有効化されているバージョンへのシンボリックリンク |
| `~/.local/share/mizu/previous` | 直前に有効化されていたバージョンへのシンボリックリンク |
| `~/.local/bin/mizu` | CLI コマンドの実行可能ファイル |
| `~/.config/mizu/config.toml` | 運用者が設定する権限・上限値・モデルプロファイル |
| `~/.config/mizu/policies/` | 運用者が定義する各役割の判断方針（Markdown） |
| `~/.config/mizu/credentials.env` | API キーなどの秘密情報（パーミッション 0600） |
| `~/.config/mizu/pi/` | Pi 専用の設定および認証情報領域 |
| `~/.local/state/mizu/` | プロジェクト管理データ、監査証跡、共有予算情報 |

元のソースコードを直接作業領域として使用せず、必ず専用の作業コピーを作成してください。また、インストール先、データ保存先、バックアップ先の各ディレクトリを、公開用の Git リポジトリ配下に置かないよう注意してください。

## 2. OS 依存パッケージの導入

スクリプトの内容を確認したうえで、必要な場合のみ以下のコマンドを管理者権限（sudo）で実行します。

```sh
sudo ./scripts/system-deps.sh --install
python3 --version
git --version
podman info --format json
```

このスクリプトは apt または dnf を使用して基本パッケージを導入します。なお、Mizu や Pi の実行、設定ファイルの作成、コンテナの実行などは root 権限ではなく、すべて一般ユーザー権限で行います。

Linux 上で rootless Podman を利用する場合、一般ユーザー用の `/etc/subuid` および `/etc/subgid` の割り当て、rootless 用ストレージ設定、cgroup v2 の有効化をディストリビューションの手順に従って構成してください。環境が要件を満たしているかどうかの最終確認は、単にバージョン番号を目視するのではなく、`mizu doctor --sandbox` による実際の動作検証（読み取り専用、非 root、秘密情報の非継承、ネットワーク遮断）によって行います。Mizu は環境の不備をホスト上での直接実行にフォールバックして回避することはありません。

## 3. Node.js と npm の準備

すでに要件を満たすバージョンの Node.js が導入されている場合は、追加作業は不要です。

```sh
node --version
npm --version
```

未導入の場合は、ディストリビューションの公式パッケージなど信頼できる方法でインストールしてください。付属のバージョン指定ブートストラップスクリプトを利用することも可能です。

```sh
./scripts/install-node.sh --version X.Y.Z --sha256 VERIFIED_ARCHIVE_SHA256
```

ここで `X.Y.Z` には 22.19.0 以上の確認済みバージョンを、SHA256 には Node.js 公式サイトで配布されている**対象 OS および CPU アーキテクチャ用アーカイブ**の検証済みハッシュ値を指定します（値を推測で設定しないでください）。公式の署名およびチェックサムの検証手順については [上流資料](upstream.md) を参照してください。スクリプトはダウンロード完了後にチェックサムを照合し、既存の Node.js 環境を上書きすることなく専用ディレクトリに展開して、追加すべき PATH を表示します。システムサービスとして登録する前にこの PATH を適用してセットアップを実行することで、Pi の起動コマンドに Node の絶対パスが記録されます。

## 4. ソース検査と Pi を含むセットアップ

```sh
python3 scripts/check.py
./scripts/lock-pi.sh
# adapters/pi/package-lock.json の内容と依存パッケージのライセンスをレビュー
./scripts/setup.sh
export PATH="$HOME/.local/bin:$PATH"
mizu --version
```

標準の `setup.sh` は Mizu の候補版を専用ディレクトリにコピーし、Pi `0.87.1` 以上および対応する `pi-ai` を最低バージョン指定で導入します。ロックファイルが存在する場合は `npm ci` を実行し、存在しない場合はロックファイルを生成してから `npm ci` を実行します。依存パッケージのライフサイクルスクリプト（lifecycle scripts）はセキュリティのため既定で無効化されています。必要性を確認した場合にのみ `--allow-install-scripts` オプションを指定してください。

依存パッケージの導入後、オフラインテストの実行、Pi のバージョン検証、および導入記録の生成が行われます。初回実行時は自動的に有効化（シンボリックリンクの設定）されますが、サービスの自動起動やプロジェクトの有効化（arm）、API リクエストの送信は行われません。2回目以降の実行では、既定で候補版の配置（ステージング）のみが行われます。既存の `config.toml`、`policies/`、`credentials.env` が上書きされることはありません。

Node.js の未導入、ネットワーク障害、npm の失敗、テストの失敗などが発生した場合、その候補版が有効化されることはありません。未検査のバージョンへ暗黙のうちにフォールバックすることもありません。

## 5. モデル設定と秘密情報の管理

`~/.config/mizu/config.toml` を編集します。`primary` と `alternate` は便宜的なプロファイル名であり、同じモデルを指定しても異なるモデルを指定しても問題ありません。選択したモデルがツール呼び出し（Tool Calling）を正しく処理できることを、あらかじめ実機テストで確認してください。

```toml
[profiles.primary]
provider = "EXACT_PROVIDER_ID"
model = "EXACT_MODEL_ID"
thinking = "off"

[profiles.alternate]
provider = "EXACT_PROVIDER_ID"
model = "EXACT_MODEL_ID"
thinking = "off"
```

上記の設定値は例示用のプレースホルダーです。Pi がサポートしている正確な provider ID および model ID を指定してください。Mizu がモデル名を自動推測することはありません。独自のエンドポイントを利用する場合は、専用の Pi 設定領域に Pi 公式フォーマットのモデル定義ファイルを配置してください。

`credentials.env` は `KEY=value` の形式で1行ずつ記述するシンプルな設定ファイルです。シェルスクリプトとして `source` されることはなく、環境変数の展開やコマンド置換も行われません。環境変数名（KEY）には、選択した Pi プロバイダーが要求する名前を使用してください。Pi の認証ファイル方式を利用する場合も、専用の Pi 設定領域内に配置可能です。OAuth を含む各種認証方式については、上流の Pi および各プロバイダーの仕様・利用規約に従ってください。

```sh
chmod 600 "$HOME/.config/mizu/credentials.env"
```

Node.js の実行時オプションや PATH などのシステム環境変数を `credentials.env` に混在させないでください。ここに記述された秘密情報が LLM 向けツールや実験用コンテナに渡されることはありません。ただし、Pi は推論を実行するために、対象リポジトリのコードを設定先のプロバイダーへ送信します。データの送信先や利用規約（学習利用の有無など）は事前に必ず確認してください。

また、`[limits] daily_requests` に意図した正の整数を設定してください。これはすべてのプロジェクトおよび役割で共有される、UTC 基準での1日あたりのリクエスト受付上限数です。金銭的な利用料金の上限そのものではないため、各プロバイダー側で提供されている課金上限や使用量アラート機能も必ず併用してください。

## 6. 隔離用コンテナイメージの準備

運用者が内容を確認済みの Debian 系 Python イメージのダイジェスト（sha256）を指定してビルドします。

```sh
./scripts/build-sandbox.sh --base 'REGISTRY/REVIEWED_IMAGE@sha256:VERIFIED_DIGEST'
```

ビルド完了時に標準出力へ表示されるローカルの `sha256:...` を、`config.toml` の `[sandbox] image` に設定します。外部ネットワークを使用するのは準備段階の pull および build 処理のみであり、実際のタスク実行時には常に `--pull=never` かつネットワーク遮断状態でコンテナが起動されます。イメージ内には `/bin/sh`、Python 3.11以上、およびプロジェクトに必要なテスト用依存関係が含まれている必要があります。付属の Containerfile は Git、make、コンパイラなどを追加します。Node.js が必要なプロジェクトの場合は、内容を確認した Node.js を追加したコンテナイメージを別途用意してください。

単一 UID/GID 環境（`mode = "single"`、Linux 専用のレガシー構成）では以下のような違いがあります。イメージの pull や build はラッパー内で適切なグローバルフラグを付与して手動実行します。`FROM` 句にはダイジェスト解決後に pull したタグ参照を使用し（`--build-arg BASE_IMAGE=<tag>`）、さらに `--build-arg APT_SANDBOX_USER=root` を指定します（これは apt サンドボックスによる `_apt` ユーザーへの降格を無効化するためのものであり、タスク実行時のコンテナ隔離性には影響しません）。ビルド完了後は `podman image inspect` で取得したイメージ ID に `sha256:` プレフィックスを付けて設定ファイルに指定します。ビルド失敗によってイメージインデックスが破損した場合は、`podman system reset` を実行した後に再度 pull することで復旧できます。

可変なタグ名ではなく、ビルドによって生成された一意なイメージ ID を設定に記録してください。なお、apt などの外部パッケージリポジトリを厳密に固定しない限り、将来同じ定義から再ビルドしても完全に同一のバイナリが生成される保証はない点に留意してください。

## 7. 動作検査と最初のタスク実行

```sh
mizu doctor --sandbox
mizu smoke --live
mizu init demo --source examples/demo --goal examples/PROJECT.md \
  --verify 'python3 -m unittest discover -v'
mizu arm demo
mizu run demo --role worker
mizu status demo
```

`init` コマンドで設定されるデフォルトの役割は `worker` のみです。調査（searcher）、査読（reviewer）、報告（reporter）も含めた標準の4役編成で稼働させる場合は、運用者が明示的にオプションを指定します。

```sh
mizu init demo --source examples/demo --goal examples/PROJECT.md \
  --roles worker,searcher,reviewer,reporter \
  --verify 'python3 -m unittest discover -v'
```

ソースコードの内容をその場で確認済みの場合は、`init --armed` を指定して即座に実行可能状態にすることもできます。デフォルトが unarmed（未承認・実行停止）であり、`arm` が独立した操作として分かれているのは、未レビューのコードが誤って自動実行されるのを防ぐための安全設計（フェイルセーフな既定値）です。なお、バックアップからの復元時は常に unarmed かつ paused 状態となり、`--armed` オプションは用意されていません。

`doctor --sandbox` は実際に rootless コンテナを起動し、読み取り専用マウント、一般ユーザー実行、ネットワーク遮断、秘密情報の非継承、および終了時のクリーンアップ処理が正しく機能するかを検証します。`smoke --live` は、実際に有料の API を使用して行う読み取り専用の Pi 動作確認テストです。テスト用の一時プロジェクト内に配置されたランダムなファイルをモデルに読み取らせ、正確な応答と正常終了（finish）を確認します。このテストの最大消費リクエスト数は 2 回に制限されています。モデルが不要な追加ターンを消費した場合はテストが失敗することがありますが、リクエスト上限が自動的に緩和されることはありません。

運用開始時は、まず単発のコマンド実行でログ出力と作業内容を確認し、次にデーモン実行、最後に定期実行サービスを有効化していく段階的な手順を推奨します。常時稼働の開始手順については [運用ドキュメント](operations.md) を、受入検証の詳細については [テストドキュメント](testing.md) を参照してください。
