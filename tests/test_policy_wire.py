from __future__ import annotations

import ast
import datetime as dt
import email.message
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import zlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def load_script(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / "scripts" / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_wire_module():
    return load_script("policy_radar", "policy_radar.py")


def _make_minimal_flate_pdf(text_line: str) -> bytes:
    stream_plain = f"BT /F1 12 Tf 72 720 Td ({text_line}) Tj ET".encode("latin-1")
    compressed = zlib.compress(stream_plain)
    return (
        b"%PDF-1.4\n1 0 obj<< /Length "
        + str(len(compressed)).encode("ascii")
        + b" /Filter /FlateDecode >>\nstream\n"
        + compressed
        + b"\nendstream\nendobj\n%%EOF\n"
    )


def test_wire_profiles_and_dynamic_desk_extensibility(tmp_path: Path) -> None:
    wire = load_wire_module()

    site_yaml = yaml.safe_load((ROOT / "data" / "site.yaml").read_text(encoding="utf-8"))
    site_active = [d["slug"] for d in site_yaml["desks"] if d.get("status") == "active"]
    site_upcoming = [d["slug"] for d in site_yaml["desks"] if d.get("status") == "upcoming"]
    active = wire.list_active_desks(include_disabled=False)
    all_desks = wire.list_active_desks(include_disabled=True)
    assert set(active) == set(site_active)
    for slug in site_upcoming:
        assert slug in all_desks

    custom_dir = tmp_path / "wire"
    custom_dir.mkdir()
    reg_file = tmp_path / "housing_sources.yaml"
    reg_file.write_text(
        yaml.safe_dump(
            [
                {
                    "id": "HSG-OR-01",
                    "desk": "housing",
                    "jurisdiction": "OR",
                    "title": "ORS 90.324 Eviction Notice Timelines",
                    "url": "https://www.oregonlegislature.gov/bills_laws/ors/ors090.html",
                    "checked": "2026-11-21",
                    "status": "official",
                }
            ]
        ),
        encoding="utf-8",
    )
    (custom_dir / "housing.yaml").write_text(
        yaml.safe_dump(
            {
                "desk": "housing",
                "enabled": True,
                "label": "Eviction & Tenant Protections",
                "registry": str(reg_file),
                "source_prefix": "HSG-",
                "route_prefix": "/housing/",
                "editorial_brief": "Oregon and Washington tenant notice and eviction rules.",
                "jurisdictions": {
                    "OR": {
                        "name": "Oregon",
                        "domains": ["oregon.gov", "oregonlegislature.gov"],
                        "keywords": ["ORS 90", "Oregon eviction"],
                    }
                },
                "channels": {
                    "federal_register": {"terms": ['"eviction" HUD'], "public_inspection": False},
                    "ecfr": [],
                    "courtlistener": {"watched_dockets": {}, "v4_queries": []},
                    "legiscan": {"queries": [{"state": "OR", "query": '"eviction" OR "90.324"'}]},
                    "rss": {
                        "primary_pattern": r"\b(eviction|tenant)\b",
                        "context_pattern": r"\b(oregon|notice|rent)\b",
                        "feeds": [{"name": "OR Feed", "url": "https://oregoncapitalchronicle.com/feed/"}],
                    },
                    "tavily": {"queries": ['"ORS 90" eviction notice form']},
                },
            }
        ),
        encoding="utf-8",
    )

    discovered = wire.list_active_desks(custom_dir, include_disabled=False)
    assert discovered == ["housing"]
    profiles = wire.load_wire_profiles(custom_dir, include_disabled=False)

    def custom_fetcher(url: str, **_kwargs) -> bytes:
        if "federalregister.gov" in url:
            return json.dumps({"results": []}).encode("utf-8")
        if "oregonlegislature.gov" in url:
            return b"<html><body>ORS 90.324 eviction notice text</body></html>"
        return (
            b'<?xml version="1.0" encoding="UTF-8"?><rss><channel><item>'
            b"<title>Oregon updates tenant eviction notice rules for 2027</title>"
            b"<link>https://oregoncapitalchronicle.com/2026/11/24/eviction-notice/</link>"
            b"<pubDate>Tue, 24 Nov 2026 12:00:00 +0000</pubDate>"
            b"<description>Updated rent notice forms.</description>"
            b"</item></channel></rss>"
        )

    res = wire.run_sweep(
        "housing",
        days=7,
        today=dt.date(2026, 11, 25),
        fetcher=custom_fetcher,
        env={},
        profiles=profiles,
    )
    assert res["new_count"] == 1
    assert res["items"][0]["jurisdictions"] == ["OR"]


def test_all_api_channels_urgency_calibration_and_citation_impact_mapping() -> None:
    wire = load_wire_module()
    captured: list[tuple[str, dict[str, str], bytes | None]] = []
    tavily_payloads: list[dict] = []

    def full_wire_fetcher(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        body: bytes | None = None,
    ) -> bytes:
        hdrs = dict(headers or {})
        captured.append((url, hdrs, body))

        if "federalregister.gov/api/v1/public-inspection-documents.json" in url:
            return json.dumps({"count": 0, "results": []}).encode("utf-8")

        if "federalregister.gov/api/v1/documents.json" in url:
            assert "per_page=100" in url
            assert "effective_on" in url and "comments_close_on" in url
            if "page=2" in url:
                return json.dumps(
                    {
                        "results": [
                            {
                                "document_number": "2026-88003",
                                "title": "Passports; Final Rule on DS-11 Documentary Proof",
                                "type": "Rule",
                                "publication_date": "2026-11-24",
                                "effective_on": "2026-12-15",
                                "comments_close_on": None,
                                "html_url": "https://www.federalregister.gov/d/2026-88003",
                                "abstract": "Final rule amending 22 CFR Part 51.",
                                "agencies": [{"name": "State Department"}],
                            }
                        ]
                    }
                ).encode("utf-8")
            return json.dumps(
                {
                    "next_page_url": "https://www.federalregister.gov/api/v1/documents.json?page=2&per_page=100&effective_on=1&comments_close_on=1",
                    "results": [
                        {
                            "document_number": "2026-88001",
                            "title": "Passports; Sex Marker and DS-82 Documentary Requirements",
                            "type": "Notice",
                            "publication_date": "2026-11-23",
                            "effective_on": None,
                            "comments_close_on": "2027-01-15",
                            "html_url": "https://www.federalregister.gov/d/2026-88001",
                            "abstract": "State Department notice revising DS-11 and DS-82 instructions.",
                            "agencies": [{"name": "State Department"}],
                        },
                        {
                            "document_number": "2026-88002",
                            "title": "Commercial Salmon Quotas Off Pacific Coast",
                            "type": "Rule",
                            "publication_date": "2026-11-23",
                            "html_url": "https://www.federalregister.gov/d/2026-88002",
                            "abstract": "Fishery management.",
                            "agencies": [{"name": "NOAA"}],
                        },
                    ],
                }
            ).encode("utf-8")

        if "ecfr.gov" in url:
            if "title-22" in url:
                return json.dumps(
                    {
                        "content_versions": [
                            {
                                "identifier": "51.20",
                                "name": "22 CFR 51.20 General passport application rules",
                                # Newly added issue_date inside window, carrying an older amendment_date!
                                "issue_date": "2026-11-22",
                                "amendment_date": "2026-10-01",
                            }
                        ]
                    }
                ).encode("utf-8")
            return json.dumps({"content_versions": []}).encode("utf-8")

        if "rules.utah.gov/feed/" in url:
            return (
                b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
                b"<item><title>Utah Vital Records R436 Birth Certificate Amendment Notice</title>"
                b"<link>https://rules.utah.gov/2026/11/r436-vital-records-notice/</link>"
                b"<pubDate>Mon, 23 Nov 2026 16:00:00 +0000</pubDate>"
                b"<description>Proposed amendment to R436 vital records fee rules.</description></item>"
                b"</channel></rss>"
            )

        if "lawfilesext.leg.wa.gov/law/wsr/agency/" in url:
            return b"<html><body><table id='agency'></table></body></html>"

        if "api.oregonlegislature.gov" in url:
            return json.dumps({"value": []}).encode("utf-8")

        if "courtlistener.com/docket/69623342/feed/" in url:
            return (
                b'<?xml version="1.0" encoding="utf-8"?>'
                b'<feed xmlns="http://www.w3.org/2005/Atom">'
                b"<entry>"
                b"<title>Entry #215 in Orr v. Trump</title>"
                b'<link href="https://www.courtlistener.com/docket/69623342/orr-v-trump/?order_by=desc#entry-215" rel="alternate"/>'
                b"<published>2026-11-01T10:00:00-05:00</published>"
                b"<updated>2026-11-24T10:00:00-05:00</updated>"
                b"<summary>Order clarifying class compliance schedule.</summary>"
                b"</entry>"
                b"<entry>"
                b"<title>Entry #216 in Orr v. Trump</title>"
                b'<link href="https://www.courtlistener.com/docket/69623342/orr-v-trump/?order_by=desc#entry-216" rel="alternate"/>'
                b"<published>2026-11-24T11:00:00-05:00</published>"
                b"<summary>Joint status report on DS-82 processing.</summary>"
                b"</entry>"
                b"<entry>"
                b"<title>Entry #217 in Orr v. Trump</title>"
                b'<link href="https://www.courtlistener.com/docket/69623342/orr-v-trump/?order_by=desc#entry-217" rel="alternate"/>'
                b"<published>2026-11-24T12:00:00-05:00</published>"
                b"<summary>MOTION for Counsel to Appear Pro Hac Vice.</summary>"
                b"</entry>"
                b"</feed>"
            )

        if "api/rest/v4/search/" in url:
            assert hdrs.get("Authorization") == "Token cl-secret-token-999"
            assert "order_by=dateFiled+desc" in url
            if "type=o" in url:
                return json.dumps(
                    {
                        "results": [
                            {
                                "caseName": "Orr v. Trump",
                                "court": "1st Cir.",
                                "dateFiled": "2026-11-24",
                                "absolute_url": "/opinion/991122/orr-v-trump/",
                                "opinions": [{"snippet": "Appellate order affirming preliminary injunction scope."}],
                            }
                        ]
                    }
                ).encode("utf-8")
            return json.dumps(
                {
                    "results": [
                        {
                            "caseName": "Doe v. Social Security Administration",
                            "court": "D. Or.",
                            "dateFiled": "2026-11-23",
                            "docket_id": 88881111,
                            "docketNumber": "3:26-cv-01999",
                        }
                    ]
                }
            ).encode("utf-8")

        if "api.legiscan.com" in url:
            if "op=getBill" in url and "id=7001" in url:
                return json.dumps(
                    {
                        "status": "OK",
                        "bill": {
                            "bill_id": 7001,
                            "bill_number": "HB 2050",
                            "state": "OR",
                            "change_hash": "hash-hb2050-v2",
                            "state_link": "https://olis.oregonlegislature.gov/liz/2027R1/Measures/Overview/HB2050",
                            "url": "https://legiscan.com/OR/bill/HB2050/2027",
                            "calendar": [
                                {
                                    "type": "Hearing",
                                    "date": "2026-12-02",
                                    "time": "08:00",
                                    "location": "HR A",
                                    "description": "House Judiciary Public Hearing",
                                }
                            ],
                        },
                    }
                ).encode("utf-8")
            assert "year=3" in url and "action%3A20261118..20261125" in url
            if "state=OR" in url:
                return json.dumps(
                    {
                        "status": "OK",
                        "searchresult": {
                            "summary": {"count": 2},
                            "0": {
                                "relevance": 95,
                                "bill_id": 7001,
                                "bill_number": "HB 2050",
                                "title": "Relating to circuit court filing fees under ORS 21.145 and ORS 33.460",
                                "last_action_date": "2026-11-23",
                                "last_action": "Referred to House Judiciary",
                                "url": "https://legiscan.com/OR/bill/HB2050/2027",
                            },
                            "1": {
                                "relevance": 25,
                                "bill_id": 7002,
                                "bill_number": "HB 2099",
                                "title": "Unrelated low relevance bill",
                                "last_action_date": "2026-11-23",
                                "last_action": "Introduced",
                                "url": "https://legiscan.com/OR/bill/HB2099/2027",
                            },
                        },
                    }
                ).encode("utf-8")
            return json.dumps({"status": "OK", "searchresult": {"summary": {"count": 0}}}).encode("utf-8")

        if url == "https://api.tavily.com/search":
            assert hdrs.get("Authorization") == "Bearer tvly-secret-test-key"
            req_data = json.loads((body or b"{}").decode("utf-8"))
            tavily_payloads.append(req_data)
            assert req_data["search_depth"] == "basic"
            assert req_data["include_domains_mode"] == "restrict"
            assert req_data["include_published_date"] is True
            return json.dumps(
                {
                    "results": [
                        {
                            "title": "Oregon Circuit Court Filing Fee Schedule (ORS 21.145)",
                            "url": "https://www.courts.oregon.gov/forms/Documents/2027-OJD-Fee-Schedule.pdf",
                            "published_date": "2026-11-24",
                            "score": 0.94,
                            "content": "Updated circuit court filing fee schedule under ORS 21.145 effective Jan. 1, 2027.",
                        },
                        {
                            # Direct registry URL match that Gemini omits — guardrail must retain it!
                            "title": "Circuit Court Fee Schedule, effective January 1, 2026",
                            "url": "https://www.courts.oregon.gov/Documents/2026_CircuitFeeSchedule_public_eff-2026-01-01.pdf",
                            "published_date": "",
                            "score": 0.72,
                            "content": "Revised fee schedule note on ORS 21.145.",
                        },
                        {
                            "title": "Oregon Courts Welcome Page",
                            "url": "https://www.courts.oregon.gov/pages/default.aspx",
                            "published_date": "",
                            "score": 0.68,
                            "content": "General directory.",
                        },
                        {
                            "title": "2022 Old Fee Schedule",
                            "url": "https://www.courts.oregon.gov/forms/Documents/2022-Fees.pdf",
                            "published_date": "2022-01-01",
                            "score": 0.91,
                            "content": "Archived.",
                        },
                    ]
                }
            ).encode("utf-8")

        if ":generateContent" in url:
            assert hdrs.get("x-goog-api-key") == "AIzaSyTestFreeTierGeminiKey123456789"
            req_json = json.loads((body or b"{}").decode("utf-8"))
            assert "tools" not in req_json
            assert req_json["generationConfig"]["responseMimeType"] == "application/json"
            assert "responseSchema" in req_json["generationConfig"]
            prompt_text = req_json["contents"][0]["parts"][0]["text"]
            if "executive wire summary" in prompt_text:
                return json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "text": json.dumps(
                                                {
                                                    "bullets": [
                                                        "Primary federal updates hit 22 CFR 51.20 and the Orr v. Trump docket.",
                                                        "Oregon court fee lead under ORS 21.145 may affect tracked $124 petition fee.",
                                                    ]
                                                }
                                            )
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ).encode("utf-8")
            # Intentionally return ONLY index 0 so we verify index 1 (cited registry URL) is force-retained
            # and index 2 (Oregon Courts Welcome Page) lands in dropped_candidates!
            return json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "kept": [
                                                    {
                                                        "index": 0,
                                                        "urgency": "urgent",
                                                        "jurisdiction": "OR",
                                                        "summary": "Oregon Judicial Department published a 2027 circuit court fee schedule PDF.",
                                                        "check_hint": "Verify $124 name/sex petition fee in facts.yaml.",
                                                    }
                                                ]
                                            }
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")

        if "oregoncapitalchronicle.com" in url or "stateline.org" in url:
            return (
                b'<?xml version="1.0" encoding="UTF-8"?><rss><channel>'
                b"<item><title>Oregon courts prepare updated 2027 name change packet instructions</title>"
                b"<link>https://oregoncapitalchronicle.com/2026/11/24/name-change-packets/</link>"
                b"<pubDate>Tue, 24 Nov 2026 14:00:00 +0000</pubDate>"
                b"<description>Updated court filing guidance for marriage, divorce, and court petitions.</description></item>"
                b"<item><title>Holiday coastal ferry schedule announced</title>"
                b"<link>https://oregoncapitalchronicle.com/2026/11/24/ferry/</link>"
                b"<pubDate>Tue, 24 Nov 2026 15:00:00 +0000</pubDate>"
                b"<description>Bring your driver's license for boarding.</description></item>"
                b"</channel></rss>"
            )

        return b'<?xml version="1.0" encoding="UTF-8"?><rss><channel></channel></rss>'

    env = {
        "COURTLISTENER_TOKEN": "cl-secret-token-999",
        "LEGISCAN_API_KEY": "ls-secret-key-888",
        "TAVILY_API_KEY": "tvly-secret-test-key",
        "GEMINI_API_KEY": "AIzaSyTestFreeTierGeminiKey123456789",
    }
    res = wire.run_sweep(
        "id-records",
        days=7,
        today=dt.date(2026, 11, 25),
        fetcher=full_wire_fetcher,
        env=env,
        ai_brief=True,
    )

    # 3 dated Tavily queries + 1 undated registry-domain sweep = 4 credits
    assert res["tavily_credits_used"] == 4
    assert any("start_date" in p for p in tavily_payloads)
    assert any("start_date" not in p for p in tavily_payloads)
    assert not res["health_alerts"]
    assert res["all_channels_failed"] is False

    items_by_url = {i["url"]: i for i in res["items"]}

    # Federal Register pagination + deadlines + urgency calibration:
    # Notice 2026-88001 is `watch` with comment deadline; Final Rule 2026-88003 (effective 2026-12-15) is `urgent`!
    fr_notice = items_by_url["https://www.federalregister.gov/d/2026-88001"]
    fr_rule = items_by_url["https://www.federalregister.gov/d/2026-88003"]
    assert fr_notice["urgency"] == "watch"
    assert "Comments close 2027-01-15" in fr_notice["summary"]
    assert fr_rule["urgency"] == "urgent"
    assert "Effective 2026-12-15" in fr_rule["summary"]

    # eCFR caught newly issued rule despite older 2026-10-01 amendment_date and displayed both dates!
    ecfr_item = items_by_url["https://www.ecfr.gov/current/title-22/section-51.20"]
    assert ecfr_item["urgency"] == "urgent"
    assert "Amendment date: 2026-10-01" in ecfr_item["summary"]
    assert "2026-11-22" in ecfr_item["summary"]

    # State Register (Utah Rules RSS) arrived as primary official item!
    assert "https://rules.utah.gov/2026/11/r436-vital-records-notice/" in items_by_url
    assert items_by_url["https://rules.utah.gov/2026/11/r436-vital-records-notice/"]["tier"] == "primary"

    # RECAP caught late-uploaded older Entry #215 (`published` Nov 1, `updated` Nov 24) as `urgent` Order,
    # while Entry #216 Joint status report is `watch` (NOT urgent)!
    recap_215 = items_by_url["https://www.courtlistener.com/docket/69623342/orr-v-trump/?order_by=desc#entry-215"]
    recap_216 = items_by_url["https://www.courtlistener.com/docket/69623342/orr-v-trump/?order_by=desc#entry-216"]
    assert recap_215["urgency"] == "urgent"
    assert "RECAP updated 2026-11-24" in recap_215["summary"]
    assert recap_216["urgency"] == "watch"

    # CourtListener v4 Opinion is `urgent`, new Docket without order is `watch`
    assert items_by_url["https://www.courtlistener.com/opinion/991122/orr-v-trump/"]["urgency"] == "urgent"
    assert items_by_url["https://www.courtlistener.com/docket/88881111/"]["urgency"] == "watch"

    # LegiScan linked directly to official Oregon OLIS `state_link` and surfaced upcoming hearing from `calendar`!
    leg_item = items_by_url["https://olis.oregonlegislature.gov/liz/2027R1/Measures/Overview/HB2050"]
    assert "2026-12-02 08:00 (HR A): House Judiciary Public Hearing" in leg_item["summary"]
    assert res["wire_state"]["legiscan_change_hashes"]["7001"] == "hash-hb2050-v2"

    # Gemini guardrail retained cited `ID-OR-OJD-FEES` URL as `urgent`, and listed Welcome Page in dropped `<details>`!
    retained = items_by_url["https://www.courts.oregon.gov/Documents/2026_CircuitFeeSchedule_public_eff-2026-01-01.pdf"]
    assert retained["urgency"] == "urgent"
    assert "Retained automatically" in retained["summary"]
    assert any("Welcome Page" in d["title"] for d in res["dropped_candidates"])

    md = wire.render_markdown_report(res)
    assert "<details><summary>Dropped `.gov` / court search candidates" in md
    assert "### Wire channel telemetry" in md
    for secret_val in env.values():
        assert secret_val not in md
        assert secret_val not in json.dumps({k: v for k, v in res.items() if k != "rendered_report"})


