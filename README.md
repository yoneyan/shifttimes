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
| お知らせ | 運営からの通知を掲示。運営は通知管理ページから掲示期間つきで追加・編集できる |
| 認証 | メールによるアカウント有効化、パスワードリセット、TOTP による 2 要素認証 |

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

## テスト

```bash
uv run python manage.py test
```

Lint は ruff で行います。

```bash
uv run ruff check .
```

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
| `SITE_URL` | `http://localhost:8000` | Stripe のリダイレクト先などに使う絶対 URL |
| `DOMAIN_URL` | `SITE_URL` と同じ | メール本文のリンクに使う |
| `ADMIN_DOMAIN_URL` | `test.local` | Slack 通知に載せる管理画面の URL |
| `APP_NAME` | `ShiftTimes` | 認証アプリ（TOTP）に表示されるサービス名 |
| `CONTACT_EMAIL` | `contact@example.com` | Enterprise プランの問い合わせ先 |

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
`master` への push と `v*` タグで GitHub Actions がイメージを Docker Hub
（`yoneyan/shifttimes`）へ push します。

```bash
docker build -t shifttimes .
docker run -p 8010:8010 --env-file .env shifttimes
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
| [docs/user-guide.md](docs/user-guide.md) | グループ管理者・メンバー向けの操作手順 |

## ライセンス

未設定です。
