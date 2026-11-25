#!/usr/bin/env bash
# Cloudflare Pages build command: bash scripts/cloudflare_build.sh (or python3 scripts/build.py)
# Output directory:               public
set -euo pipefail

python3 --version
python3 -m pip install --quiet -r requirements.txt
python3 scripts/build.py
python3 scripts/check_brand_purge.py