def test_page_watch_yaml_fingerprint_state_baseline_and_pdf_text_extraction(tmp_path: Path) -> None:
    wire = load_wire_module()
    verify = load_script("verify_sources", "verify_sources.py")
    retry_mod = load_script("http_retry", "http_retry.py")

    # 1. SharePoint / Dynatrace volatile HTML tokens must NOT change normalized fingerprint
    html_v1 = (
        '<html><head><script src="/ruxitagentjs.js" data-dtconfig="rid=RID_111"></script></head>'
        '<body><input type="hidden" name="__VIEWSTATE" value="AAA111" />'
        '<input type="hidden" name="__REQUESTDIGEST" value="DIGEST_1" />'
        "<h1>Oregon Filing Fee</h1><p>Petition fee is $124.</p></body></html>"
    ).encode("utf-8")
    html_v1_rotated = (
        '<html><head><script src="/ruxitagentjs.js" data-dtconfig="rid=RID_999"></script></head>'
        '<body><input type="hidden" name="__VIEWSTATE" value="ZZZ999" />'
        '<input type="hidden" name="__REQUESTDIGEST" value="DIGEST_2" />'
        "<h1>Oregon Filing Fee</h1><p>Petition fee is $124.</p></body></html>"
    ).encode("utf-8")
    html_v2_changed = (
        "<html><body><h1>Oregon Filing Fee</h1><p>Petition fee is $135 effective Jan. 1, 2027.</p></body></html>"
    ).encode("utf-8")

    fp1 = retry_mod.compute_page_fingerprint(html_v1, {"Content-Type": "text/html"})
    fp1_rot = retry_mod.compute_page_fingerprint(html_v1_rotated, {"Content-Type": "text/html"})
    fp2 = retry_mod.compute_page_fingerprint(html_v2_changed, {"Content-Type": "text/html"})
    assert fp1["fingerprint"] == fp1_rot["fingerprint"]
    assert fp1["fingerprint"] != fp2["fingerprint"]

    # 2. Compressed PDF stream text extraction detects single-number edits inside undated PDFs
    pdf_v1 = _make_minimal_flate_pdf("Oregon Health Plan HR1 Provider FAQ: 80 hours per month")
    pdf_v2 = _make_minimal_flate_pdf("Oregon Health Plan HR1 Provider FAQ: 60 hours per month")
    pfp1 = retry_mod.compute_page_fingerprint(pdf_v1, {"Content-Type": "application/pdf"}, url="https://oregon.gov/faq.pdf")
    pfp2 = retry_mod.compute_page_fingerprint(pdf_v2, {"Content-Type": "application/pdf"}, url="https://oregon.gov/faq.pdf")
    assert pfp1["method"] == "pdf_text"
    assert pfp1["fingerprint"] != pfp2["fingerprint"]

    # 3. End-to-end `verify_sources.record_fingerprints` + `poll_page_watch` ("Changed since you last read it on ...")
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    reg_yaml = sources_dir / "mini.yaml"
    reg_yaml.write_text(
        "# Header comment preserved\n"
        "- id: MED-TEST-01\n"
        "  desk: medicaid\n"
        "  jurisdiction: OR\n"
        "  title: OHA Provider FAQ\n"
        "  short: OHA FAQ\n"
        "  publisher: OHA\n"
        "  url: https://www.oregon.gov/oha/faq.pdf\n"
        "  kind: agency-form\n"
        "  document_date: No date shown\n"
        "  checked: '2026-11-21'\n"
        "  status: official\n"
        "- id: MED-TEST-02\n"
        "  desk: medicaid\n"
        "  jurisdiction: OR\n"
        "  title: OHA Fee Page\n"
        "  short: OHA Fee Page\n"
        "  publisher: OHA\n"
        "  url: https://www.oregon.gov/oha/fees.aspx\n"
        "  kind: state-page\n"
        "  document_date: Live page\n"
        "  checked: '2026-11-20'\n"
        "  status: official\n",
        encoding="utf-8",
    )

    live_payloads = {
        "https://www.oregon.gov/oha/faq.pdf": (200, {"content-type": "application/pdf", "etag": '"v1"'}, pdf_v1),
        "https://www.oregon.gov/oha/fees.aspx": (200, {"content-type": "text/html", "etag": '"h1"'}, html_v1),
    }
    updated_cnt, warns = verify.record_fingerprints(
        source_ids={"MED-TEST-01"},
        sources_dir=sources_dir,
        pause_seconds=0.0,
        fetcher=lambda u, _t: live_payloads[u],
    )
    assert updated_cnt == 1 and not warns
    saved_text = reg_yaml.read_text(encoding="utf-8")
    assert "# Header comment preserved" in saved_text
    assert f"fingerprint: {pfp1['fingerprint']}" in saved_text

    wire_dir = tmp_path / "wire"
    wire_dir.mkdir()
    (wire_dir / "medicaid.yaml").write_text(
        yaml.safe_dump(
            {
                "desk": "medicaid",
                "enabled": True,
                "label": "Medicaid",
                "registry": str(reg_yaml),
                "source_prefix": "MED-",
                "route_prefix": "/medicaid/",
                "channels": {"page_watch": {"enabled": True}},
            }
        ),
        encoding="utf-8",
    )
    profiles = wire.load_wire_profiles(wire_dir)

    # Sweep 1: MED-TEST-01 matches YAML fingerprint; MED-TEST-02 initializes baseline in wire_state -> 0 alerts
    items1, fps1, probed1, st1 = wire.poll_page_watch(
        "medicaid",
        lambda u, **_kw: live_payloads[u],
        today=dt.date(2026, 11, 25),
        wire_state=wire.empty_wire_state("medicaid"),
        profiles=profiles,
    )
    assert items1 == []
    assert probed1 == 2
    assert "1 baseline(s) initialized" in st1

    # Sweep 2: Both PDF (YAML fingerprint) and HTML (state baseline) are edited in place -> 2 primary urgent items!
    live_payloads["https://www.oregon.gov/oha/faq.pdf"] = (200, {"content-type": "application/pdf", "etag": '"v2"'}, pdf_v2)
    live_payloads["https://www.oregon.gov/oha/fees.aspx"] = (200, {"content-type": "text/html", "etag": '"h2"'}, html_v2_changed)
    state_after_1 = {**wire.empty_wire_state("medicaid"), "page_fingerprints": fps1}
    items2, fps2, _, _ = wire.poll_page_watch(
        "medicaid",
        lambda u, **_kw: live_payloads[u],
        today=dt.date(2026, 11, 27),
        wire_state=state_after_1,
        profiles=profiles,
    )
    assert len(items2) == 2
    pdf_alert = [i for i in items2 if "MED-TEST-01" in i["title"]][0]
    html_alert = [i for i in items2 if "MED-TEST-02" in i["title"]][0]
    assert "Changed since you last read it on 2026-11-21" in pdf_alert["summary"]
    assert "Changed since baseline recorded on 2026-11-25" in html_alert["summary"]

    # Sweep 3: Server returns HTTP 304 Not Modified on If-None-Match -> 0 items
    def cond_304_fetcher(u: str, *, headers: dict | None = None) -> tuple[int, dict[str, str], bytes]:
        if headers and headers.get("If-None-Match") == '"h2"':
            return (304, {}, b"")
        return (200, {"content-type": "application/pdf"}, pdf_v1)

    state_after_2 = {**wire.empty_wire_state("medicaid"), "page_fingerprints": fps2}
    items3, _, _, st3 = wire.poll_page_watch(
        "medicaid",
        cond_304_fetcher,
        today=dt.date(2026, 11, 28),
        wire_state=state_after_2,
        profiles=profiles,
    )
    assert items3 == []
    assert "1 HTTP 304 unchanged" in st3


