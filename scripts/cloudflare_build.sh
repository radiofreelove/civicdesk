#!/usr/bin/env bash
# Cloudflare Workers Builds build command: bash scripts/cloudflare_build.sh
# Static assets directory (wrangler.jsonc): ./public
set -euo pipefail

python3 --version
python3 -m pip install --quiet -r requirements.txt
python3 scripts/build.py
python3 scripts/check_brand_purge.py
