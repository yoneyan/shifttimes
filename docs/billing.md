# 課金・Stripe 運用ガイド

グループ単位の月額サブスクリプションを Stripe で扱います。
プランによって変わるのは **グループに所属できるメンバー数の上限だけ** で、
シフト・勤怠・通知などの機能はどのプランでも同じように使えます。

- 実装: [`custom_auth/billing_views.py`](../custom_auth/billing_views.py)
- プラン定義: [`shifttimes/settings.py`](../shifttimes/settings.py)
- 画面: [`shifttimes/templates/group/billing.html`](../shifttimes/templates/group/billing.html)
- テスト: [`custom_auth/tests.py`](../custom_auth/tests.py)

## プラン

| プラン | 月額 | メンバー上限 | rank | 備考 |
|---|---|---|---|---|
| Free | ¥0 | 10（`FREE_PLAN_MAX_MEMBERS`） | 0 | 未契約のグループに自動適用 |
| Standard | ¥1,980 | 30 | 10 | 既定のおすすめプラン |
| Pro 1 | ¥4,980 | 100 | 20 | |
| Pro 2 | ¥9,980 | 250 | 30 | |
| Enterprise | 個別 | 無制限 | 99 | Stripe では販売せず、無償付与で `unlimited` を指定して実現する |

`rank` はプランの上下比較に使う数値です。アップグレードかダウングレードかの判定、
および「無償付与」と「有料契約」を併用している場合にどちらを適用するかの判定に使います。

金額やメンバー上限を変えるときは `settings.STRIPE_PLANS` を編集します。
Price ID だけは環境変数から読み込むため、Stripe 側の金額と `amount` の値が
ずれないように両方そろえて更新してください。

## Stripe 側の初期設定

### 1. 商品と価格を作る

Stripe ダッシュボードの「商品カタログ」で、プランごとに商品を 1 つ作り、
**継続（月次）** の価格を追加します。通貨は JPY です。

作成した価格の Price ID（`price_` で始まる文字列）を環境変数に設定します。

```bash
export STRIPE_PRICE_ID_STANDARD=price_xxxxxxxxxxxx
export STRIPE_PRICE_ID_PRO1=price_xxxxxxxxxxxx
export STRIPE_PRICE_ID_PRO2=price_xxxxxxxxxxxx
```

Price ID が未設定のプランは、請求画面でボタンが「準備中」として無効表示になります。

### 2. API キーを設定する

```bash
export STRIPE_SECRET_KEY=sk_test_xxxxxxxxxxxx
export STRIPE_PUBLISHABLE_KEY=pk_test_xxxxxxxxxxxx
```

`STRIPE_SECRET_KEY` が空のときは、請求画面に「Stripe が未設定」の案内が出て
申し込み導線がすべて無効になります。開発中に誤って本番の決済を叩かないための安全弁です。

### 3. カスタマーポータルを有効にする

支払い方法の変更と請求書の閲覧は Stripe のカスタマーポータルに委譲しています。
ダッシュボードの「設定 → Billing → カスタマーポータル」で機能を有効化してください。
有効化していないと `billing_portal.Session.create` がエラーになります。

### 4. Webhook を設定する

エンドポイントは `POST /stripe/webhook/` です。購読するイベントは次の 6 つです。

| イベント | 用途 |
|---|---|
| `checkout.session.completed` | 申し込み完了時にサブスクリプションをグループへ紐づける |
| `customer.subscription.created` | 契約開始を反映 |
| `customer.subscription.updated` | プラン変更・ステータス変更・期間末解約の予約を反映 |
| `customer.subscription.deleted` | 契約終了。プランを失効させ Free に戻す |
| `invoice.payment_succeeded` | 支払い成功後のステータスを取り直す |
| `invoice.payment_failed` | 支払い失敗（`past_due` など）を反映 |

署名シークレットを設定します。

```bash
export STRIPE_WEBHOOK_SECRET=whsec_xxxxxxxxxxxx
```

`STRIPE_WEBHOOK_SECRET` が空のとき、Webhook は署名検証をせずに 400 を返します。
検証なしでイベントを受け付けることはありません。