def test_gemini_stable_before_preview_sorting_and_tavily_content_hash_resurfacing() -> None:
    wire = load_wire_module()

    def models_fetcher(url: str, **_kwargs) -> bytes:
        assert url.endswith("/v1beta/models")
        return json.dumps(
            {
                "models": [
                    {"name": "models/gemini-4.5-flash-preview-09-2026", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3.10-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3.8-flash-tts", "supportedGenerationMethods": ["generateContent"]},
                ]
            }
        ).encode("utf-8")

    ordered = wire.discover_gemini_flash_models("AIzaSyDummyKey1234567890123", models_fetcher)
    # Stable lite (3.10 > 3.5) -> stable flash (3.6) -> preview last (4.5-preview), with TTS excluded!
    assert ordered == [
        "gemini-3.10-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-4.5-flash-preview-09-2026",
    ]

    # Verify Tavily URL + content hash resurfaces a previously reported URL when its snippet changes!
    version = {"n": 1}

    def tavily_edit_fetcher(url: str, **_kwargs) -> bytes:
        if url == "https://api.tavily.com/search":
            txt = (
                "OHA community engagement starts Jan 2027."
                if version["n"] == 1
                else "OHA delayed community engagement enforcement until April 2027."
            )
            return json.dumps(
                {
                    "results": [
                        {
                            "title": "OHA HR1 Bulletin",
                            "url": "https://www.oregon.gov/oha/hr1-bulletin.pdf",
                            "published_date": "",
                            "score": 0.91,
                            "content": txt,
                        }
                    ]
                }
            ).encode("utf-8")
        raise AssertionError(url)

    items_v1, _, _ = wire.poll_tavily_and_gemini(
        "medicaid",
        dt.date(2026, 11, 20),
        "tvly-key-1234",
        "",
        tavily_edit_fetcher,
        today=dt.date(2026, 11, 25),
    )
    version["n"] = 2
    items_v2, _, _ = wire.poll_tavily_and_gemini(
        "medicaid",
        dt.date(2026, 11, 20),
        "tvly-key-1234",
        "",
        tavily_edit_fetcher,
        today=dt.date(2026, 11, 27),
        prior_url_hashes={"oregon.gov/oha/hr1-bulletin.pdf": items_v1[0]["_content_fp"]},
    )
    assert items_v1[0]["id"] != items_v2[0]["id"]
    assert "Updated text on previously seen .gov/court URL" in items_v2[0]["summary"]


def test_wire_health_alerts_nonzero_exit_overlap_lookback_truncation_and_heartbeat(tmp_path: Path) -> None:
    wire = load_wire_module()

    # 1. Overlap lookback (`last_success - 3d`) + empty-streak health alert
    prior_state = wire.empty_wire_state("medicaid")
    prior_state["channels"]["federal_register"] = {
        "last_success": "2026-11-10",
        "consecutive_empty_runs": 0,
        "ever_matched": True,
    }
    prior_state["channels"]["rss"] = {
        "last_success": "2026-11-24",
        "consecutive_empty_runs": 2,
        "ever_matched": True,
    }

    seen_fr_urls: list[str] = []
    hb_calls: list[tuple[str, bytes | None]] = []

    def quiet_fetcher(url: str, *, body: bytes | None = None, **_kwargs) -> bytes:
        if "heartbeat.example" in url:
            hb_calls.append((url, body))
            return b"ok"
        if "federalregister.gov" in url:
            seen_fr_urls.append(url)
            return json.dumps({"results": []}).encode("utf-8")
        if "ecfr.gov" in url:
            return json.dumps({"content_versions": []}).encode("utf-8")
        if "api.oregonlegislature.gov" in url:
            return json.dumps({"value": []}).encode("utf-8")
        return b'<?xml version="1.0" encoding="UTF-8"?><rss><channel></channel></rss>'

    res = wire.run_sweep(
        "medicaid",
        days=7,
        today=dt.date(2026, 11, 27),
        wire_state=prior_state,
        fetcher=quiet_fetcher,
        env={"WIRE_HEARTBEAT_URL": "https://heartbeat.example/ping/secret-uuid-999"},
    )
    # 2026-11-10 minus 3 overlap days = 2026-11-07 (bridging the 17-day gap cleanly!)
    assert any("2026-11-07" in u for u in seen_fr_urls)
    # RSS hit 3 consecutive empty runs -> wire:health alert + `/fail` heartbeat!
    assert any("Policy & State News RSS: returned 0 raw items for 3 consecutive run(s)" in a for a in res["health_alerts"])
    assert len(hb_calls) == 1
    assert hb_calls[0][0].endswith("/fail")
    assert "secret-uuid-999" not in res.get("heartbeat_status", "")

    # 2. Smart report truncation preserves telemetry table AND defers omitted item IDs out of `reported_ids`
    many_items_fetcher_calls = 0

    def busy_rss_fetcher(url: str, **_kwargs) -> bytes:
        nonlocal many_items_fetcher_calls
        many_items_fetcher_calls += 1
        if "federalregister.gov" in url:
            return json.dumps({"results": []}).encode("utf-8")
        if "ecfr.gov" in url:
            return json.dumps({"content_versions": []}).encode("utf-8")
        if "oregoncapitalchronicle.com" in url:
            items_xml = "".join(
                f"<item><title>Medicaid OHP community engagement bulletin number {i:03d} in Oregon</title>"
                f"<link>https://oregoncapitalchronicle.com/2026/11/25/item-{i}/</link>"
                f"<pubDate>Wed, 25 Nov 2026 12:00:00 +0000</pubDate>"
                f"<description>{'Detailed OHP renewal and exemption text. ' * 5}</description></item>"
                for i in range(30)
            )
            return f'<?xml version="1.0" encoding="UTF-8"?><rss><channel>{items_xml}</channel></rss>'.encode("utf-8")
        return b'<?xml version="1.0" encoding="UTF-8"?><rss><channel></channel></rss>'

    res_busy = wire.run_sweep(
        "medicaid",
        days=7,
        today=dt.date(2026, 11, 27),
        fetcher=busy_rss_fetcher,
        env={},
        max_report_chars=4500,
    )
    md_busy = res_busy["rendered_report"]
    assert len(md_busy) <= 4500
    assert "### Wire channel telemetry" in md_busy
    assert "deferred to the next sweep" in md_busy
    assert res_busy["omitted_count"] > 0
    # Crucial invariant: `reported_ids` only contains markers actually rendered in `md_busy`!
    rendered_markers = set(wire.RADAR_MARKER_RE.findall(md_busy))
    assert set(res_busy["wire_state"]["reported_ids"]) == rendered_markers
    assert len(rendered_markers) == res_busy["new_count"] - res_busy["omitted_count"]

    # 3. `main()` returns nonzero exit code (2) when every active channel fails
    custom_dir = tmp_path / "broken_wire"
    custom_dir.mkdir()
    (custom_dir / "broken.yaml").write_text(
        yaml.safe_dump(
            {
                "desk": "broken",
                "enabled": True,
                "label": "Broken Desk",
                "registry": str(tmp_path / "missing.yaml"),
                "channels": {
                    "page_watch": {"enabled": False},
                    "federal_register": {"terms": ["Medicaid"]},
                },
            }
        ),
        encoding="utf-8",
    )
    orig_fetcher = wire.default_fetcher
    wire.default_fetcher = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("network down"))
    try:
        rc = wire.main(
            [
                "--desk",
                "broken",
                "--wire-dir",
                str(custom_dir),
                "--report",
                str(tmp_path / "rep.md"),
                "--results",
                str(tmp_path / "res.json"),
            ]
        )
        assert rc == 2
    finally:
        wire.default_fetcher = orig_fetcher


