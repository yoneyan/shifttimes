FROM python:3.12 AS app

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /bin/

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates nginx python3-dev xmlsec1 libxmlsec1-dev \
    libldap2-dev libsasl2-dev slapd ldap-utils tox \
    lcov valgrind \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app

ENV PYTHONPATH=/opt/app/
# uv が管理する仮想環境を PATH に載せ、manage.py / gunicorn をそのまま実行できるようにする
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml /opt/app/pyproject.toml
COPY uv.lock /opt/app/uv.lock
COPY .python-version /opt/app/.python-version
RUN uv sync --locked --no-dev --group prod

COPY manage.py /opt/app/
COPY shifttimes/ /opt/app/shifttimes/
COPY custom_auth/ /opt/app/custom_auth/
COPY notice/ /opt/app/notice/
COPY shift/ /opt/app/shift/

# NGINX
RUN python manage.py collectstatic --noinput && \
    ln -s /opt/app/static /var/www/html/static
COPY files/default.conf /etc/nginx/sites-enabled/default

#EXPOSE 80
EXPOSE 8010

COPY files/entrypoint.sh /opt/app/
CMD ["bash", "-xe", "/opt/app/entrypoint.sh"]
