#!/usr/bin/env python3
"""Scan tracked file paths and text contents for retired brand terms."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2",
    ".ttf", ".otf", ".eot", ".pdf", ".zip", ".gz", ".tar", ".pyc",
}

FORBIDDEN_PATTERN = re.compile(
    r"\b(?:stasis|strand|destiny\s+2|destiny|bungie|lightfall|beyond\s+light|spacefast"
    # Retired Civic Horizon visual system (replaced by Broadsheet, 2026-11).
    r"|civic\s+horizon|horizon-(?:sky|paper|rose|slate))\b",
    re.IGNORECASE,
)

ALLOWED_STRINGS = [
    "venous stasis",
    "projectstasis.pages.dev",
    "projectstrand.pages.dev",
    "radiofreelove/projectstasis",
    "radiofreelove/projectstrand",
]

ALLOWED_PATTERN = re.compile(
    "|".join(re.escape(s) for s in ALLOWED_STRINGS),
    re.IGNORECASE,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


def strip_allowed(text: str) -> str:
    return ALLOWED_PATTERN.sub("", text)


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    violations: list[str] = []
    self_path = "scripts/check_brand_purge.py"

    for rel_path in result.stdout.splitlines():
        if not rel_path or rel_path == self_path:
            continue
        if FORBIDDEN_PATTERN.search(strip_allowed(rel_path)):
            violations.append(f"Path violation: {rel_path}")
        path = REPO_ROOT / rel_path
        if path.suffix.lower() in BINARY_EXTENSIONS or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            cleaned = strip_allowed(line)
            match = FORBIDDEN_PATTERN.search(cleaned)
            if match:
                violations.append(f"{rel_path}:{lineno}: matched '{match.group(0)}'")

    if violations:
        for v in violations:
            print(v, file=sys.stderr)
        return 1
    print("Brand purge check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
