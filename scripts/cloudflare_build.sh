#!/usr/bin/env bash
# Cloudflare Workers Builds build command: bash scripts/cloudflare_build.sh
# Static assets directory (wrangler.jsonc): ./public
set -euo pipefail

if [ -z "${WORKERS_CI_COMMIT_SHA:-}" ] && command -v git >/dev/null 2>&1; then
  WORKERS_CI_COMMIT_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
  export WORKERS_CI_COMMIT_SHA
fi

python3 --version
python3 -m pip install --quiet -r requirements.txt
python3 scripts/build.py
python3 scripts/check_brand_purge.py