### ローカルでの Webhook 受信

```bash
stripe listen --forward-to localhost:8000/stripe/webhook/ --events checkout.session.completed,customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,invoice.payment_succeeded,invoice.payment_failed
```

`stripe listen` が表示する `whsec_` から始まる値を `STRIPE_WEBHOOK_SECRET` に設定します。

## 契約状態の持ち方

Stripe の状態は `Group` モデルにキャッシュします。Stripe を「正」としつつ、
API が落ちている間も最後に同期した内容で画面を出し続けるためです。

| フィールド | 内容 |
|---|---|
| `stripe_customer_id` | Stripe の Customer ID。グループごとに 1 つ作る |
| `stripe_subscription_id` | Stripe の Subscription ID |
| `stripe_plan` | プランキー（`standard` など）。失効時は空文字 |
| `stripe_status` | Stripe のステータス（`active`、`past_due`、`canceled` など） |
| `stripe_current_period_end` | 現在の請求期間の終了日 |
| `stripe_cancel_at_period_end` | 期間末に解約する予約が入っているか |

同期のきっかけは 2 つです。

1. **Webhook**（正。契約確定・変更・失効はここで確実に反映される）
2. **請求画面の表示時**（補助。管理者が画面を開いたときに最新化する）

申し込み完了後のリダイレクト先（`billing_success`）でも同期しますが、これは
ユーザがその場で結果を見られるようにするための補助です。ユーザが決済後に
ブラウザを閉じても、Webhook が契約を確定させます。

### 利用可能とみなすステータス

`ACTIVE_SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due")`

`past_due` を含めているのは、Stripe が自動でリトライしている最中に
サービスを止めてしまわないためです。画面には警告を出しつつ、機能は使えます。

## 適用されるプランの決まり方

`Group.plan_key` が以下の順で候補を集め、**`rank` が最も高いもの**を採用します。

1. Free（常に候補）
2. 無償付与のプラン（`free_plan` が設定されていて、期限内のとき）
3. 有料契約のプラン（`stripe_status` が利用可能な状態のとき）

そのため、Standard を無償付与されているグループが自分で Pro 2 を契約した場合は
Pro 2 が適用されます。逆に Enterprise（`unlimited`）を無償付与されていれば、
契約の有無にかかわらず無制限になります。

## 無償化（無償付与）の運用

学生団体や運営関係のグループへ、Stripe の契約なしに有料プラン相当を付与する機能です。

### 手順

1. Django 管理画面の「グループ」を開く
2. 対象グループの編集画面で **「無償化」** セクションを開く
3. **無償付与プラン** で付与するプランを選ぶ（`Enterprise 相当(人数無制限)` も選べる）
4. **無償付与の理由** に判断の根拠を書く（監査のために必ず埋める）
5. 期限を切る場合は **Membership → 有効期限** に日時を入れる（未設定なら無期限）
6. 保存する

付与された内容は請求画面に「無償付与」のバッジと理由・期限つきで表示され、
グループ管理者からも確認できます。お支払いは発生しません。

### 期限切れの扱い

`有効期限` を過ぎると無償付与は自動的に無効になり、
有料契約がなければ Free プランの上限に戻ります。バッチ処理は不要で、
`Group.is_free_granted` が参照のたびに判定します。

既存メンバーが強制的に削除されることはありません。上限を超えた状態では
**新規メンバーの追加だけ**ができなくなります。

## メンバー数上限の適用

- グループ管理者がメンバーを登録する画面（`register_member` / `register_admin`）で、
  登録前に `Group.can_add_member()` を検査します
- 上限に達している場合はフォームを無効化し、請求画面への導線を出します
- POST を直接投げられても `_register_user_to_group` 側で弾きます
- ダウングレードも同様で、現在のメンバー数が移行先プランの上限を超えるときは
  理由つきでボタンを無効化し、サーバ側でも拒否します

## 解約とプラン変更

### 解約

即時解約ではなく **期間末解約**（`cancel_at_period_end=True`）です。
支払い済みの期間は最後まで使えます。予約後は請求画面に
「解約を取り消して継続する」ボタンが出るので、終了日までなら戻せます。

