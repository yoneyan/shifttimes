# オンプレミスモード

自社サーバなどに自前で立てて使うための動作モードです。
オンプレミスでの運用は **Enterprise プランの契約形態** として扱います。
`ONPREMISE_MODE=true` を渡すと、SaaS 前提の課金まわりを丸ごと落とし、
全グループを Enterprise プラン（人数無制限）として扱い、
トップページをサイト名だけの簡素な入口に差し替えます。

```bash
export ONPREMISE_MODE=true
export SITE_NAME=社内シフト管理
```

- 切り替え: [`shifttimes/settings.py`](../shifttimes/settings.py)
- 画面出し分け: [`shifttimes/context_processors.py`](../shifttimes/context_processors.py)
- トップページ: [`shifttimes/templates/landing_onpremise.html`](../shifttimes/templates/landing_onpremise.html)
- テスト: [`shifttimes/tests.py`](../shifttimes/tests.py)

## 通常モードとの違い

| | 通常（SaaS） | オンプレミス |
|---|---|---|
| トップページ（未ログイン） | 機能紹介・料金プラン・問い合わせ | サイト名とログインボタンのみ |
| 適用プラン | 契約・無償付与に応じて決まる | 常に Enterprise |
| メンバー数の上限 | プランごとの上限 | 無制限 |
| 請求画面 (`/group/<id>/billing/`) | あり | URL ごと未登録（404） |
| Stripe Webhook (`/stripe/webhook/`) | あり | URL ごと未登録（404） |
| お知らせの「未課金のグループ」警告 | 出る | 出ない |
| 管理者ページの「請求管理」ボタン | 出る | 出ない |
| 管理者ページのプラン表示 | 契約中のプラン | `Enterprise` |
| Django 管理画面の Stripe / 無償化の項目 | 出る | 出ない |

## 設定値の上書き

`ONPREMISE_MODE=true` のとき、`settings.py` は次のように値を差し替えます。

| 設定 | 値 | 理由 |
|---|---|---|
| `BILLING_ENABLED` | `False` | URL とテンプレートの出し分けに使う共通フラグ |
| `FREE_PLAN` | `UNLIMITED_PLAN` と同じ内容 | 契約のないグループを Enterprise 相当として扱う |
| `STRIPE_PLANS` | `{}` | 販売するプランがない |
| `STRIPE_SECRET_KEY` 他 | `""` | 環境に残っていても Stripe API を叩かせない |

`STRIPE_*` や `FREE_PLAN_MAX_MEMBERS` を環境変数で渡しても無視されます。

`FREE_PLAN`（＝契約のないグループに適用されるプラン）が `UNLIMITED_PLAN` そのものに
なるため、`Group.plan` は常に Enterprise の定義を返し、`Group.max_members` は
`None`（無制限）になります。Enterprise は `rank` が最大値なので、
無償付与や過去の契約が残っていても常にこちらが優先されます。

## サイト名

`SITE_NAME`（既定値 `シフト管理システム`）は、モードを問わず
ヘッダーのブランド表示・フッター・ブラウザのタイトルに使われます。
オンプレミスモードではトップページの見出しにもこの値が出ます。

## テスト

課金前提のテストは `@skipUnless(settings.BILLING_ENABLED, ...)` を付けてあるため、
`ONPREMISE_MODE=true` を付けたままでもテストは通ります。

```bash
uv run python manage.py test                     # 通常モード
ONPREMISE_MODE=true uv run python manage.py test # オンプレミスモード
```

## 画面の出し分けを追加するとき

課金導線を新しく足す場合は、テンプレート側を必ず `billing_enabled` で囲みます。

```html
{% if billing_enabled %}
  <a href="{% url 'custom_auth_group:billing' group.id %}">請求管理</a>
{% endif %}
```

オンプレミスモードでは URL 自体が登録されないため、`{% if %}` を忘れると
`NoReverseMatch` でページ全体が 500 になります。
ビュー側で分岐する場合は `settings.BILLING_ENABLED` を見てください。

`urlpatterns` は import 時に組み立てられるので、テストで URL の有無を
切り替えるには `override_settings` だけでは足りません。
`shifttimes/tests.py` の `onpremise_urlconf()` のように URLConf を読み直します。
