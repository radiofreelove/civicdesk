"""Shared kind-based source freshness limits used by build.py and verify_sources.py."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DEFAULT_MAX_AGE_DAYS = 120
MAX_AGE_BY_KIND: dict[str, int] = {
    # Short window: forms, fee schedules, and active litigation dockets
    "agency-form": 60,
    "court-form": 60,
    "state-form": 60,
    "fee-schedule": 60,
    "court": 60,
    # Medium window: live agency and court web pages / letters
    "agency-page": 90,
    "court-page": 90,
    "state-page": 90,
    "federal-page": 90,
    "federal-letter": 90,
    # Standard window: explainers, guides, and external reference pages
    "state-explainer": 120,
    "guide": 120,
    "legal-aid": 120,
    "advocacy": 120,
    "helper": 120,
    "legislature": 120,
    "platform": 120,
    "secondary": 120,
    # Longer window: codified statutes, session laws, rules, standards, and fixed datasets
    "statute": 180,
    "session-law": 180,
    "state-rule": 180,
    "federal-rule": 180,
    "executive-order": 180,
    "standard": 180,
    "accessibility-standard": 180,
    "data": 180,
}


def max_age_for_source(source: Mapping[str, Any], override_days: int | None = None) -> int:
    if override_days is not None:
        return int(override_days)
    if source.get("max_age_days") is not None:
        return int(source["max_age_days"])
    kind = str(source.get("kind") or "").strip().lower()
    return MAX_AGE_BY_KIND.get(kind, DEFAULT_MAX_AGE_DAYS)
