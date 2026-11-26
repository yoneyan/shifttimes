# ShiftTimes（シフト管理システム）

シフトの登録・共有・集計と、勤怠の打刻・集計をまとめて行う Django アプリケーションです。
店舗や団体を「グループ」として扱い、グループごとにシフト希望の収集、開講スケジュールの管理、
勤怠の記録、メンバー管理、Stripe によるサブスクリプション課金を行います。

## 主な機能

| 機能 | 概要 |
|---|---|
| 勤務時間 | シフト希望の入力に使う時間帯をグループごとに設定。使わなくなった時間帯は無効化できる |
| シフト希望 | 月カレンダー／一覧表から、時間帯ごとに「勤務可能・勤務不可・要相談」を登録。下書き保存と一括確定に対応 |
| 提出期限 | 対象期間と提出期限をグループ単位で設定。期限後は入力を締め切る |
| 開講スケジュール | 日付ごとに開講区分（営業日・休業日など）を設定。区分ごとに表示色を選べ、「シフト入力不可」の区分も指定できる |
| シフト集計 | メンバー × 日付の提出状況を管理者が一覧で確認 |
| 勤怠管理 | 出退勤ボタンによる打刻と手動入力。月ごとの実働時間を集計。日をまたぐ夜勤にも対応 |
| メンバー管理 | グループへのユーザ登録、管理者権限の付与、メンバー一覧 |
| サブスクリプション | Stripe によるプラン契約。プランごとにメンバー数の上限を適用。運営による無償付与にも対応 |
| Slack 通知 | グループごとに Incoming Webhook を登録し、シフト入力のお願いを送信。提出期限のリマインドやシフト確定の自動通知にも対応 |
| お知らせ | 運営からの通知を掲示。運営は通知管理ページから掲示期間つきで追加・編集できる |
| 認証 | メールによるアカウント有効化、パスワードリセット、TOTP による 2 要素認証 |
| LINE ログイン | LINE アカウントを連携すると、次回からログイン画面の「LINEでログイン」が使える。連携済みのユーザ専用で、LINE から新規ユーザは作られない |

## 技術スタック

