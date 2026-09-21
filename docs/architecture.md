# 設計ドキュメント

開発者向けに、アプリの構成・モデル・URL・設計上の約束ごとをまとめます。

## アプリ構成

| アプリ | 役割 |
|---|---|
| `shifttimes` | プロジェクト設定、共通テンプレート、トップページ、ログイン、Slack 通知 |
| `custom_auth` | ユーザ・グループ・権限・2 要素認証・LINE ログイン・サブスクリプション課金 |
| `shift` | シフト希望、開講スケジュール、提出期限、勤怠管理 |
| `notice` | 運営からのお知らせ |

Django の標準 `User` は使わず、`custom_auth.User` を `AUTH_USER_MODEL` に
指定しています。`custom_auth.Group` も `django.contrib.auth.models.Group` とは
別物で、店舗や団体を表す独自モデルです（`models.py` 冒頭で標準 `Group` を
import しているため `# noqa: F811` が付いています）。

## データモデル

### custom_auth

```
User ──< UserGroup >── Group
                         │
                         ├─ stripe_customer_id / stripe_subscription_id
                         ├─ stripe_plan / stripe_status / stripe_current_period_end
                         ├─ stripe_cancel_at_period_end
                         └─ free_plan / free_reason
```

| モデル | 説明 |
|---|---|
| `User` | ユーザ。ユーザ名は英語名（`username`）と日本語名（`username_jp`）の 2 つを持つ |
| `Group` | 店舗・団体。契約プランとメンバー数上限はここで決まる |
| `UserGroup` | 中間テーブル。`is_admin` でグループ管理者かどうかを持つ。`(user, group)` は一意 |
| `UserActivateToken` | アカウント有効化用のトークン |
| `UserEmailVerify` | ログイン時のメール認証コード |
| `TOTPDevice` | TOTP の秘密鍵。1 ユーザにつき最大 5 件 |
| `LineAccount` | ユーザと LINE アカウントの連携（`OneToOne`）。`line_user_id` は一意 |

`Group` のプラン関連のプロパティは次のとおりです。

| プロパティ | 内容 |
|---|---|
| `is_free_granted` | 運営による無償付与が有効か（`membership_expired_at` で期限判定） |
| `has_active_subscription` | Stripe の契約が利用可能な状態か |
| `plan_key` | 実際に適用されるプランのキー。候補のうち `rank` が最大のものを選ぶ |
| `plan` | 適用されるプラン定義（`settings` の辞書） |
| `max_members` | メンバー数上限。`None` は無制限 |
| `member_count` | 現在のメンバー数 |
| `can_add_member(count=1)` | あと `count` 人追加できるか |

詳細は [billing.md](billing.md) を参照してください。

### shift

```
Group ──< OpeningScheduleType ──< DateOpeningSchedule
      ──< ShiftDeadline
      ──< TimeSlot
      ──< ShiftEntry >── TimeSlot
      ──< AttendanceRecord
      ──1 AttendanceSetting
      ──1 SlackNotificationSetting
```

| モデル | 説明 |
|---|---|
| `TimeSlot` | グループごとの勤務時間（時間帯）マスタ。開始・終了時刻を持ち、`(group, name)` が一意 |
| `OpeningScheduleType` | グループごとの開講区分。`color`（`#rrggbb`）でカレンダーの表示色を指定。`blocks_shift_input` が真の日はシフト入力を禁止 |
| `DateOpeningSchedule` | 日付ごとの開講区分の割り当て |
| `ShiftEntry` | シフト希望。`(group, user, work_date, time_slot)` が一意。`is_draft` で下書き管理 |
| `ShiftDeadline` | 対象期間（`period_start`〜`period_end`）と提出期限 |
| `AttendanceSetting` | グループごとの勤怠設定（`OneToOne`）。機能の有効・無効と入力方法の許可 |
| `AttendanceRecord` | 1 日ぶんの勤怠。`(group, user, work_date)` が一意 |
| `SlackNotificationSetting` | グループごとの Slack 通知設定（`OneToOne`）。Webhook URL と通知ごとの有効・無効 |

