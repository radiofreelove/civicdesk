#!/usr/bin/env python3
"""Weekly live API contract verifier for CivicDesk Policy Wire channels.

Discovers probe targets dynamically from active desk profiles in `data/wire/*.yaml`
and performs one lightweight live request per channel kind (`User-Agent: CivicDesk-Verify/1.0`),
validating upstream JSON/XML/HTML response shapes so breaking upstream API changes
surface immediately instead of silently returning zero dispatches.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

try:
    from http_retry import fetch_with_retry
    from policy_radar import (
        FEDERAL_REGISTER_FIELDS,
        WIRE_DIR,
        _WA_WSR_ROW_RE,
        discover_gemini_flash_models,
        load_wire_profiles,
        redact_secrets,
    )
except ImportError:  # pragma: no cover - package-qualified import fallback
    from scripts.http_retry import fetch_with_retry  # type: ignore[no-redef]
    from scripts.policy_radar import (  # type: ignore[no-redef]
        FEDERAL_REGISTER_FIELDS,
        WIRE_DIR,
        _WA_WSR_ROW_RE,
        discover_gemini_flash_models,
        load_wire_profiles,
        redact_secrets,
    )


def _default_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes | None = None,
) -> bytes:
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


def _discover_probe_targets(profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Derive one lightweight probe target per channel kind from active desk profiles."""
    ecfr_targets: list[tuple[str, str]] = []
    docket_ids: list[str] = []
    state_regs_by_kind: dict[str, dict[str, Any]] = {}
    rss_feeds: list[tuple[str, str]] = []
    cl_v4_queries: list[dict[str, str]] = []
    leg_states: list[tuple[str, str]] = []
    tavily_probe: tuple[str, str] | None = None

    for cfg in profiles.values():
        for entry in cfg.get("ecfr_watch") or ():
            pair = (str(entry[0]), str(entry[1]))
            if pair not in ecfr_targets:
                ecfr_targets.append(pair)
        for did in (cfg.get("default_dockets") or {}).keys():
            if did not in docket_ids:
                docket_ids.append(str(did))
        for sr in cfg.get("state_registers") or ():
            kind = str(sr.get("kind") or "")
            if kind and kind not in state_regs_by_kind:
                state_regs_by_kind[kind] = sr
        for fname, furl, _prefiltered in cfg.get("rss_feeds") or ():
            if not any(u == furl for _, u in rss_feeds):
                rss_feeds.append((str(fname), str(furl)))
        for q in cfg.get("courtlistener_v4_queries") or ():
            cl_v4_queries.append(q)
        for st_code, _query in cfg.get("legiscan_queries") or ():
            st_upper = str(st_code).strip().upper()
            if st_upper and st_upper != "US" and not any(s == st_upper for s, _ in leg_states):
                jur_meta = (cfg.get("jurisdictions") or {}).get(st_upper) or {}
                kws = [str(k).strip() for k in (jur_meta.get("keywords") or ()) if str(k).strip()]
                term = kws[0] if kws else cfg.get("label", "policy").split()[0]
                leg_states.append((st_upper, term))
        if tavily_probe is None:
            domains = list(cfg.get("tavily_registry_domains") or cfg.get("tavily_domains") or ())
            if domains:
                tavily_probe = (str(domains[0]), str(cfg.get("label") or cfg["desk"]))

    # Prefer a second distinct profile's CFR title for eCFR when available so both desks get covered.
    fedreg_cfr = ecfr_targets[0] if ecfr_targets else ("42", "435")
    ecfr_cfr = ecfr_targets[-1] if len(ecfr_targets) > 1 else fedreg_cfr
    for cfg in list(profiles.values())[1:]:
        watches = cfg.get("ecfr_watch") or ()
        if watches:
            ecfr_cfr = (str(watches[0][0]), str(watches[0][1]))
            break

    cl_v4_choice = next((q for q in cl_v4_queries if q.get("search_type") == "o"), None) or (
        cl_v4_queries[0] if cl_v4_queries else {"search_type": "o", "query": "Medicaid"}
    )

    return {
        "fedreg_cfr": fedreg_cfr,
        "ecfr_cfr": ecfr_cfr,
        "recap_docket": docket_ids[0] if docket_ids else "69623342",
        "state_regs_by_kind": state_regs_by_kind,
        "rss_feed": rss_feeds[0] if rss_feeds else ("Oregon Capital Chronicle", "https://oregoncapitalchronicle.com/feed/"),
        "cl_v4": cl_v4_choice,
        "legiscan": leg_states[0] if leg_states else ("OR", "Medicaid"),
        "tavily": tavily_probe or ("oregon.gov", "Oregon public policy"),
    }


