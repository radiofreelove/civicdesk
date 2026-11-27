#!/usr/bin/env python3
"""Weekly live API contract verifier for CivicDesk Policy Wire channels.

Performs one lightweight live request per channel (`User-Agent: CivicDesk-Verify/1.0`)
and validates upstream JSON/XML/HTML response shapes so breaking upstream API
changes surface immediately instead of silently returning zero dispatches.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Callable

try:
    from http_retry import fetch_with_retry
    from policy_radar import (
        FEDERAL_REGISTER_FIELDS,
        _WA_WSR_ROW_RE,
        discover_gemini_flash_models,
        redact_secrets,
    )
except ImportError:  # pragma: no cover - package-qualified import fallback
    from scripts.http_retry import fetch_with_retry  # type: ignore[no-redef]
    from scripts.policy_radar import (  # type: ignore[no-redef]
        FEDERAL_REGISTER_FIELDS,
        _WA_WSR_ROW_RE,
        discover_gemini_flash_models,
        redact_secrets,
    )


def _default_get(url: str, *, headers: dict[str, str] | None = None, method: str = "GET", body: bytes | None = None) -> bytes:
    status, _hdrs, payload = fetch_with_retry(
        url,
        headers=headers,
        method=method,
        body=body,
        timeout=25,
        max_retries=2,
        base_delay=1.0,
    )
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")
    return payload


def run_live_contract_checks(
    *,
    fetcher: Callable[..., bytes] = _default_get,
    env: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Run live contract probes across keyless and configured keyed channels."""
    cfg_env = env if env is not None else dict(os.environ)
    cl_token = (cfg_env.get("COURTLISTENER_TOKEN") or "").strip()
    leg_key = (cfg_env.get("LEGISCAN_API_KEY") or "").strip()
    tav_key = (cfg_env.get("TAVILY_API_KEY") or "").strip()
    gem_key = (cfg_env.get("GEMINI_API_KEY") or "").strip()
    secrets = (cl_token, leg_key, tav_key, gem_key)

    results: list[dict[str, str]] = []

    def _check(name: str, fn: Callable[[], str]) -> None:
        try:
            detail = fn()
            results.append({"channel": name, "status": "ok", "detail": redact_secrets(detail, secrets)})
        except Exception as exc:  # noqa: BLE001
            err = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
            results.append({"channel": name, "status": "error", "detail": err})

    # 1. Federal Register API v1 (documents + CFR filter + public inspection)
    def _probe_fedreg() -> str:
        params = urllib.parse.urlencode(
            [
                ("conditions[cfr][title]", "42"),
                ("conditions[cfr][part]", "435"),
                ("per_page", "2"),
                ("order", "newest"),
                *(("fields[]", f) for f in FEDERAL_REGISTER_FIELDS),
            ]
        )
        data = json.loads(fetcher(f"https://www.federalregister.gov/api/v1/documents.json?{params}").decode("utf-8"))
        docs = data.get("results")
        if not isinstance(docs, list) or not docs:
            raise ValueError("Federal Register CFR query returned missing/empty `results` list")
        doc0 = docs[0]
        for req in ("document_number", "title", "publication_date", "html_url", "type"):
            if not doc0.get(req):
                raise ValueError(f"Federal Register document missing `{req}`")
        if "effective_on" not in doc0 or "comments_close_on" not in doc0:
            raise ValueError("Federal Register response omitted `effective_on`/`comments_close_on` fields")

        pi = json.loads(
            fetcher("https://www.federalregister.gov/api/v1/public-inspection-documents.json?per_page=1").decode("utf-8")
        )
        if not isinstance(pi, dict) or "count" not in pi:
            raise ValueError("Federal Register Public Inspection response missing `count`")
        return f"documents={len(docs)}, public_inspection_count={pi['count']}"

    _check("Federal Register API v1", _probe_fedreg)

    # 2. eCFR Versioner API v1
    def _probe_ecfr() -> str:
        data = json.loads(
            fetcher("https://www.ecfr.gov/api/versioner/v1/versions/title-22.json?part=51").decode("utf-8")
        )
        versions = data.get("content_versions")
        if not isinstance(versions, list) or not versions:
            raise ValueError("eCFR response missing non-empty `content_versions` list")
        v0 = versions[0]
        for req in ("identifier", "issue_date", "amendment_date", "name"):
            if req not in v0:
                raise ValueError(f"eCFR content_version missing `{req}`")
        return f"content_versions={len(versions)}"

    _check("eCFR Versioner API v1", _probe_ecfr)

    # 3. CourtListener RECAP Atom Feed
    def _probe_recap() -> str:
        raw = fetcher("https://www.courtlistener.com/docket/69623342/feed/")
        root = ET.fromstring(raw)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        if not root.tag.endswith("feed"):
            raise ValueError(f"Unexpected Atom root tag: {root.tag}")
        entries = root.findall("atom:entry", ns)
        if not entries:
            raise ValueError("RECAP Atom feed returned 0 `<entry>` elements")
        return f"atom_entries={len(entries)}"

    _check("CourtListener RECAP Atom", _probe_recap)

    # 4. State Registers & Official State Legislatures (UT RSS, WA WSR, OR OLIS OData)
    def _probe_ut_rules() -> str:
        root = ET.fromstring(fetcher("https://rules.utah.gov/feed/"))
        items = root.findall(".//item")
        if not items:
            raise ValueError("Utah Rules RSS returned 0 `<item>` elements")
        if not items[0].findtext("title") or not items[0].findtext("link"):
            raise ValueError("Utah Rules RSS item missing `title` or `link`")
        return f"rss_items={len(items)}"

    _check("Utah Administrative Rules RSS", _probe_ut_rules)

    def _probe_wa_wsr() -> str:
        html_text = fetcher("https://lawfilesext.leg.wa.gov/law/wsr/agency/HealthCareAuthority.htm").decode(
            "utf-8", errors="replace"
        )
        matches = list(_WA_WSR_ROW_RE.finditer(html_text))
        if not matches:
            raise ValueError("Washington State Register HCA table pattern matched 0 rows")
        return f"wsr_rows={len(matches)}"

    _check("Washington State Register (HCA)", _probe_wa_wsr)

    def _probe_or_olis() -> str:
        data = json.loads(
            fetcher(
                "https://api.oregonlegislature.gov/odata/ODataService.svc/LegislativeSessions?$top=2&$orderby=BeginDate%20desc&$format=json"
            ).decode("utf-8")
        )
        vals = data.get("value")
        if not isinstance(vals, list) or not vals or "SessionKey" not in vals[0]:
            raise ValueError("Oregon OLIS OData response missing `value[0].SessionKey`")
        return f"latest_session={vals[0]['SessionKey']}"

    _check("Oregon OLIS OData API", _probe_or_olis)

    # 5. Policy Journalism RSS
    def _probe_news_rss() -> str:
        root = ET.fromstring(fetcher("https://oregoncapitalchronicle.com/feed/"))
        items = root.findall(".//item")
        if not items:
            raise ValueError("Oregon Capital Chronicle RSS returned 0 `<item>` elements")
        return f"rss_items={len(items)}"

    _check("States Newsroom RSS", _probe_news_rss)

    # 6. Optional keyed channels
    if cl_token:
        def _probe_cl_v4() -> str:
            params = urllib.parse.urlencode(
                [("type", "o"), ("order_by", "dateFiled desc"), ("q", "Medicaid")]
            )
            data = json.loads(
                fetcher(
                    f"https://www.courtlistener.com/api/rest/v4/search/?{params}",
                    headers={"Authorization": f"Token {cl_token}"},
                ).decode("utf-8")
            )
            if not isinstance(data.get("results"), list):
                raise ValueError("CourtListener v4 response missing `results` list")
            return f"results={len(data['results'])}"

        _check("CourtListener REST v4", _probe_cl_v4)
    else:
        results.append({"channel": "CourtListener REST v4", "status": "skipped", "detail": "COURTLISTENER_TOKEN not set"})

    if leg_key:
        def _probe_legiscan() -> str:
            params = urllib.parse.urlencode(
                [("key", leg_key), ("op", "getSearch"), ("state", "OR"), ("year", "3"), ("query", "Medicaid AND action:month")]
            )
            data = json.loads(fetcher(f"https://api.legiscan.com/?{params}").decode("utf-8"))
            if str(data.get("status") or "").upper() != "OK" or not isinstance(data.get("searchresult"), dict):
                raise ValueError(f"LegiScan unexpected payload status={data.get('status')!r}")
            return "status=OK"

        _check("LegiScan API", _probe_legiscan)
    else:
        results.append({"channel": "LegiScan API", "status": "skipped", "detail": "LEGISCAN_API_KEY not set"})

    if tav_key:
        def _probe_tavily() -> str:
            body = json.dumps(
                {
                    "query": "Oregon Health Plan Medicaid",
                    "search_depth": "basic",
                    "topic": "general",
                    "max_results": 2,
                    "include_domains": ["oregon.gov"],
                    "include_domains_mode": "restrict",
                    "include_published_date": True,
                    "filter_by_published_date": False,
                }
            ).encode("utf-8")
            data = json.loads(
                fetcher(
                    "https://api.tavily.com/search",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {tav_key}"},
                    method="POST",
                    body=body,
                ).decode("utf-8")
            )
            if not isinstance(data.get("results"), list):
                raise ValueError("Tavily `/search` response missing `results` list")
            return f"results={len(data['results'])}"

        _check("Tavily Search API", _probe_tavily)
    else:
        results.append({"channel": "Tavily Search API", "status": "skipped", "detail": "TAVILY_API_KEY not set"})

    if gem_key:
        def _probe_gemini() -> str:
            models = discover_gemini_flash_models(gem_key, fetcher)
            if not models:
                raise ValueError("Gemini `/v1beta/models` returned 0 compatible Flash models")
            return f"top_model={models[0]} ({len(models)} total)"

        _check("Gemini Models API", _probe_gemini)
    else:
        results.append({"channel": "Gemini Models API", "status": "skipped", "detail": "GEMINI_API_KEY not set"})

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=str, default=None, help="Optional JSON output file path")
    args = parser.parse_args(argv)

    outcomes = run_live_contract_checks()
    failed = [r for r in outcomes if r["status"] == "error"]
    for row in outcomes:
        tag = row["status"].upper()
        print(f"[{tag:7s}] {row['channel']}: {row['detail']}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"failed_count": len(failed), "results": outcomes}, fh, indent=2)
            fh.write("\n")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
