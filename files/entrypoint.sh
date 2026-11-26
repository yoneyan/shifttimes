#!/bin/bash

set -e

# SECRET_KEY が未設定だと settings.py の公開済みの開発用キーで動いてしまい、
# セッションやパスワードリセットのトークンを第三者が偽造できる。本番コンテナでは起動させない
if [ -z "${SECRET_KEY:+set}" ] && [ "${DEBUG,,}" != "true" ]; then
  echo "ERROR: SECRET_KEY が設定されていません。十分に長いランダムな値を環境変数で渡してください。" >&2
  exit 1
fi

/usr/sbin/nginx -g "daemon off;" &

# DB Migration
# Check if update is needed
if ! ./manage.py migrate --check >/dev/null 2>&1; then
  ./manage.py migrate --no-input
  ./manage.py clearsessions
fi

# Create Superuser
if [ "$SKIP_SUPERUSER" == "false" ]; then
  if [ -z "${SUPERUSER_PASSWORD:+set}" ]; then
    # 既定パスワードで管理ユーザを作ると誰でも管理画面に入れてしまうため、パスワードは必須にする
    echo "ERROR: SKIP_SUPERUSER=false のときは SUPERUSER_PASSWORD を指定してください。" >&2
    exit 1
  fi
  export SUPERUSER_NAME="${SUPERUSER_NAME:-admin}"
  export SUPERUSER_EMAIL="${SUPERUSER_EMAIL:-admin@example.com}"
  export SUPERUSER_PASSWORD

  # 値はシェル展開でコードに埋め込まず、環境変数から読む（引用符などを含む値でのコード注入を防ぐ）
  ./manage.py shell --interface python <<'END'
import os

from django.contrib.auth import get_user_model

User = get_user_model()
name = os.environ["SUPERUSER_NAME"]
if not User.objects.filter(username=name).exists():
    User.objects.create_superuser(name, os.environ["SUPERUSER_EMAIL"], os.environ["SUPERUSER_PASSWORD"])
    print(f"Created superuser Username: {name}")
END
fi

gunicorn --bind 0.0.0.0:8000 -w 2 shifttimes.wsgi:application