期間が終了すると Stripe が `customer.subscription.deleted` を送り、
その Webhook でプランが失効して Free に戻ります。

### プラン変更

契約中でも他のプランへ変更できます。サブスクリプションの明細行の Price を
差し替えるだけで、差額の計算と請求は Stripe に任せます
（`proration_behavior="create_prorations"`）。

二重契約を防ぐため、すでに有効な契約があるグループが新規申し込み（Checkout）を
POST した場合は、決済セッションを作らずに請求画面へ戻します。

## 障害時の挙動

| 状況 | 挙動 |
|---|---|
| `STRIPE_SECRET_KEY` 未設定 | 請求画面は表示されるが申し込み・変更・解約はすべて無効。案内を表示 |
| Stripe API がダウン | 請求画面は警告を出しつつ、最後に同期した内容で表示を継続。機能は止まらない |
| `STRIPE_WEBHOOK_SECRET` 未設定 | Webhook は 400 を返す（検証なしでは受け付けない） |
| 署名が不正 | 400 を返す |
| 未対応のイベント種別 | 何もせず 200 を返す（Stripe 側でのリトライを防ぐ） |
| Price ID が未知 | プランを空（Free 相当）として扱う |

## テスト

課金まわりのテストは `custom_auth/tests.py` にあります。Stripe API は
`unittest.mock` で差し替えているため、ネットワークにも API キーにも依存しません。

```bash
uv run python manage.py test custom_auth
```

| テストクラス | 対象 |
|---|---|
| `PlanResolutionTests` | 無償付与・有料契約・期限切れからの適用プラン決定 |
| `MemberLimitTests` | メンバー数上限の適用 |
| `SubscriptionSyncTests` | Stripe レスポンスの取り込み、日時変換、請求書からの ID 抽出 |
| `BillingViewTests` | 請求画面、Checkout、プラン変更、解約、継続、ポータル、権限 |
| `StripeWebhookTests` | 署名検証、各イベントの処理 |

手動で通しで確認する場合は、Stripe のテストカード `4242 4242 4242 4242`
（有効期限は未来の任意の日付、CVC は任意の 3 桁）を使います。

## Stripe API バージョンについて

stripe-python 15.x が既定で使う API バージョン（`2026-08-26.dahlia` 系）では、
過去のバージョンから次の破壊的変更が入っています。実装はどちらの形でも
動くようにしてありますが、SDK を上げるときは改めて確認してください。

| 項目 | 変更前 | 変更後 |
|---|---|---|
| 請求期間の終了日 | `Subscription.current_period_end` | `SubscriptionItem.current_period_end` |
| 請求書のサブスクリプション | `Invoice.subscription` | `Invoice.parent.subscription_details.subscription` |

また、`Subscription` などの Stripe オブジェクトは `dict` のサブクラスです。
`subscription.items` と書くと `dict.items` メソッドに解決されてしまうため、
明細行を取るときは必ず `subscription["items"]` のようにキーでアクセスします。

## 既知の制限

- **Webhook の順序は保証されない**。Stripe はイベントの到着順を保証しないため、
  `customer.subscription.updated` が `deleted` の後に届くと状態が巻き戻ります。
  厳密にするならイベントの `created` を保存して、古いイベントを捨てる必要があります
- **支払い失敗時の通知がない**。`past_due` になっても管理者へメールや通知は飛びません。
  [`shifttimes/notify.py`](../shifttimes/notify.py) に未使用の `notice_payment()` が
  あるので、Webhook から呼び出せば Slack 通知を追加できます
  （あわせて `SLACK_WEBHOOK_URL_LOG` を `settings.py` に定義する必要があります）
- **請求書の履歴を自前で持っていない**。過去の支払い履歴はカスタマーポータル側で確認します
- **既存グループの移行**。上限を超えているグループは新規メンバーの追加だけが止まります。
  運用上まずい場合は、データマイグレーションで `free_plan` を付与するか
  `FREE_PLAN_MAX_MEMBERS` を引き上げてください