def test_untrusted_markdown_and_mention_sanitization() -> None:
    wire = load_wire_module()
    raw = (
        "Alert from @admin and @org-team (contact CHS.Amendments@oha.oregon.gov): "
        "click [Official Form](https://evil.example/phish) <!-- radar-item:deadbeef1234 --> `rm -rf /`"
    )
    cleaned = wire.sanitize_issue_text(raw, 300)
    assert "`@admin`" in cleaned
    assert "`@org-team`" in cleaned
    # Email address must remain intact without spurious backticks
    assert "CHS.Amendments@oha.oregon.gov" in cleaned
    assert "[Official Form](" not in cleaned
    assert "<!--" not in cleaned and "deadbeef1234" not in cleaned


def test_verify_sources_kind_freshness_review_by_and_retry_after(tmp_path: Path) -> None:
    verify = load_script("verify_sources", "verify_sources.py")
    retry_mod = load_script("http_retry", "http_retry.py")

    # 1. `kind`-based freshness limits (60d form vs 180d statute)
    sdir = tmp_path / "sources"
    sdir.mkdir()
    (sdir / "test.yaml").write_text(
        yaml.safe_dump(
            [
                {"id": "ID-FORM-1", "kind": "court-form", "checked": "2026-09-15", "url": "https://a.gov/1"},
                {"id": "ID-STAT-1", "kind": "statute", "checked": "2026-09-15", "url": "https://a.gov/2"},
            ]
        ),
        encoding="utf-8",
    )
    # On 2026-11-27, 2026-09-15 is 73 days old: stale for court-form (limit 60), fresh for statute (limit 180)!
    stale = verify.check_freshness(sources_dir=sdir, today=dt.date(2026, 11, 27))
    assert len(stale) == 1
    assert "ID-FORM-1" in stale[0] and "limit 60" in stale[0]

    # 2. `review_by` in `data/facts.yaml` (`ut_fee_petition` has `review_by: '2027-01-01'`)
    assert verify.check_review_by_dates(today=dt.date(2026, 11, 27)) == []
    due = verify.check_review_by_dates(today=dt.date(2027, 1, 2))
    assert any("fact ut_fee_petition" in line for line in due)

    # 3. `429` + `Retry-After` (numeric and HTTP-date) in shared `http_retry`
    now_ref = dt.datetime(2026, 11, 27, 14, 0, 0, tzinfo=dt.timezone.utc)
    assert retry_mod.parse_retry_after("4", now=now_ref) == 4.0
    assert retry_mod.parse_retry_after("Fri, 27 Nov 2026 14:00:09 GMT", now=now_ref) == 9.0

    slept: list[float] = []
    calls = {"n": 0}

    class _FakeResp:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b"recovered"

    def flaky_urlopen(req, timeout=25):
        assert req.get_header("User-agent") == "CivicDesk-Verify/1.0"
        calls["n"] += 1
        if calls["n"] == 1:
            hdrs = email.message.Message()
            hdrs["Retry-After"] = "3.5"
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", hdrs, io.BytesIO(b""))
        return _FakeResp()

    st, _, body = retry_mod.fetch_with_retry(
        "https://www.oregon.gov/test",
        sleeper=slept.append,
        urlopen_fn=flaky_urlopen,
    )
    assert st == 200 and body == b"recovered"
    assert slept == [3.5]


