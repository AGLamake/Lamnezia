# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.13 AS uv

# Build stage: install dependencies and collect static files.
FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
# GNU gettext compiles the interface translations (.po -> .mo); only the build stage needs it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gettext \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
COPY . .
# Tailwind standalone CLI (pinned, checksum-verified) builds static/css/app.css; no Node.js.
RUN --mount=type=cache,target=/app/.cache/tailwind .venv/bin/python scripts/tailwind.py
# Placeholders: settings require these keys, collectstatic and compilemessages never use them.
RUN export LAMNEZIA_SECRET_KEY=build-only LAMNEZIA_ENCRYPTION_KEY=build-only \
    && .venv/bin/python manage.py compilemessages --ignore=.venv \
    && .venv/bin/python manage.py collectstatic --noinput

# Runtime stage: the same Python, the built app, an unprivileged user.
FROM python:3.12-slim
RUN useradd --system --create-home --uid 1000 lamnezia \
    && mkdir -p /app/media \
    && chown lamnezia:lamnezia /app/media
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings
WORKDIR /app
COPY --from=build /app /app
USER lamnezia
EXPOSE 8000
CMD ["gunicorn", "config.asgi:application", "--worker-class", "uvicorn_worker.UvicornWorker", "--bind", "0.0.0.0:8000"]