`ShiftEntry.status` は `available`（勤務可能）/ `unavailable`（勤務不可）/
`maybe`（要相談）の 3 値です。
`AttendanceRecord.source` は `manual`（手動入力）/ `clock`（打刻）です。

### notice

`Notice` のみ。`start_at`・`end_at`・`is_active` で掲示期間を制御し、
`NoticeManager.get_notice()` が現在有効なものを返します。
掲示する通知の追加・編集は `notice:manage`（`is_staff` 限定）から行います。

## URL 一覧

### ルート（`shifttimes/urls.py`）

| パス | 名前 | 説明 |
|---|---|---|
| `/` | `index` | トップページ |
| `/login/` | `login` | ログイン |
| `/logout/` | `logout` | ログアウト |
| `/activate/<uuid>/` | `activate_user` | アカウント有効化 |
| `/line/login/` | `custom_auth_line:login` | LINE ログインの開始 |
| `/line/link/` | `custom_auth_line:link` | LINE 連携の開始（ログイン） |
| `/line/callback/` | `custom_auth_line:callback` | LINE からのコールバック |
| `/line/unlink/` | `custom_auth_line:unlink` | LINE 連携の解除（ログイン・POST） |
| `/stripe/webhook/` | `stripe_webhook` | Stripe Webhook（CSRF 免除・POST のみ）※1 |
| `/admin/` | | Django 管理画面 |

※1 `ONPREMISE_MODE=true` では登録されません（[onpremise.md](onpremise.md)）。

### プロフィール（`/profile/`, `custom_auth`）

| パス | 名前 |
|---|---|
| `` | `custom_auth:index` |
| `password` | `custom_auth:password_change` |
| `email` | `custom_auth:email_change` |
| `edit` | `custom_auth:edit_profile` |
| `line` | `custom_auth:line_account` |
| `two_auth` | `custom_auth:list_two_auth` |
| `two_auth/add` | `custom_auth:add_two_auth` |

### グループ（`/group/`, `custom_auth_group`）

| パス | 名前 | 権限 |
|---|---|---|
| `` | `index` | ログイン |
| `<id>/` | `list` | メンバー |
| `add/` | `add` | ログイン（`allow_group_add` が必要） |
| `<id>/members/` | `members` | メンバー（メールは管理者のみ表示） |
| `<id>/edit` | `edit` | メンバー（更新は管理者） |
| `<id>/permission` | `permission` | メンバー（変更は管理者） |
| `<id>/admin/` | `admin_home` | 管理者 |
| `<id>/register/` | `register_member` | 管理者 |
| `<id>/register-admin/` | `register_admin` | 管理者 |
| `<id>/billing/` | `billing` | 管理者 |
| `<id>/billing/checkout/` | `billing_checkout` | 管理者・POST |
| `<id>/billing/success/` | `billing_success` | 管理者 |
| `<id>/billing/change/` | `billing_change_plan` | 管理者・POST |
| `<id>/billing/cancel/` | `billing_cancel` | 管理者・POST |
| `<id>/billing/resume/` | `billing_resume` | 管理者・POST |
| `<id>/billing/portal/` | `billing_portal` | 管理者・POST |

`billing` で始まる URL は `ONPREMISE_MODE=true` では登録されません。
テンプレートから参照するときは `billing_enabled` で囲んでください
（[onpremise.md](onpremise.md)）。

### シフト・勤怠（`/shift/`, `shift`）

