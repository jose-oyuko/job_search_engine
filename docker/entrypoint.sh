#!/bin/sh
# Container entrypoint. Only the web service collects static files and migrates, and only when
# asked to, so worker/bot containers never race the web container on startup.
set -e

if [ "${RUN_COLLECTSTATIC:-0}" = "1" ]; then
  # "src" holds the Tailwind input, which is not a deployable static file.
  python manage.py collectstatic --noinput --ignore src
fi
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  python manage.py migrate --noinput
  # Backing table for the shared rate-limit counters (no-op if it already exists).
  python manage.py createcachetable
fi

exec "$@"
