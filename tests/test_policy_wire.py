from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_wire_module():
    spec = importlib.util.spec_from_file_location("policy_radar", ROOT / "scripts" / "policy_radar.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_wire_profiles_and_dynamic_desk_extensibility(tmp_path: Path) -> None:
    wire = load_wire_module()

    active = wire.list_active_desks(include_disabled=False)
    all_desks = wire.list_active_desks(include_disabled=True)
    assert active == ["medicaid", "id-records"]
    assert "public-records" in all_desks

    # Drop a brand-new YAML desk profile and verify zero-code discovery + sweep
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
                    "federal_register": {"terms": ['"eviction" HUD']},
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


def test_all_four_api_channels_and_citation_impact_mapping() -> None:
    wire = load_wire_module()
    captured: list[tuple[str, dict[str, str], bytes | None]] = []

    def full_wire_fetcher(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        body: bytes | None = None,
    ) -> bytes:
        hdrs = dict(headers or {})
        captured.append((url, hdrs, body))

        if "federalregister.gov" in url:
            return json.dumps(
                {
                    "results": [
                        {
                            "document_number": "2026-88001",
                            "title": "Passports; Sex Marker and DS-82 Documentary Requirements",
                            "type": "Notice",
                            "publication_date": "2026-11-23",
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
                    ]
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
                                "issue_date": "2026-11-22",
                                "amendment_date": "2026-11-22",
                            }
                        ]
                    }
                ).encode("utf-8")
            return json.dumps({"content_versions": []}).encode("utf-8")

        if "courtlistener.com/docket/69623342/feed/" in url:
            return (
                b'<?xml version="1.0" encoding="utf-8"?>'
                b'<feed xmlns="http://www.w3.org/2005/Atom">'
                b"<entry>"
                b"<title>Entry #215 in Orr v. Trump</title>"
                b'<link href="https://www.courtlistener.com/docket/69623342/orr-v-trump/?order_by=desc#entry-215" rel="alternate"/>'
                b"<published>2026-11-24T10:00:00-05:00</published>"
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
            assert "year=3" in url and "action%3A20261118..20270223" in url
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
                                "last_action": "Public Hearing Scheduled in House Judiciary",
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
            assert req_data["search_depth"] == "basic"
            assert req_data["include_domains_mode"] == "restrict"
            assert "courts.oregon.gov" in req_data["include_domains"]
            assert "uscourts.gov" in req_data["include_domains"]
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
            prompt_text = req_json["contents"][0]["parts"][0]["text"]
            if '"bullets"' in prompt_text:
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

    # 6 primary (FedReg, eCFR, RECAP #215 + #216 with #217 Pro Hac Vice filtered, CL v4 docket, CL v4 opinion)
    # + 2 gov/leg (LegiScan HB 2050, Tavily+Gemini OJD PDF) + 1 deduplicated RSS = 9
    assert res["new_count"] == 9
    assert res["tavily_credits_used"] == 3
    assert len(res["ai_brief"]) == 2
    recap_status = [s["status"] for s in res["statuses"] if s["source"] == "CourtListener RECAP Feeds"][0]
    assert "1 routine appearance/clerk notice(s) filtered" in recap_status

    # Verify salmon, ferry, and Pro Hac Vice noise were filtered out while both #entry-215 and #entry-216 survived
    titles = [i["title"] for i in res["items"]]
    assert any("Entry #215" in t for t in titles)
    assert any("Entry #216" in t for t in titles)
    assert not any("Entry #217" in t for t in titles)
    assert not any("Salmon" in t for t in titles)
    assert not any("ferry" in t.lower() for t in titles)

    # Verify CivicDesk Citation-Graph Impact Mapping linked ORS 21.145 / ORS 33.460 to ID-OR-OJD-FEES & or_fee_petition!
    assert "ID-OR-OJD-FEES" in res["touched_sources"]
    assert any(f.startswith("or_fee_petition") for f in res["touched_facts"])
    assert "/id-records/oregon/court/" in res["touched_routes"]

    # Verify zero secret leakage in URLs (except LegiScan query param which is redacted in all outputs) or rendered Markdown
    md = wire.render_markdown_report(res)
    assert "### Editorial triage summary" in md
    assert "CivicDesk impact map:" in md
    for secret_val in env.values():
        assert secret_val not in md
        assert secret_val not in json.dumps(res)
    for called_url, _, _ in captured:
        assert "AIzaSyTestFreeTierGeminiKey123456789" not in called_url
        assert "tvly-secret-test-key" not in called_url
        assert "cl-secret-token-999" not in called_url


def test_gemini_model_autodiscovery_and_tavily_fallback_resilience() -> None:
    wire = load_wire_module()

    def autodiscover_fetcher(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        body: bytes | None = None,
    ) -> bytes:
        if url == "https://api.tavily.com/search":
            return json.dumps(
                {
                    "results": [
                        {
                            "title": "OHA HR1 Community Engagement Bulletin",
                            "url": "https://www.oregon.gov/oha/HSD/OHP/Pages/HR1-Update.aspx",
                            "published_date": "2026-11-24",
                            "score": 0.89,
                            "content": "Oregon Health Authority posted updated community engagement exemption rules.",
                        }
                    ]
                }
            ).encode("utf-8")
        if url.endswith("/v1beta/models"):
            return json.dumps(
                {
                    "models": [
                        {
                            "name": "models/gemini-4.2-flash-lite",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                }
            ).encode("utf-8")
        if ":generateContent" in url and "gemini-4.2-flash-lite" not in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        if "gemini-4.2-flash-lite:generateContent" in url:
            return json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"thought": True, "text": "internal reasoning"},
                                    {
                                        "text": json.dumps(
                                            {
                                                "kept": [
                                                    {
                                                        "index": 0,
                                                        "urgency": "urgent",
                                                        "jurisdiction": "OR",
                                                        "summary": "OHA posted updated OHP community engagement exemption guidance.",
                                                    }
                                                ]
                                            }
                                        )
                                    },
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")
        raise AssertionError(f"Unexpected URL: {url}")

    items, credits, status = wire.poll_tavily_and_gemini(
        "medicaid",
        dt.date(2026, 11, 20),
        "tvly-key-1234",
        "AIzaSyDiscoveryKey1234567890",
        autodiscover_fetcher,
        today=dt.date(2026, 11, 25),
    )
    assert len(items) == 1
    assert credits == 3
    assert "gemini-4.2-flash-lite" in status

    # Verify Tavily leads survive even when Gemini returns 429 across all models
    def gemini_429_fetcher(url: str, **_kwargs) -> bytes:
        if url == "https://api.tavily.com/search":
            return json.dumps(
                {
                    "results": [
                        {
                            "title": "Utah Medicaid Community Engagement Waiver",
                            "url": "https://medicaid.utah.gov/2026-waiver-update",
                            "published_date": "2026-11-24",
                            "score": 0.88,
                            "content": "Updated Utah Medicaid community engagement timeline.",
                        }
                    ]
                }
            ).encode("utf-8")
        if url.endswith("/v1beta/models"):
            return json.dumps({"models": []}).encode("utf-8")
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b"{}"))

    fb_items, _, fb_status = wire.poll_tavily_and_gemini(
        "medicaid",
        dt.date(2026, 11, 20),
        "tvly-key-1234",
        "AIzaSyQuotaKey1234567890123",
        gemini_429_fetcher,
        today=dt.date(2026, 11, 25),
    )
    assert len(fb_items) == 1
    assert "Gemini triage fallback" in fb_status
    assert "AIzaSyQuotaKey1234567890123" not in fb_status


def test_radar_workflow_plumbs_all_four_secrets_and_dynamic_discovery() -> None:
    wf_text = (ROOT / ".github" / "workflows" / "radar.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(wf_text)

    assert "discover" in wf["jobs"]
    assert "radar" in wf["jobs"]
    assert wf["jobs"]["radar"]["needs"] == "discover"

    sweep_steps = [
        s for s in wf["jobs"]["radar"]["steps"] if "policy_radar.py" in str(s.get("run") or "")
    ]
    assert len(sweep_steps) == 1
    step_env = sweep_steps[0]["env"]
    for required_secret in (
        "COURTLISTENER_TOKEN",
        "LEGISCAN_API_KEY",
        "TAVILY_API_KEY",
        "GEMINI_API_KEY",
    ):
        assert required_secret in step_env, f"Missing {required_secret} in radar.yml env"
        assert f"secrets.{required_secret}" in str(step_env[required_secret])
