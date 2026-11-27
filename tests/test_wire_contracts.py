from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def load_contracts_module():
    spec = importlib.util.spec_from_file_location(
        "check_wire_contracts", ROOT / "scripts" / "check_wire_contracts.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_wire_contract_checker_detects_healthy_and_broken_shapes() -> None:
    contracts = load_contracts_module()

    def healthy_mock(url: str, **_kwargs) -> bytes:
        if "public-inspection-documents.json" in url:
            return json.dumps({"count": 5}).encode("utf-8")
        if "federalregister.gov" in url:
            return json.dumps(
                {
                    "results": [
                        {
                            "document_number": "2026-10001",
                            "title": "Medicaid Rule",
                            "publication_date": "2026-11-24",
                            "html_url": "https://www.federalregister.gov/d/2026-10001",
                            "type": "Rule",
                            "effective_on": "2027-01-01",
                            "comments_close_on": None,
                        }
                    ]
                }
            ).encode("utf-8")
        if "ecfr.gov" in url:
            return json.dumps(
                {
                    "content_versions": [
                        {
                            "identifier": "51.20",
                            "issue_date": "2026-11-20",
                            "amendment_date": "2026-11-20",
                            "name": "Passport application",
                        }
                    ]
                }
            ).encode("utf-8")
        if "courtlistener.com/docket/" in url:
            return (
                b'<?xml version="1.0" encoding="utf-8"?>'
                b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Order</title></entry></feed>'
            )
        if "api/rest/v4/search/" in url:
            return json.dumps({"results": [{"caseName": "Orr v. Trump"}]}).encode("utf-8")
        if "api.legiscan.com" in url:
            return json.dumps({"status": "OK", "searchresult": {"summary": {"count": 0}}}).encode("utf-8")
        if "api.tavily.com/search" in url:
            return json.dumps({"results": []}).encode("utf-8")
        if "/v1beta/models" in url:
            return json.dumps(
                {
                    "models": [
                        {
                            "name": "models/gemini-3.5-flash-lite",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                }
            ).encode("utf-8")
        if "lawfilesext.leg.wa.gov" in url:
            return (
                b'<tr><td><a href="http://lawfilesext.leg.wa.gov/law/wsr/2026/22/26-22-102.htm">26-22-102</a></td>'
                b"<td>PERM</td></tr>"
            )
        if "api.oregonlegislature.gov" in url:
            return json.dumps({"value": [{"SessionKey": "2026R1"}]}).encode("utf-8")
        return (
            b'<?xml version="1.0" encoding="UTF-8"?><rss><channel>'
            b"<item><title>Bulletin</title><link>https://rules.utah.gov/1/</link></item></channel></rss>"
        )

    env = {
        "COURTLISTENER_TOKEN": "cl-tok-secret",
        "LEGISCAN_API_KEY": "leg-key-secret",
        "TAVILY_API_KEY": "tvly-key-secret",
        "GEMINI_API_KEY": "AIzaSySecretContractKey123456789",
    }
    ok_rows = contracts.run_live_contract_checks(fetcher=healthy_mock, env=env)
    assert all(r["status"] == "ok" for r in ok_rows)
    assert len(ok_rows) == 11

    # Schema drift (e.g., eCFR renames `content_versions` -> `versions`) must be flagged as `error`!
    def drifted_mock(url: str, **kwargs) -> bytes:
        if "ecfr.gov" in url:
            return json.dumps({"versions": []}).encode("utf-8")
        return healthy_mock(url, **kwargs)

    bad_rows = contracts.run_live_contract_checks(fetcher=drifted_mock, env={})
    ecfr_row = [r for r in bad_rows if r["channel"] == "eCFR Versioner API v1"][0]
    assert ecfr_row["status"] == "error"


@pytest.mark.skipif(
    os.environ.get("CIVICDESK_LIVE_CONTRACTS") != "1",
    reason="Set CIVICDESK_LIVE_CONTRACTS=1 to run live network contract checks",
)
def test_live_upstream_api_contracts() -> None:
    contracts = load_contracts_module()
    outcomes = contracts.run_live_contract_checks()
    failed = [r for r in outcomes if r["status"] == "error"]
    assert not failed, f"Live contract failures: {failed}"