| パス | 名前 | 説明 |
|---|---|---|
| `` | `index` | 所属グループごとの入力導線 |
| `calendar/<id>/` | `shift_calendar` | 自分のシフト希望（月カレンダー） |
| `entry-table/<id>/` | `entry_table` | 月ごとの一覧表からまとめて登録 |
| `confirm/<id>/` | `shift_confirm` | 下書きの一括確定（管理者） |
| `schedule/` | `schedule_index` | 開講スケジュールのグループ選択 |
| `schedule/<id>/` | `schedule` | 開講スケジュール（管理者） |
| `schedule/<id>/view/` | `schedule_member` | 開講スケジュール（メンバー） |
| `schedule/<id>/settings/` | `schedule_settings` | 開講区分・提出期限の設定（管理者） |
| `schedule/<id>/deadlines/<id>/delete/` | `deadline_delete` | 提出期限の削除（管理者） |
| `schedule/<id>/slack/` | `slack_settings` | Slack 通知の設定と手動送信（管理者） |
| `summary/` | `summary_index` | 提出状況のグループ選択 |
| `summary/<id>/` | `summary` | 提出状況（管理者） |
| `attendance/` | `attendance_index` | 勤怠が有効な所属グループ一覧 |
| `attendance/<id>/` | `attendance` | 自分の勤怠（月ごとの一覧表） |
| `attendance/<id>/clock/` | `attendance_clock` | 出退勤の打刻 |
| `attendance/admin/` | `attendance_admin_index` | 勤怠のグループ選択（管理者） |
| `attendance/<id>/settings/` | `attendance_settings` | 勤怠設定（管理者） |
| `attendance/<id>/summary/` | `attendance_summary` | 勤怠集計（管理者） |

### その他

- `/notice/` — お知らせ一覧（`notice:index`）
- `/notice/manage/` — 通知の追加・編集（`notice:manage`。`is_staff` 限定）
- `/notice/manage/<id>/delete/` — 通知の削除（`notice:notice_delete`。`is_staff` 限定）
- `/forget/` — パスワードリセット（`custom_auth_forget`）

## 設計上の約束ごと

### 権限チェック

ビューの冒頭で `request.user.usergroup_set.filter(group_id=..., ...)` を引いて、
所属していなければ `error.html` を返す、という形に統一しています。
管理者限定のビューは `is_admin=True` を条件に加えます。

```python
user_group = request.user.usergroup_set.filter(group_id=group_id, user=request.user).first()
if not user_group or not user_group.is_admin:
    return render(request, "error.html", {"text": "このグループの管理者権限がありません"})
```

課金まわりは `billing_views._get_admin_group()` にこの処理をまとめています。

### 動作モードの出し分け

`ONPREMISE_MODE` による出し分けは `settings.BILLING_ENABLED` に集約しています。
テンプレートでは `shifttimes.context_processors.site` が渡す
`billing_enabled` / `onpremise_mode` / `site_name` を使います。
`ONPREMISE_MODE` を直接見るのは `settings.py` とトップページの分岐だけです。

LINE ログインの出し分けは `custom_auth.line.is_enabled()`（チャネル ID と
シークレットが両方あるか）に集約し、テンプレートには同じコンテキスト
プロセッサから `line_login_enabled` として渡します。課金と違って URL は
常に登録されるため、無効なときはビューが案内を返します。

### タイムゾーン

`USE_TZ = False` / `TIME_ZONE = "Asia/Tokyo"` です。
**naive な `datetime` は JST として解釈されます。**

外部から UNIX 時刻を受け取って保存するときは、UTC のまま入れると 9 時間ずれます。
Stripe の日時は `billing_views._to_datetime()` で JST の naive に変換しています。

```python
value = datetime.datetime.fromtimestamp(unix_timestamp, tz=datetime.UTC)
if settings.USE_TZ:
    return value
return timezone.make_naive(value, timezone.get_default_timezone())
```

### 変更履歴

主要モデルには django-simple-history の `HistoricalRecords()` を付けています。
`simple_history.middleware.HistoryRequestMiddleware` により、変更したユーザも
記録されます。管理画面は `SimpleHistoryAdmin` を使うので履歴タブが出ます。

### LINE ログイン

`custom_auth/line.py`（LINE の API）と `custom_auth/line_views.py`（画面）に
分かれています。詳細は [line-login.md](line-login.md) を参照してください。

- 認可コードフロー。`state` と `nonce` はセッションに持ち、コールバックで
  `state` を突き合わせたらその場で捨てる（再利用させない）
- ID トークンの検証は LINE の `/oauth2/v2.1/verify` に任せる
- 通信は標準ライブラリの `urllib`。失敗は `line.LineLoginError` にまとめ、
  画面には日本語のメッセージだけを出す
- **連携済みのユーザしかログインできない**。LINE から新規ユーザは作らない

### Slack 通知

送信先の違う 2 系統があります。混ぜないように注意してください。

