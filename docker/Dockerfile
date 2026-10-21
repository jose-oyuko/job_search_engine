# syntax=docker/dockerfile:1
# Multi-stage, multi-arch (amd64 + arm64). Not used for deployment until Phase 12.

# ---- Stage 1: build the Tailwind CSS with the standalone CLI (no Node) ----
FROM debian:bookworm-slim AS css
ARG TARGETARCH
ARG TAILWIND_VERSION=v4.1.11
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN case "${TARGETARCH:-amd64}" in \
      amd64) arch=x64 ;; \
      arm64) arch=arm64 ;; \
      *) echo "unsupported arch ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/tailwind \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-${arch}" \
    && chmod +x /usr/local/bin/tailwind
COPY static/src ./static/src
COPY templates ./templates
COPY apps ./apps
RUN tailwind -i static/src/input.css -o static/css/app.css --minify

# ---- Stage 2: install Python dependencies into a venv ----
FROM python:3.12-slim-bookworm AS deps
COPY --from=ghcr.io/astral-sh/uv:0.8.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# ---- Stage 3: runtime ----
FROM python:3.12-slim-bookworm AS runtime
# WeasyPrint (Phase 7) needs Pango and friends; installing now keeps the image stable.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libfontconfig1 shared-mime-info \
      fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --system app && useradd --system --gid app --home /app --shell /usr/sbin/nologin app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=deps /app/.venv /app/.venv
COPY --chown=app:app . .
COPY --from=css --chown=app:app /build/static/css/app.css /app/static/css/app.css
RUN mkdir -p /data /app/staticfiles && chown -R app:app /data /app/staticfiles
USER app
ENV DJANGO_SETTINGS_MODULE=jobpilot.settings.prod
EXPOSE 8000
ENTRYPOINT ["sh", "/app/docker/entrypoint.sh"]
CMD ["gunicorn", "jobpilot.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
