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

# Writable locations for collectstatic, uploads and logs.
RUN mkdir -p /app/staticfiles /app/media /app/logs && \
    addgroup --system django && \
    adduser --system --ingroup django django && \
    chown -R django:django /app

USER django

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/ || exit 1

# Overridden by docker-compose.yml for development (runserver + migrations).
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]