def test_workflows_plumb_state_health_contracts_and_secrets() -> None:
    radar_wf = yaml.safe_load((ROOT / ".github" / "workflows" / "radar.yml").read_text(encoding="utf-8"))
    assert "discover" in radar_wf["jobs"]
    assert "radar" in radar_wf["jobs"]

    sweep_steps = [
        s for s in radar_wf["jobs"]["radar"]["steps"] if "policy_radar.py" in str(s.get("run") or "")
    ]
    assert len(sweep_steps) == 1
    step_env = sweep_steps[0]["env"]
    for required_secret in (
        "COURTLISTENER_TOKEN",
        "LEGISCAN_API_KEY",
        "TAVILY_API_KEY",
        "GEMINI_API_KEY",
        "WIRE_HEARTBEAT_URL",
    ):
        assert required_secret in step_env
        assert f"secrets.{required_secret}" in str(step_env[required_secret])

    radar_steps = radar_wf["jobs"]["radar"]["steps"]
    radar_runs = "\n".join(str(s.get("run") or "") for s in radar_steps)
    assert "wire:state" in radar_runs
    assert "wire:health" in radar_runs
    assert "RADAR_RC" in radar_runs

    dispatch_idx = next(
        i for i, s in enumerate(radar_steps) if "policy-radar issue" in str(s.get("name") or "")
    )
    state_idx = next(
        i for i, s in enumerate(radar_steps) if "Persist per-desk wire:state" in str(s.get("name") or "")
    )
    assert state_idx > dispatch_idx, (
        "Persist per-desk wire:state must run after policy-radar dispatch creation "
        "so a crash in health/dispatch steps never marks unposted dispatches as reported"
    )

    weekly_wf = yaml.safe_load((ROOT / ".github" / "workflows" / "weekly.yml").read_text(encoding="utf-8"))
    weekly_runs = "\n".join(str(s.get("run") or "") for s in weekly_wf["jobs"]["verify"]["steps"])
    assert "verify_sources.py" in weekly_runs
    assert "check_wire_contracts.py" in weekly_runs
    assert "workflows/radar.yml/enable" in weekly_runs


