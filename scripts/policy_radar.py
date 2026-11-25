#!/usr/bin/env python3
"""CivicDesk Matrix Policy Radar (`--desk medicaid` or `--desk id-records`).

Polls primary official APIs (Federal Register, eCFR, CourtListener RECAP dockets)
and policy/news leads (LegiScan, States Newsroom & KFF RSS, optional Tavily + Gemini)
without modifying repository files. Always sends `User-Agent: CivicDesk-Verify/1.0`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "CivicDesk-Verify/1.0"
REQUEST_TIMEOUT = 35
PAUSE_SECONDS = 0.35

RADAR_MARKER_RE = re.compile(r"<!--\s*radar-item:([a-f0-9]{12})\s*-->")
COURTLISTENER_DOCKET_RE = re.compile(r"https?://(?:www\.)?courtlistener\.com/docket/(\d+)(?:/([^/?#]+))?")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
KEY_PARAM_RE = re.compile(r"([?&]key=)[^&\s\"']+", re.IGNORECASE)
TAVILY_KEY_RE = re.compile(r"\btvly-[A-Za-z0-9_-]+")

DESK_CONFIG: dict[str, dict[str, Any]] = {
    "medicaid": {
        "label": "2027 Medicaid Work & Activity Rules",
        "registry": ROOT / "data" / "sources" / "medicaid.yaml",
        "federal_register_terms": (
            '"community engagement" Medicaid',
            '"435.552" OR "435.603" OR "hold harmless" Medicaid',
            'Medicaid ("Oregon Health Plan" OR "Apple Health" OR "Utah Medicaid") ("1115" OR "work")',
            '"public charge" Medicaid',
        ),
        "ecfr_watch": (
            ("42", "435", "42 CFR Part 435 (Medicaid Eligibility & Community Engagement)", ()),
            ("8", "212", "8 CFR Part 212 (Public Charge Inadmissibility)", ("212.20", "212.21", "212.22", "212.23")),
        ),
        "default_dockets": {
            "73546826": "Commonwealth of Massachusetts v. Oz, No. 1:26-cv-12962 (D. Mass.)",
            "74699899": "Neighborhood House v. HHS, No. 2:26-cv-03006 (W.D. Wash.)",
            "74784748": "State of New York v. U.S. Department of Homeland Security, No. 1:26-cv-07978",
            "75165734": "Mahmoud v. Oz, No. 1:26-cv-00881 (D.R.I.)",
        },
        "courtlistener_query": 'Medicaid AND ("community engagement" OR "work requirements" OR "435.552")',
        "rss_feeds": (
            ("KFF Health News (Medicaid)", "https://kffhealthnews.org/topics/medicaid/feed/", True),
            ("KFF Health News (All)", "https://kffhealthnews.org/feed/", False),
            ("Oregon Capital Chronicle", "https://oregoncapitalchronicle.com/feed/", False),
            ("Washington State Standard", "https://washingtonstatestandard.com/feed/", False),
            ("Utah News Dispatch", "https://utahnewsdispatch.com/feed/", False),
            ("Stateline (States Newsroom)", "https://stateline.org/feed/", False),
        ),
        "news_primary_re": re.compile(
            r"\b(medicaid|oregon health plan|ohp|apple health|community engagement|work requirement|work requirements|medically frail|public charge|cms\b|42 c\.?f\.?r)\b",
            re.IGNORECASE,
        ),
        "news_context_re": re.compile(
            r"\b(work|hour|hours|community engagement|exempt|exemption|exclusion|waiver|1115|h\.?r\.?\s*1\b|71119|renew|renewal|eligibility|public charge|injunction|lawsuit|court|rule|rulemaking|oregon|washington|utah|oha\b|odhs\b|hca\b|dshs\b|dhhs\b|dws\b)\b",
            re.IGNORECASE,
        ),
        "legiscan_queries": (
            ("OR", '("Medicaid" OR "Oregon Health Plan" OR "medical assistance" OR "Healthier Oregon") AND ("community engagement" OR "work requirement" OR "work requirements" OR "hold harmless" OR "public charge")'),
            ("WA", '("Apple Health" OR "Medicaid" OR "74.09") AND ("community engagement" OR "work requirement" OR "work requirements" OR "hold harmless" OR "public charge")'),
            ("UT", '("Medicaid" OR "26B-3") AND ("community engagement" OR "work requirement" OR "work requirements" OR "26B-3-142.1" OR "hold harmless")'),
            ("US", 'Medicaid AND ("community engagement" OR "work requirement" OR "work requirements" OR "435.552" OR "hold harmless")'),
        ),
    },
    "id-records": {
        "label": "Legal Name, ID & Vital Records",
        "registry": ROOT / "data" / "sources" / "id_records.yaml",
        "federal_register_terms": (
            '("passport" OR "passports") AND ("sex" OR "gender" OR "14168" OR "DS-11" OR "DS-82" OR "DS-5504")',
            '("Social Security" OR "NUMIDENT") AND ("sex" OR "gender" OR "name change")',
            '("REAL ID" OR "USCIS") AND ("sex" OR "gender" OR "birth certificate")',
        ),
        "ecfr_watch": (
            ("22", "51", "22 CFR Part 51 (Passports)", ()),
            ("20", "422", "20 CFR Part 422 (Social Security Numbers & Records)", ("422.103", "422.107", "422.110")),
            ("6", "37", "6 CFR Part 37 (REAL ID Driver's Licenses and Identification Cards)", ()),
        ),
        "default_dockets": {
            "69623342": "Orr v. Trump, No. 1:25-cv-10313 (D. Mass. passport sex-marker litigation)",
        },
        "courtlistener_query": '("passport" OR "Social Security" OR "birth certificate") AND ("sex marker" OR "gender marker" OR "sex designation")',
        "rss_feeds": (
            ("Oregon Capital Chronicle", "https://oregoncapitalchronicle.com/feed/", False),
            ("Washington State Standard", "https://washingtonstatestandard.com/feed/", False),
            ("Utah News Dispatch", "https://utahnewsdispatch.com/feed/", False),
            ("Stateline (States Newsroom)", "https://stateline.org/feed/", False),
            ("KFF Health News", "https://kffhealthnews.org/feed/", False),
        ),
        "news_primary_re": re.compile(
            r"\b(passport|passports|sex marker|gender marker|sex designation|gender designation|birth certificate|name change|orr v\.?\s*trump|14168|real id|driver'?s? license)\b",
            re.IGNORECASE,
        ),
        "news_context_re": re.compile(
            r"\b(sex marker|gender marker|sex designation|gender designation|nonbinary|transgender|sex assigned at birth|name change|marriage|divorce|birth certificate|passport|orr v\.?\s*trump|14168|vital records|court fee|filing fee)\b",
            re.IGNORECASE,
        ),
        "legiscan_queries": (
            ("OR", '"name change" OR "change of sex" OR "sex designation" OR "gender marker" OR "33.410" OR "33.420" OR "33.460" OR "106.220" OR "107.105" OR "432.235" OR "21.145"'),
            ("WA", '"name change" OR "sex designation" OR "gender designation" OR "gender marker" OR "4.24.130" OR "26.09.150" OR "70.58A" OR "36.18.020" OR "36.18.016"'),
            ("UT", '"name change" OR "sex designation" OR "gender marker" OR "42-1-1" OR "26B-8-111" OR "68-3-12.5" OR "53-3-205" OR "78A-2-301"'),
            ("US", '("passport" OR "Social Security" OR "REAL ID" OR "birth certificate") AND ("sex marker" OR "gender marker" OR "sex designation" OR "name change")'),
        ),
    },
}


def redact_secrets(text: str, secrets: tuple[str, ...] = ()) -> str:
    cleaned = KEY_PARAM_RE.sub(r"\1[REDACTED]", text)
    cleaned = TAVILY_KEY_RE.sub("[REDACTED]", cleaned)
    for secret in secrets:
        tok = secret.strip()
        if tok and len(tok) >= 4:
            cleaned = cleaned.replace(tok, "[REDACTED]")
    return cleaned


def strip_html(raw: str | None, max_len: int = 240) -> str:
    if not raw:
        return ""
    text = html.unescape(TAG_RE.sub(" ", raw))
    text = WS_RE.sub(" ", text).strip()
    if len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text


def safe_url(url: str | None) -> str:
    if not url:
        return ""
    cleaned = url.strip()
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return cleaned


def item_fingerprint(desk: str, source_kind: str, unique_key: str) -> str:
    digest = hashlib.sha256(f"{desk}:{source_kind}:{unique_key.strip()}".encode("utf-8")).hexdigest()
    return digest[:12]


def parse_any_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        try:
            return dt.date.fromisoformat(raw[:10])
        except ValueError:
            pass
    try:
        parsed_dt = email.utils.parsedate_to_datetime(raw)
        if parsed_dt.tzinfo is not None:
            parsed_dt = parsed_dt.astimezone(dt.timezone.utc)
        return parsed_dt.date()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def default_fetcher(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes | None = None,
) -> bytes:
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read()


def load_seen_markers(seen_issues_path: Path | None) -> set[str]:
    if not seen_issues_path or not seen_issues_path.is_file():
        return set()
    try:
        issues = json.loads(seen_issues_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    seen: set[str] = set()
    for issue in issues if isinstance(issues, list) else []:
        seen.update(RADAR_MARKER_RE.findall(str(issue.get("body") or "")))
        for c in issue.get("comments") or []:
            seen.update(RADAR_MARKER_RE.findall(str(c.get("body") or "")))
    return seen


def discover_dockets(desk: str) -> dict[str, str]:
    cfg = DESK_CONFIG[desk]
    dockets = dict(cfg["default_dockets"])
    reg_path: Path = cfg["registry"]
    if reg_path.is_file():
        entries = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or []
        for s in entries:
            m = COURTLISTENER_DOCKET_RE.search(str(s.get("url") or ""))
            if m and m.group(1) not in dockets:
                dockets[m.group(1)] = str(s.get("short") or s.get("title") or f"Docket {m.group(1)}")
    return dockets


def poll_federal_register(desk: str, since: dt.date, fetcher: Callable[..., bytes]) -> list[dict[str, Any]]:
    cfg = DESK_CONFIG[desk]
    items: list[dict[str, Any]] = []
    seen_docs: set[str] = set()
    for term in cfg["federal_register_terms"]:
        params = urllib.parse.urlencode({
            "conditions[term]": term,
            "conditions[publication_date][gte]": since.isoformat(),
            "per_page": "20",
            "order": "newest",
        })
        url = f"https://www.federalregister.gov/api/v1/documents.json?{params}"
        data = json.loads(fetcher(url).decode("utf-8"))
        for doc in data.get("results") or []:
            doc_num = str(doc.get("document_number") or "")
            if not doc_num or doc_num in seen_docs:
                continue
            seen_docs.add(doc_num)
            pub_date = str(doc.get("publication_date") or since.isoformat())
            html_url = safe_url(doc.get("html_url"))
            if not html_url:
                continue
            items.append({
                "id": item_fingerprint(desk, "fedreg", doc_num),
                "tier": "primary",
                "source": "Federal Register",
                "date": pub_date,
                "title": strip_html(doc.get("title"), 200),
                "url": html_url,
                "summary": strip_html(doc.get("abstract"), 240),
            })
    return items


def poll_ecfr(desk: str, since: dt.date, fetcher: Callable[..., bytes]) -> list[dict[str, Any]]:
    cfg = DESK_CONFIG[desk]
    items: list[dict[str, Any]] = []
    for title_num, part_num, label, prefixes in cfg["ecfr_watch"]:
        url = f"https://www.ecfr.gov/api/versioner/v1/versions/title-{title_num}.json?part={part_num}"
        data = json.loads(fetcher(url).decode("utf-8"))
        for v in data.get("content_versions") or []:
            amend_date = parse_any_date(v.get("amendment_date") or v.get("issue_date"))
            if not amend_date or amend_date < since:
                continue
            ident = str(v.get("identifier") or "")
            if prefixes and not any(ident.startswith(p) for p in prefixes):
                continue
            key = f"{title_num}:{part_num}:{ident}:{amend_date.isoformat()}"
            items.append({
                "id": item_fingerprint(desk, "ecfr", key),
                "tier": "primary",
                "source": f"eCFR ({label})",
                "date": amend_date.isoformat(),
                "title": f"{label} section {ident or part_num} amended",
                "url": f"https://www.ecfr.gov/current/title-{title_num}/part-{part_num}",
                "summary": strip_html(v.get("name"), 200),
            })
    return items


def poll_courtlistener_feeds(desk: str, since: dt.date, fetcher: Callable[..., bytes]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for docket_id, case_label in discover_dockets(desk).items():
        feed_url = f"https://www.courtlistener.com/docket/{docket_id}/feed/"
        raw_xml = fetcher(feed_url)
        root = ET.fromstring(raw_xml)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            updated_el = entry.find("atom:updated", ns) or entry.find("atom:published", ns)
            entry_date = parse_any_date(updated_el.text if updated_el is not None else None)
            if not entry_date or entry_date < since:
                continue
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            summary_el = entry.find("atom:summary", ns)
            href = safe_url(link_el.attrib.get("href") if link_el is not None else "")
            if not href:
                continue
            items.append({
                "id": item_fingerprint(desk, "recap", href),
                "tier": "primary",
                "source": f"CourtListener ({case_label})",
                "date": entry_date.isoformat(),
                "title": strip_html(title_el.text if title_el is not None else "Docket entry", 200),
                "url": href,
                "summary": strip_html(summary_el.text if summary_el is not None else "", 240),
            })
    return items


def poll_rss(desk: str, since: dt.date, fetcher: Callable[..., bytes]) -> list[dict[str, Any]]:
    cfg = DESK_CONFIG[desk]
    items: list[dict[str, Any]] = []
    for feed_name, feed_url, prefiltered in cfg["rss_feeds"]:
        raw_xml = fetcher(feed_url)
        root = ET.fromstring(raw_xml)
        for item in root.findall(".//item"):
            pub = parse_any_date(item.findtext("pubDate"))
            if not pub or pub < since:
                continue
            title = strip_html(item.findtext("title"), 200)
            desc = strip_html(item.findtext("description"), 240)
            link = safe_url(item.findtext("link"))
            if not link:
                continue
            combined = f"{title} {desc}"
            if not cfg["news_primary_re"].search(combined):
                continue
            if not prefiltered and not cfg["news_context_re"].search(combined):
                continue
            items.append({
                "id": item_fingerprint(desk, "rss", link),
                "tier": "lead",
                "source": f"{feed_name} (Lead - verify in primary .gov source)",
                "date": pub.isoformat(),
                "title": title,
                "url": link,
                "summary": desc,
            })
    return items


def poll_legiscan(desk: str, since: dt.date, api_key: str, fetcher: Callable[..., bytes]) -> list[dict[str, Any]]:
    cfg = DESK_CONFIG[desk]
    items: list[dict[str, Any]] = []
    seen_bills: set[str] = set()
    for state_code, query in cfg["legiscan_queries"]:
        params = urllib.parse.urlencode({
            "key": api_key,
            "op": "getSearch",
            "state": state_code,
            "query": query,
            "year": "3",
        })
        url = f"https://api.legiscan.com/?{params}"
        data = json.loads(fetcher(url).decode("utf-8"))
        sr = data.get("searchresult") or {}
        for k, row in sr.items():
            if k == "summary" or not isinstance(row, dict):
                continue
            if int(row.get("relevance") or 0) < 50:
                continue
            action_date = parse_any_date(str(row.get("last_action_date") or ""))
            if not action_date or action_date < since:
                continue
            bill_id = str(row.get("bill_id") or "")
            if not bill_id or bill_id in seen_bills:
                continue
            seen_bills.add(bill_id)
            bill_url = safe_url(row.get("url"))
            if not bill_url:
                continue
            items.append({
                "id": item_fingerprint(desk, "legiscan", bill_id),
                "tier": "lead",
                "source": f"LegiScan ({state_code} - verify in state legislature)",
                "date": action_date.isoformat(),
                "title": f"{state_code} {row.get('bill_number', '')}: {strip_html(row.get('title'), 180)}",
                "url": bill_url,
                "summary": strip_html(row.get("last_action"), 220),
            })
    return items


def run_sweep(
    desk: str,
    *,
    days: int = 7,
    today: dt.date | None = None,
    seen_markers: set[str] | None = None,
    fetcher: Callable[..., bytes] = default_fetcher,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    if desk not in DESK_CONFIG:
        raise ValueError(f"Unknown desk: {desk}")
    now_date = today or dt.datetime.now(dt.timezone.utc).date()
    since = now_date - dt.timedelta(days=max(1, min(days, 60)))
    seen = seen_markers or set()
    cfg_env = env if env is not None else dict(os.environ)

    legiscan_key = (cfg_env.get("LEGISCAN_API_KEY") or "").strip()
    cl_token = (cfg_env.get("COURTLISTENER_TOKEN") or "").strip()
    gemini_key = (cfg_env.get("GEMINI_API_KEY") or "").strip()
    secrets = (legiscan_key, cl_token, gemini_key)

    statuses: list[dict[str, str]] = []
    collected: list[dict[str, Any]] = []

    polls: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = [
        ("Federal Register API", lambda: poll_federal_register(desk, since, fetcher)),
        ("eCFR Versioner API", lambda: poll_ecfr(desk, since, fetcher)),
        ("CourtListener RECAP Feeds", lambda: poll_courtlistener_feeds(desk, since, fetcher)),
        ("Policy & State News RSS", lambda: poll_rss(desk, since, fetcher)),
    ]
    if legiscan_key:
        polls.append(("LegiScan API", lambda: poll_legiscan(desk, since, legiscan_key, fetcher)))
    else:
        statuses.append({"source": "LegiScan API", "status": "skipped (LEGISCAN_API_KEY not set)"})

    for name, fn in polls:
        try:
            found = fn()
            collected.extend(found)
            statuses.append({"source": name, "status": f"ok ({len(found)} match(es))"})
        except Exception as exc:
            err_msg = redact_secrets(str(exc), secrets)
            statuses.append({"source": name, "status": f"error: {err_msg}"})

    unseen = [item for item in collected if item["id"] not in seen]
    deduped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in unseen:
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            deduped.append(item)

    return {
        "desk": desk,
        "desk_label": DESK_CONFIG[desk]["label"],
        "since": since.isoformat(),
        "today": now_date.isoformat(),
        "total_matched": len(collected),
        "new_count": len(deduped),
        "items": deduped,
        "statuses": statuses,
    }


def render_markdown_report(result: dict[str, Any]) -> str:
    lines = [
        f"## CivicDesk Policy Radar — `{result['desk']}` ({result['desk_label']})",
        "",
        f"Window: `{result['since']}` to `{result['today']}` · Unseen items: **{result['new_count']}**",
        "",
    ]
    primary = [i for i in result["items"] if i["tier"] == "primary"]
    leads = [i for i in result["items"] if i["tier"] != "primary"]

    if primary:
        lines.append("### Primary official records (`official`)")
        lines.append("")
        for i in primary:
            lines.append(
                f"- **{i['date']}** · [{i['title']}]({i['url']}) — *{i['source']}* "
                f"{'— ' + i['summary'] if i.get('summary') else ''} <!-- radar-item:{i['id']} -->"
            )
        lines.append("")

    if leads:
        lines.append("### Policy & news leads (`not_official` — verify in primary `.gov` or court source before citing)")
        lines.append("")
        for i in leads:
            lines.append(
                f"- **{i['date']}** · [{i['title']}]({i['url']}) — *{i['source']}* "
                f"{'— ' + i['summary'] if i.get('summary') else ''} <!-- radar-item:{i['id']} -->"
            )
        lines.append("")

    if not primary and not leads:
        lines.append("No new unseen items matched during this lookback window.")
        lines.append("")

    lines.append("### Feed statuses")
    lines.append("")
    for st in result["statuses"]:
        lines.append(f"- `{st['source']}`: {st['status']}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desk", choices=["medicaid", "id-records"], required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--seen-issues", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--results", type=Path, default=None)
    args = parser.parse_args()

    seen = load_seen_markers(args.seen_issues)
    res = run_sweep(args.desk, days=args.days, seen_markers=seen)
    md = render_markdown_report(res)

    if args.report:
        args.report.write_text(md, encoding="utf-8")
    else:
        print(md)
    if args.results:
        args.results.write_text(json.dumps(res, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
