# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------------------
# Stage 1: build the Tailwind stylesheet.
# Kept separate so the final image does not carry the Node toolchain.
# ---------------------------------------------------------------------------------------
FROM node:22-alpine AS assets

WORKDIR /assets

# Install dependencies first: this layer is only invalidated when the lockfile changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY tailwind.config.js ./
COPY frontend/ ./frontend/
COPY templates/ ./templates/
COPY apps/ ./apps/

RUN npm run build:css


# ---------------------------------------------------------------------------------------
# Stage 2: Python runtime.
# ---------------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app

# libpq for psycopg, curl for the healthcheck, netcat for compose startup ordering.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Dependency layer: wheels are resolved from pyproject.toml only.
# NOTE: [tool.setuptools] packages = [] means the project itself installs no code, so
# the dependency group is installed before the source tree is copied.
# ---------------------------------------------------------------------------
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip && pip install ".[prod]" gunicorn uvicorn

COPY . .

# Stylesheet compiled in the assets stage overwrites the source-tree placeholder.
COPY --from=assets /assets/static/css/app.css /app/static/css/app.css

# Bake the static bundle into the image: production uses WhiteNoise's manifest storage,
# which refuses to serve anything until collectstatic has produced it. The inline
# environment satisfies production's fail-fast guards while building; every value is a
# placeholder, none of it is a secret, and none of it reaches the runtime container
# (runtime configuration comes from .env via docker-compose).
RUN DJANGO_SETTINGS_MODULE=config.settings.production \
    SECRET_KEY=build-only-collectstatic-key-not-a-secret-00000000000000 \
    ALLOWED_HOSTS=localhost \
    CSRF_TRUSTED_ORIGINS=https://localhost \
    DATABASE_URL=postgres://flashwear:flashwear@db:5432/flashwear \
    REDIS_URL=redis://redis:6379/0 \
    PAYMENT_PROVIDER=stripe \
    PAYMENT_WEBHOOK_SECRET=build-only-collectstatic-placeholder \
    EMAIL_HOST=smtp.example.invalid \
    python manage.py collectstatic --noinput

# Writable locations for collectstatic, uploads and logs, plus entrypoint permissions.
RUN mkdir -p /app/staticfiles /app/media /app/logs && \
    sed -i 's/\r$//' /app/entrypoint.sh && \
    chmod +x /app/entrypoint.sh && \
    addgroup --system django && \
    adduser --system --ingroup django --home /app django && \
    chown -R django:django /app

ENV HOME=/app
USER django

EXPOSE 8000

# /health/live/ is the liveness probe (it must answer 200, not a redirect: the
# X-Forwarded-Proto header tells Django the request is already HTTPS, so
# SECURE_SSL_REDIRECT does not answer 301 and mask a dead process). The runtime
# ALLOWED_HOSTS must therefore include 127.0.0.1 -- see .env.example.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS -H "X-Forwarded-Proto: https" http://127.0.0.1:8000/health/live/ || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "config.wsgi:application"]