def test_workflow_run_blocks_have_valid_bash_and_python_heredocs() -> None:
    bash_bin = shutil.which("bash")
    heredoc_start = re.compile(r"python3\s+-\s+<<(-?)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
    for wf_path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        wf = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        for job_name, job in (wf.get("jobs") or {}).items():
            for idx, step in enumerate(job.get("steps") or [], start=1):
                script = step.get("run")
                if not script:
                    continue
                label = f"{wf_path.name}::{job_name}::step {idx} ({step.get('name', '<unnamed>')})"
                if bash_bin:
                    proc = subprocess.run(
                        [bash_bin, "-n"],
                        input=script,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    assert proc.returncode == 0, f"{label} failed bash -n:\n{proc.stderr}"

                lines = script.splitlines()
                i = 0
                while i < len(lines):
                    m = heredoc_start.search(lines[i])
                    if not m:
                        i += 1
                        continue
                    strip_tabs, tag = bool(m.group(1)), m.group(3)
                    body: list[str] = []
                    i += 1
                    closed = False
                    while i < len(lines):
                        candidate = lines[i].lstrip("\t") if strip_tabs else lines[i]
                        if candidate == tag:
                            closed = True
                            i += 1
                            break
                        body.append(lines[i].lstrip("\t") if strip_tabs else lines[i])
                        i += 1
                    assert closed, f"{label}: unterminated python3 heredoc <<{tag}"
                    try:
                        ast.parse("\n".join(body))
                    except SyntaxError as exc:
                        raise AssertionError(f"{label}: invalid Python inside <<{tag}: {exc}") from exc