| 系統 | モジュール | 送信先 | 誰が設定するか |
|---|---|---|---|
| 運営向けの監査ログ | `shifttimes/notify.py` | `settings.SLACK_WEBHOOK_URL_LOG`（全体で 1 つ） | 運営（設定ファイル） |
| グループ向けの業務通知 | `shift/slack.py` | `SlackNotificationSetting.webhook_url`（グループごと） | グループ管理者（画面） |

**運営向け**は、モデルの作成・更新・削除を各アプリの `signals.py` から飛ばします。
Webhook URL は `getattr(settings, "SLACK_WEBHOOK_URL_LOG", "")` で読むため、
**`settings.py` には定義されていません**。`develop_settings.py` などの
ローカル設定モジュールで定義しないと通知は常に無効です（例外にはなりません）。

**グループ向け**は `shift/slack.py` にまとめています。送れるのは 4 種類です。

| 通知 | 送信のきっかけ | 設定項目 |
|---|---|---|
| シフト入力のお願い | 管理者が Slack 通知設定ページで送信ボタンを押す | （常に手動） |
| 提出期限のリマインド | `send_shift_reminders` コマンド（cron などで 1 日 1 回） | `notify_deadline_reminder`, `reminder_days_before` |
| メンバーのシフト確定 | `shift_confirm` で下書きを確定したとき | `notify_shift_confirmed` |
| 提出期限・開講スケジュールの変更 | `schedule_settings` で保存したとき | `notify_schedule_changed` |

設計上の約束ごと。

- 送信関数は `(送信できたか, エラーメッセージ)` を返し、**例外を外に出しません**。
  Slack が落ちていてもシフトの登録や設定の保存は成功させます。
- `is_ready`（有効かつ Webhook URL 登録済み）でない設定には何も送りません。
- リマインドは提出期限 1 件につき 1 回だけです。送信済みかどうかは
  `ShiftDeadline.reminder_sent_at` で判定し、期限を保存し直すと `None` に戻って再送できます。
- Webhook URL は秘密情報なので、`HistoricalRecords(excluded_fields=["webhook_url"])` で
  履歴に残さず、設定画面の入力欄にも既存値を描画しません（伏せ字のみ表示）。
- メッセージ中の URL は `settings.SITE_URL` を基準に組み立てます。

### テンプレート

- 置き場所は `shifttimes/templates/`（`APP_DIRS` も有効）
- すべて `base.html` を継承し、`title` / `page_title` / `page_subtitle` /
  `page_actions` / `content` のブロックを埋める
- Bootstrap 5 と Bootstrap Icons を CDN から読み込む
- 完了は `done.html`、エラーは `error.html` に `text` を渡して表示する

### コンテキストプロセッサ

ナビゲーションの出し分けに 2 つ登録しています。

| 名前 | 提供する変数 |
|---|---|
| `shift.context_processors.shift_admin_groups` | `shift_admin_groups`, `has_shift_admin_groups` |
| `shift.context_processors.attendance_groups` | `has_attendance_groups` |

どちらも `is_staff` のユーザには全グループを見せます。

## テスト

```bash
uv run python manage.py test
```

`custom_auth/tests.py` と `shift/tests.py` にあります。
外部 API は `unittest.mock` で差し替えるため、ネットワークには依存しません。

## Lint

```bash
uv run ruff check .
```

`pyproject.toml` で `line-length = 120`、`E`/`W`/`F`/`I`/`C`/`B`/`UP` を有効に
しています。マイグレーションは除外対象です。

> 既存コードには未修正の指摘が残っています（主に `UP031`: `%` 書式）。
> 新しく書くコードでは指摘を増やさないでください。

## 注意が必要な箇所

- `files/entrypoint.sh` の管理ユーザ作成は `django.contrib.auth.models.User` を
  import していますが、このプロジェクトの `AUTH_USER_MODEL` は
  `custom_auth.User` です。`SKIP_SUPERUSER=false` で動かすと失敗します
- `__pycache__` 配下の `.pyc` がリポジトリに追跡されています。差分が読みにくく
  なるため、`.gitignore` への追加と `git rm --cached` での除去を推奨します
- `shifttimes/models.py` の `MediumTextField` は `custom_auth/models.py` でも
  import されていますが未使用です（`F401`）
