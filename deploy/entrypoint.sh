#!/bin/sh
set -eu

if [ -z "${TZ_CHECKER_TOKEN:-}" ]; then
  echo "ERROR: TZ_CHECKER_TOKEN is required for server deployment." >&2
  echo "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\"" >&2
  exit 1
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips="*"