def run_live_contract_checks(
    *,
    fetcher: Callable[..., bytes] = _default_get,
    env: dict[str, str] | None = None,
    wire_dir: Path = WIRE_DIR,
) -> list[dict[str, str]]:
    """Run live contract probes across keyless and configured keyed channels."""
    cfg_env = env if env is not None else dict(os.environ)
    cl_token = (cfg_env.get("COURTLISTENER_TOKEN") or "").strip()
    leg_key = (cfg_env.get("LEGISCAN_API_KEY") or "").strip()
    tav_key = (cfg_env.get("TAVILY_API_KEY") or "").strip()
    gem_key = (cfg_env.get("GEMINI_API_KEY") or "").strip()
    secrets = (cl_token, leg_key, tav_key, gem_key)

    profiles = load_wire_profiles(wire_dir, include_disabled=False)
    targets = _discover_probe_targets(profiles)
    results: list[dict[str, str]] = []

    def _check(name: str, fn: Callable[[], str]) -> None:
        try:
            detail = fn()
            results.append({"channel": name, "status": "ok", "detail": redact_secrets(detail, secrets)})
        except Exception as exc:  # noqa: BLE001
            err = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
            results.append({"channel": name, "status": "error", "detail": err})

    # 1. Federal Register API v1 (documents + CFR filter + public inspection)
    fr_title, fr_part = targets["fedreg_cfr"]

    def _probe_fedreg() -> str:
        params = urllib.parse.urlencode(
            [
                ("conditions[cfr][title]", fr_title),
                ("conditions[cfr][part]", fr_part),
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
        return f"cfr={fr_title}/{fr_part}, documents={len(docs)}, public_inspection_count={pi['count']}"

    _check("Federal Register API v1", _probe_fedreg)

    # 2. eCFR Versioner API v1
    ecfr_title, ecfr_part = targets["ecfr_cfr"]

    def _probe_ecfr() -> str:
        data = json.loads(
            fetcher(
                f"https://www.ecfr.gov/api/versioner/v1/versions/title-{ecfr_title}.json?part={ecfr_part}"
            ).decode("utf-8")
        )
        versions = data.get("content_versions")
        if not isinstance(versions, list) or not versions:
            raise ValueError("eCFR response missing non-empty `content_versions` list")
        v0 = versions[0]
        for req in ("identifier", "issue_date", "amendment_date", "name"):
            if req not in v0:
                raise ValueError(f"eCFR content_version missing `{req}`")
        return f"cfr={ecfr_title}/{ecfr_part}, content_versions={len(versions)}"

    _check("eCFR Versioner API v1", _probe_ecfr)

    # 3. CourtListener RECAP Atom Feed
    recap_docket = targets["recap_docket"]

    def _probe_recap() -> str:
        raw = fetcher(f"https://www.courtlistener.com/docket/{recap_docket}/feed/")
        root = ET.fromstring(raw)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        if not root.tag.endswith("feed"):
            raise ValueError(f"Unexpected Atom root tag: {root.tag}")
        entries = root.findall("atom:entry", ns)
        if not entries:
            raise ValueError("RECAP Atom feed returned 0 `<entry>` elements")
        return f"docket={recap_docket}, atom_entries={len(entries)}"

    _check("CourtListener RECAP Atom", _probe_recap)

    # 4. State Registers & Official State Legislatures discovered from active desk profiles
    for kind, spec in targets["state_regs_by_kind"].items():
        sr_name = str(spec["name"])
        sr_url = str(spec["url"])

        if kind in {"ut_rss", "rss", "atom"}:
            def _probe_state_rss(url: str = sr_url, label: str = sr_name) -> str:
                root = ET.fromstring(fetcher(url))
                items = root.findall(".//item")
                if items:
                    if not items[0].findtext("title") or not items[0].findtext("link"):
                        raise ValueError(f"{label} RSS item missing `title` or `link`")
                    return f"rss_items={len(items)}"
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                entries = root.findall("atom:entry", ns)
                if not entries:
                    raise ValueError(f"{label} returned 0 RSS `<item>` or Atom `<entry>` elements")
                return f"atom_entries={len(entries)}"

            _check(sr_name, _probe_state_rss)

        elif kind == "wa_wsr":
            def _probe_wa_wsr(url: str = sr_url, label: str = sr_name) -> str:
                html_text = fetcher(url).decode("utf-8", errors="replace")
                matches = list(_WA_WSR_ROW_RE.finditer(html_text))
                if not matches:
                    raise ValueError(f"{label} WSR table pattern matched 0 rows")
                return f"wsr_rows={len(matches)}"

            _check(sr_name, _probe_wa_wsr)

        elif kind == "or_olis_odata":
            base_service = sr_url.rstrip("/").rsplit("/", 1)[0]
            sessions_url = f"{base_service}/LegislativeSessions?$top=2&$orderby=BeginDate%20desc&$format=json"

            def _probe_or_olis(url: str = sessions_url) -> str:
                data = json.loads(fetcher(url).decode("utf-8"))
                vals = data.get("value")
                if not isinstance(vals, list) or not vals or "SessionKey" not in vals[0]:
                    raise ValueError("Oregon OLIS OData response missing `value[0].SessionKey`")
                return f"latest_session={vals[0]['SessionKey']}"

            _check("Oregon OLIS OData API", _probe_or_olis)

    # 5. Policy Journalism RSS
    rss_name, rss_url = targets["rss_feed"]

    def _probe_news_rss() -> str:
        root = ET.fromstring(fetcher(rss_url))
        items = root.findall(".//item")
        if not items:
            raise ValueError(f"{rss_name} RSS returned 0 `<item>` elements")
        return f"feed={rss_name}, rss_items={len(items)}"

    _check(f"Policy News RSS ({rss_name})", _probe_news_rss)

    # 6. Optional keyed channels
    if cl_token:
        cl_type = str(targets["cl_v4"].get("search_type") or "o")
        cl_query = str(targets["cl_v4"].get("query") or "Medicaid")

        def _probe_cl_v4() -> str:
            params = urllib.parse.urlencode(
                [("type", cl_type), ("order_by", "dateFiled desc"), ("q", cl_query)]
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
        leg_state, leg_term = targets["legiscan"]

        def _probe_legiscan() -> str:
            params = urllib.parse.urlencode(
                [("key", leg_key), ("op", "getSearch"), ("state", leg_state), ("year", "3"), ("query", f"{leg_term} AND action:month")]
            )
            data = json.loads(fetcher(f"https://api.legiscan.com/?{params}").decode("utf-8"))
            if str(data.get("status") or "").upper() != "OK" or not isinstance(data.get("searchresult"), dict):
                raise ValueError(f"LegiScan unexpected payload status={data.get('status')!r}")
            return f"state={leg_state}, status=OK"

        _check("LegiScan API", _probe_legiscan)
    else:
        results.append({"channel": "LegiScan API", "status": "skipped", "detail": "LEGISCAN_API_KEY not set"})

    if tav_key:
        tav_domain, tav_query = targets["tavily"]

        def _probe_tavily() -> str:
            body = json.dumps(
                {
                    "query": tav_query,
                    "search_depth": "basic",
                    "topic": "general",
                    "max_results": 2,
                    "include_domains": [tav_domain],
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
            return f"domain={tav_domain}, results={len(data['results'])}"

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
    parser.add_argument("--wire-dir", type=Path, default=WIRE_DIR, help="Directory containing desk YAML beat profiles")
    args = parser.parse_args(argv)

    outcomes = run_live_contract_checks(wire_dir=args.wire_dir)
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