- Python 3.12 / Django 5.2
- [uv](https://docs.astral.sh/uv/)（パッケージ管理）
- SQLite（既定。`DATABASES` を差し替えれば他の DB も利用可）
- django-simple-history（モデルの変更履歴）
- Stripe（サブスクリプション課金）
- Bootstrap 5（CDN 配信）
- ruff（Lint）

## セットアップ

### 1. 依存関係のインストール

```bash
uv sync
```

### 2. データベースの初期化

```bash
uv run python manage.py migrate
```

### 3. 管理ユーザの作成

```bash
uv run python manage.py createsuperuser
```

### 4. 開発サーバの起動

```bash
uv run python manage.py runserver 8000
```

<http://localhost:8000/> で起動します。管理画面は <http://localhost:8000/admin/> です。

### ローカル用の設定ファイル

SMTP の資格情報や Slack の Webhook URL など、環境変数に置きたくない設定は
`shifttimes/develop_settings.py` に書きます。このファイルは `.gitignore` の
`*_settings.py` によって除外されるため、リポジトリにはコミットされません。

```python
from .settings import *  # noqa: F403

DEBUG = True
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp.example.com"
# ...
```

利用するときは設定モジュールを明示します。

```bash
uv run python manage.py runserver --settings=shifttimes.develop_settings
```

> **注意**: このファイルには平文の資格情報が入りがちです。ファイル名は必ず `*_settings.py`
> の形を保ち、`git status` に現れていないことを確認してください。

## 定期実行

シフト提出期限のリマインドを Slack へ送るコマンドがあります。cron や systemd timer
から 1 日 1 回呼んでください（呼ばない限りリマインドは送られません）。

```bash
uv run python manage.py send_shift_reminders
```

| オプション | 説明 |
|---|---|
| `--dry-run` | 送信せず、対象になるグループと期限だけを表示する |
| `--group-id <id>` | 特定のグループだけを対象にする |

提出期限 1 件につき 1 回だけ送信し、送信済みかどうかは `ShiftDeadline.reminder_sent_at`
で判定します。何日前に送るかはグループごとに Slack 通知設定で指定します。

cron の例（毎朝 9 時）:

```
0 9 * * * cd /srv/shifttimes && /usr/local/bin/uv run python manage.py send_shift_reminders
```

## テスト

```bash
uv run python manage.py test
```

Lint・整形は ruff で行います。CI では整形漏れもエラーになるため、コミット前に
`ruff format` をかけてください。

```bash
uv run ruff check .
uv run ruff format .
```

Django の system check も CI と同じ条件で確認できます。

```bash
uv run python manage.py check --fail-level WARNING
```

一括整形のコミットは `.git-blame-ignore-revs` に登録しています。ローカルの `git blame`
でも無視するには次を一度実行してください。

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

### CI

`master` / `develop` への push と Pull Request で次のチェックが走ります。

| ワークフロー | ジョブ | 内容 |
|---|---|---|
| `ci.yaml` | Test | `manage.py check --fail-level WARNING`、本番相当の環境変数での `manage.py check --deploy`、`makemigrations --check`（マイグレーションの作り忘れ検出）、`manage.py test` |
| `ci.yaml` | Lint (ruff) | `ruff check` と `ruff format --check` |
| `ci.yaml` | Lint (workflows / Dockerfile) | actionlint（shellcheck 込み）で GitHub Actions を、hadolint で Dockerfile を検査（設定は `.hadolint.yaml`） |
| `ci.yaml` | Docker build check | Docker イメージのビルド確認（push はしない） |
| `dependency-audit.yaml` | pip-audit | `uv.lock` の依存（全グループ）に既知の脆弱性がないか検査。毎週月曜にも定期実行 |
| `codeql-analysis.yml` | Analyze | CodeQL による Python と GitHub Actions の静的解析。毎週定期実行 |

依存ライブラリ・アクション・ベースイメージの更新は Dependabot（`.github/dependabot.yml`）が
毎週 Pull Request を作ります。

## 環境変数

すべて省略可能で、括弧内が既定値です。本番では最低限 `SECRET_KEY`、`DEBUG`、
`ALLOWED_HOSTS`、`CSRF_TRUSTED_ORIGINS`、メール関連、Stripe 関連を設定してください。

### 基本

| 変数 | 既定値 | 説明 |
|---|---|---|
| `SECRET_KEY` | 開発用の固定値 | Django の秘密鍵。**本番では必ず変更する** |
| `DEBUG` | `false` | `true` でデバッグモード（debug_toolbar が有効になる） |
| `ADMIN_MODE` | `false` | 管理者向け表示の切り替え |
| `ONPREMISE_MODE` | `false` | `true` で[オンプレミスモード](docs/onpremise.md)（Enterprise プラン扱い・課金機能を無効化し、トップページをサイト名だけにする） |
| `SITE_NAME` | `シフト管理システム` | ヘッダー・フッター・トップページに出すサイト名 |
| `ALLOWED_HOSTS` | `*` | 空白区切り |
| `CSRF_TRUSTED_ORIGINS` | `http://localhost:8000` | 空白区切り |
| `SITE_URL` | `http://localhost:8000` | Stripe のリダイレクト先や Slack 通知のリンクに使う絶対 URL |
| `DOMAIN_URL` | `SITE_URL` と同じ | メール本文のリンクに使う |
| `ADMIN_DOMAIN_URL` | `test.local` | 運営向け Slack 通知に載せる管理画面の URL |
| `APP_NAME` | `ShiftTimes` | 認証アプリ（TOTP）に表示されるサービス名 |
| `CONTACT_EMAIL` | `contact@example.com` | Enterprise プランの問い合わせ先 |

### HTTPS

HTTP のまま運用する構成を壊さないよう既定はすべて無効です。HTTPS で公開する本番では
有効化してください（CI の `check --deploy` はこれらを有効にした状態で検査しています）。

| 変数 | 既定値 | 説明 |
|---|---|---|
| `SECURE_SSL_REDIRECT` | `false` | `true` で HTTP へのアクセスを HTTPS にリダイレクト |
| `SECURE_PROXY_SSL_HEADER` | `false` | `true` でリバースプロキシの `X-Forwarded-Proto: https` を信頼して HTTPS と判定する。TLS をプロキシで終端し、プロキシがこのヘッダを必ず付け直す構成でのみ有効化する（同梱の nginx 設定ではコメントアウトされている `proxy_set_header X-Forwarded-Proto` を有効にする） |
| `SESSION_COOKIE_SECURE` | `false` | `true` でセッション Cookie を HTTPS のみで送信 |
| `CSRF_COOKIE_SECURE` | `false` | `true` で CSRF Cookie を HTTPS のみで送信 |
| `SECURE_HSTS_SECONDS` | `0` | HSTS の有効期間（秒）。`0` で無効。短い値から段階的に延ばすこと |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | `false` | `true` で HSTS をサブドメインにも適用 |
| `SECURE_HSTS_PRELOAD` | `false` | `true` で HSTS preload を指定 |

### メール

| 変数 | 既定値 | 説明 |
|---|---|---|
| `EMAIL_BACKEND` | DEBUG 時はコンソール、それ以外は SMTP | |
| `EMAIL_HOST` | `localhost` | |
| `EMAIL_PORT` | `25` | |
| `EMAIL_HOST_USER` | 空 | |
| `EMAIL_HOST_PASSWORD` | 空 | |
| `EMAIL_USE_TLS` | `false` | |
| `DEFAULT_FROM_EMAIL` | `no-reply@example.com` | |

### 認証の有効期限

| 変数 | 既定値 | 説明 |
|---|---|---|
| `USER_LOGIN_VERIFY_EMAIL_EXPIRED_HOURS` | `72` | アカウント有効化トークンの有効時間 |
| `USER_LOGIN_VERIFY_EMAIL_EXPIRED_MINUTES` | `30` | メール認証コードの有効分数 |
| `SIGN_UP_EXPIRED_DAYS` | `7` | 招待キーの有効日数 |

### LINE ログイン

| 変数 | 既定値 | 説明 |
|---|---|---|
| `LINE_LOGIN_CHANNEL_ID` | 空 | LINE ログインチャネルのチャネル ID |
| `LINE_LOGIN_CHANNEL_SECRET` | 空 | チャネルシークレット |

両方設定されているときだけ機能が有効になります。LINE Developers には
`<SITE_URL>/line/callback/` をコールバック URL として登録してください。
メールアドレス取得権限の申請は不要です。詳細は
[docs/line-login.md](docs/line-login.md) を参照してください。

### Stripe / 課金

| 変数 | 既定値 | 説明 |
|---|---|---|
| `STRIPE_SECRET_KEY` | 空 | 未設定だと申し込み導線が無効化される |
| `STRIPE_PUBLISHABLE_KEY` | 空 | |
| `STRIPE_WEBHOOK_SECRET` | 空 | 未設定だと Webhook が 400 を返す |
| `STRIPE_PRICE_ID_STANDARD` | 空 | Standard プランの Price ID |
| `STRIPE_PRICE_ID_PRO1` | 空 | Pro 1 プランの Price ID |
| `STRIPE_PRICE_ID_PRO2` | 空 | Pro 2 プランの Price ID |
| `FREE_PLAN_MAX_MEMBERS` | `10` | 無料プランのメンバー数上限 |

詳細は [docs/billing.md](docs/billing.md) を参照してください。
`ONPREMISE_MODE=true` のときは Stripe 関連の環境変数はすべて無視されます。

## デプロイ

`Dockerfile` は nginx + gunicorn の構成でイメージを作ります。
公開イメージは `vX.Y.Z` タグの push 時に GitHub Container Registry
（`ghcr.io/yoneyan/shifttimes`）へ push します（下記「リリース」）。

```bash
docker build -t shifttimes .
docker run -p 8010:8010 --env-file .env shifttimes
```

### リリース

バージョンは `vX.Y.Z` 形式のタグで管理します（`pyproject.toml` の `version` は使いません）。

| 上げる桁 | 対象となる変更 |
|---|---|
| `X`（メジャー） | 既存の運用やデータに手当てが必要な変更（互換性のない設定・挙動の変更など） |
| `Y`（マイナー） | 機能追加、DB マイグレーションを含む変更、依存ライブラリのメジャー更新 |
| `Z`（パッチ） | 不具合修正、依存ライブラリのマイナー・パッチ更新（後述の自動リリースの対象） |

`vX.Y.Z` 形式のタグを push すると `.github/workflows/release.yaml` が動き、
テストが通った場合に限り次を行います（形式が違うタグはエラーになります）。

1. イメージを GitHub Container Registry（`ghcr.io/yoneyan/shifttimes`）へ push
   （`v1.2.3` なら `1.2.3` / `1.2` / `1` / `latest` / `sha-xxxxxxx` タグ。`v0.x` では `0` は付きません）
2. 前回タグからのコミット一覧を本文にした GitHub Release を作成

```bash
git tag v1.2.3
git push origin v1.2.3
```

`v1.3.0-rc.1` のようにハイフンを含むタグはプレリリース扱いになり、`latest` は更新されません。

#### 依存ライブラリ更新の自動パッチリリース

`master` に push されるたびに `.github/workflows/auto-patch-release.yaml` が、
前回の安定版タグ（`vX.Y.Z`）からの変更を調べます。次をすべて満たす場合は、
`Z` を 1 上げたタグ（例: `v1.2.3` → `v1.2.4`）で自動的にリリースします。

- 変更が依存ライブラリの更新（`uv.lock` / `pyproject.toml`）と、アプリに影響しないファイル
  （`.github/` 配下・`docs/` 配下・Markdown など）だけである
- 本番で使う依存（`dev` グループ以外）のバージョンが変わっている（開発用ツールだけの更新ではリリースしない）
- どの依存もメジャーバージョンが上がっていない（`0.x` 系はマイナーの変化も破壊的変更とみなす。
  `certifi` など年をバージョンにするライブラリは除く）
- 前回タグの DB に対して未適用のマイグレーションがない（ライブラリ同梱のマイグレーションも含む）

条件を満たさない場合は何もせず、ワークフローの実行結果（Summary）に理由と依存の変更一覧を出します。
その場合は内容を確認し、上の表に従って手動でタグを作成してください。
タグは CI とイメージの公開が成功してから作成されるため、失敗時に中途半端なタグは残りません。
判定内容は `python3 .github/scripts/auto_patch_release.py` でローカルでも確認できます。

自動パッチリリースの基準になるタグが必要なため、最初の 1 つ（例: `v0.1.0`）は手動で作成してください。

```bash
docker pull ghcr.io/yoneyan/shifttimes:latest
```

コンテナ起動時（`files/entrypoint.sh`）に未適用のマイグレーションがあれば自動で
適用されます。`SKIP_SUPERUSER=false` を渡すと `SUPERUSER_NAME` /
`SUPERUSER_EMAIL` / `SUPERUSER_PASSWORD` で管理ユーザを作成します。

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | アプリ構成、モデル、URL 一覧、設計上の約束ごと |
| [docs/billing.md](docs/billing.md) | Stripe の設定手順、Webhook、無償付与の運用、障害時の挙動 |
| [docs/onpremise.md](docs/onpremise.md) | オンプレミスモード（課金なしの自前運用）の設定と挙動 |
| [docs/line-login.md](docs/line-login.md) | LINE ログインのセットアップ手順と実装のしくみ |
| [docs/user-guide.md](docs/user-guide.md) | グループ管理者・メンバー向けの操作手順 |

## ライセンス

未設定です。
