# LINE ログイン

LINE アカウントでログインできるようにする機能です。**連携済みのユーザだけ**が
使えます。LINE ログインから新しいユーザが作られることはありません（ユーザの
作成はこれまでどおりグループ管理者が行います）。

## 使い方

1. ユーザ名とパスワードでログインする
2. 「アカウント情報」→「LINE連携」→「LINEアカウントを連携する」
3. LINE の同意画面で許可する
4. 次回からログイン画面の「LINEでログイン」が使える

連携の解除も同じページから行えます。解除してもユーザ名とパスワードでの
ログインには影響しません。

1 つの LINE アカウントを複数のユーザに連携することはできません。すでに別の
ユーザが連携している場合はエラーになります。1 ユーザにつき連携できる LINE
アカウントも 1 つです（連携済みの状態でもう一度連携すると上書きされます）。

## セットアップ

### 1. LINE Developers でチャネルを作る

1. [LINE Developers コンソール](https://developers.line.biz/console/) でプロバイダーを選ぶ
2. 「新規チャネル作成」→「**LINEログイン**」を選ぶ
3. アプリタイプは「ウェブアプリ」を選ぶ
4. 「LINEログイン設定」の**コールバック URL** に次を登録する

   ```
   https://<SITE_URL のホスト>/line/callback/
   ```

   例: `SITE_URL=https://shift.example.com` なら
   `https://shift.example.com/line/callback/`
   開発環境のぶんも同時に登録できます（改行区切り）。

5. 「チャネル基本設定」からチャネル ID とチャネルシークレットを控える

メールアドレスの取得権限（`email` スコープ）は申請不要です。このシステムは
メールアドレスを LINE から取得せず、要求するスコープは `profile openid` だけです。

### 2. 環境変数を設定する

| 変数 | 説明 |
|---|---|
| `LINE_LOGIN_CHANNEL_ID` | チャネル ID |
| `LINE_LOGIN_CHANNEL_SECRET` | チャネルシークレット |
| `SITE_URL` | コールバック URL の組み立てに使う。**LINE Developers に登録した値と一致させる** |

両方が設定されているときだけ機能が有効になり、ログイン画面とアカウント情報に
導線が出ます。どちらかが空なら導線は出ず、`/line/` 以下の URL は
「利用できません」を返します。

チャネルシークレットは秘密情報です。環境変数か、`.gitignore` されている
`shifttimes/develop_settings.py` に書いてください。

```python
from .settings import *  # noqa: F403

LINE_LOGIN_CHANNEL_ID = "2000000000"
LINE_LOGIN_CHANNEL_SECRET = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
SITE_URL = "http://localhost:8000"
```

### 3. マイグレーションを適用する

```bash
uv run python manage.py migrate
```

## 実装のしくみ

| ファイル | 役割 |
|---|---|
| `custom_auth/line.py` | LINE の API を叩く部分。認可 URL の組み立て、トークン交換、ID トークンの検証 |
| `custom_auth/line_views.py` | 画面側。開始 → コールバック → ログイン / 連携 |
| `custom_auth/line_urls.py` | `/line/` 以下の URL |
| `custom_auth.LineAccount` | ユーザと LINE ユーザ ID の対応（`OneToOne`。`line_user_id` は一意） |

認可コードフロー（OpenID Connect）です。

1. `/line/login/`（または `/line/link/`）で `state` と `nonce` を発行し、
   セッションに保存して LINE の同意画面へリダイレクトする
2. `/line/callback/` で `state` をセッションの値と突き合わせる。
   一致しなければ何もしない。`state` は成否にかかわらずその場で捨てるので
   使い回しはできない
3. 認可コードをトークンに交換し、ID トークンを LINE の
   `/oauth2/v2.1/verify` に渡して検証してもらう（署名・`iss`・`aud`・`exp`・
   `nonce` は LINE 側で確認される）
4. ID トークンの `sub`（LINE のユーザ ID）で `LineAccount` を引く

`sub` はチャネルごとに払い出される ID です。**チャネルを作り直すと値が変わり、
既存の連携はすべて無効になります。**

設計上の約束ごと。

- 通信は標準ライブラリの `urllib` で行い、依存パッケージは増やさない
- LINE 側の失敗はすべて `line.LineLoginError` にまとめ、画面には日本語の
  メッセージだけを出す。LINE の応答本文（`invalid_grant` など）はログに残す
- 有効・無効の判定は `line.is_enabled()` に集約する。テンプレートでは
  コンテキストプロセッサが渡す `line_login_enabled` を使う
- 課金まわりと違い URL は常に登録される。無効なときはビューが案内を返すので、
  テンプレートの `if` を外しても `NoReverseMatch` にはならない
- 連携されていない LINE アカウントでログインしようとしたときは
  `user/line_not_linked.html` で案内する。ユーザは作らない
- 無効化されたユーザ（`is_active=False`）は LINE からもログインできない

## 困ったときは

| 症状 | 原因 |
|---|---|
| ログイン画面にボタンが出ない | `LINE_LOGIN_CHANNEL_ID` か `LINE_LOGIN_CHANNEL_SECRET` が空 |
| LINE 側で `400 Bad Request` になる | コールバック URL が LINE Developers の登録と違う。`SITE_URL` を確認する |
| 「LINEとの通信に失敗しました」 | チャネルシークレットの誤りやコールバック URL の不一致。詳細はアプリのログに出る |
| 「セッションが無効です」 | 同意画面を開いたまま放置してセッションが切れた。やり直す |
| 「このLINEアカウントは別のユーザに連携済みです」 | 同じ LINE アカウントを 2 人に連携しようとしている。先に元のユーザで解除する |
