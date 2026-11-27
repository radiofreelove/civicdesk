#!/usr/bin/env python3
"""Shared HTTP retry/backoff (`429` + `Retry-After`) and page fingerprinting helpers.

Used by both `scripts/verify_sources.py` and `scripts/policy_radar.py` so every
automated probe honors `User-Agent: CivicDesk-Verify/1.0`, transient backoff,
and normalized HTML/PDF content fingerprinting.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import html
import io
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Mapping
from typing import Any

USER_AGENT = "CivicDesk-Verify/1.0"
TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})

_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_VOLATILE_INPUT_RE = re.compile(
    r"<input\b[^>]*(?:__VIEWSTATE|__VIEWSTATEGENERATOR|__EVENTVALIDATION|__REQUESTDIGEST|csrf|nonce)[^>]*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_PDF_STREAM_RE = re.compile(rb"stream(?:\r\n|\n)(.*?)(?:\r\n|\n)?endstream", re.DOTALL)
_PDF_BT_ET_RE = re.compile(rb"\bBT\b(.*?)\bET\b", re.DOTALL)
_PDF_HEX_STR_RE = re.compile(rb"<([0-9A-Fa-f\s]{2,})>")


def parse_retry_after(
    header_value: str | None,
    *,
    now: dt.datetime | None = None,
    max_seconds: float = 60.0,
) -> float | None:
    """Parse an HTTP `Retry-After` header (seconds or HTTP-date), clamped to `[0, max_seconds]`."""
    if not header_value:
        return None
    raw = str(header_value).strip()
    if not raw:
        return None
    try:
        secs = float(raw)
        return max(0.0, min(secs, max_seconds))
    except ValueError:
        pass
    try:
        parsed_dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=dt.timezone.utc)
    ref_now = now or dt.datetime.now(dt.timezone.utc)
    delta = (parsed_dt - ref_now).total_seconds()
    return max(0.0, min(delta, max_seconds))


def _headers_to_dict(hdrs: Any) -> dict[str, str]:
    if not hdrs:
        return {}
    if isinstance(hdrs, Mapping):
        return {str(k).lower(): str(v).strip() for k, v in hdrs.items() if v is not None}
    items = getattr(hdrs, "items", None)
    if callable(items):
        return {str(k).lower(): str(v).strip() for k, v in items() if v is not None}
    return {}


def fetch_with_retry(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    method: str = "GET",
    body: bytes | None = None,
    timeout: int = 25,
    max_retries: int = 2,
    base_delay: float = 0.75,
    max_retry_after: float = 60.0,
    sleeper: Callable[[float], None] = time.sleep,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[int, dict[str, str], bytes]:
    """Fetch `url` with `CivicDesk-Verify/1.0`, retrying `429`/`5xx` and transient network errors."""
    req_headers: dict[str, str] = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/xml, text/html, */*",
    }
    if headers:
        req_headers.update(headers)

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urlopen_fn(req, timeout=timeout) as resp:
                status = int(getattr(resp, "status", 200))
                resp_headers = _headers_to_dict(getattr(resp, "headers", None))
                payload = resp.read()
            if status in TRANSIENT_HTTP_CODES and attempt < max_retries:
                retry_delay = parse_retry_after(
                    resp_headers.get("retry-after"),
                    max_seconds=max_retry_after,
                )
                wait_s = retry_delay if retry_delay is not None else (base_delay * (2**attempt))
                attempt += 1
                sleeper(wait_s)
                continue
            return status, resp_headers, payload
        except urllib.error.HTTPError as exc:
            exc_headers = _headers_to_dict(getattr(exc, "headers", None))
            if exc.code == 304:
                return 304, exc_headers, b""
            if exc.code in TRANSIENT_HTTP_CODES and attempt < max_retries:
                retry_delay = parse_retry_after(
                    exc_headers.get("retry-after"),
                    max_seconds=max_retry_after,
                )
                wait_s = retry_delay if retry_delay is not None else (base_delay * (2**attempt))
                attempt += 1
                sleeper(wait_s)
                continue
            raise
        except Exception:
            if attempt < max_retries:
                wait_s = base_delay * (2**attempt)
                attempt += 1
                sleeper(wait_s)
                continue
            raise


def _unescape_pdf_literal(raw: bytes) -> str:
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b == 0x5C and i + 1 < n:  # backslash
            nxt = raw[i + 1]
            if nxt in b"01234567":
                oct_digits = bytes([nxt])
                j = i + 2
                while j < n and len(oct_digits) < 3 and raw[j] in b"01234567":
                    oct_digits += bytes([raw[j]])
                    j += 1
                out.append(int(oct_digits, 8) & 0xFF)
                i = j
                continue
            esc_map = {
                ord("n"): 0x0A,
                ord("r"): 0x0D,
                ord("t"): 0x09,
                ord("b"): 0x08,
                ord("f"): 0x0C,
                ord("("): ord("("),
                ord(")"): ord(")"),
                ord("\\"): ord("\\"),
            }
            if nxt in (0x0A, 0x0D):
                i += 2
                if nxt == 0x0D and i < n and raw[i] == 0x0A:
                    i += 1
                continue
            out.append(esc_map.get(nxt, nxt))
            i += 2
            continue
        out.append(b)
        i += 1
    if out.startswith(b"\xfe\xff"):
        return bytes(out[2:]).decode("utf-16-be", errors="ignore")
    return bytes(out).decode("latin-1", errors="ignore")


def _extract_pdf_paren_strings(block: bytes) -> list[str]:
    results: list[str] = []
    i = 0
    n = len(block)
    while i < n:
        if block[i] == ord("("):
            depth = 1
            j = i + 1
            buf = bytearray()
            while j < n and depth > 0:
                ch = block[j]
                if ch == 0x5C and j + 1 < n:
                    buf.append(ch)
                    buf.append(block[j + 1])
                    j += 2
                    continue
                if ch == ord("("):
                    depth += 1
                    buf.append(ch)
                elif ch == ord(")"):
                    depth -= 1
                    if depth > 0:
                        buf.append(ch)
                else:
                    buf.append(ch)
                j += 1
            if depth == 0 and buf:
                s = _unescape_pdf_literal(bytes(buf)).strip()
                if s:
                    results.append(s)
            i = j
        else:
            i += 1
    return results


def extract_pdf_text(raw_bytes: bytes) -> str:
    """Extract normalized text from a PDF using `pypdf` when installed or stdlib stream decoding."""
    if not raw_bytes:
        return ""
    try:
        import pypdf  # type: ignore[import-not-found]

        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        pages_text = [(page.extract_text() or "") for page in reader.pages]
        joined = _WS_RE.sub(" ", " ".join(pages_text)).strip().lower()
        if joined:
            return joined
    except Exception:  # noqa: BLE001
        pass

    chunks: list[str] = []
    for m in _PDF_STREAM_RE.finditer(raw_bytes):
        stream_data = m.group(1)
        decoded: bytes | None = None
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                decoded = zlib.decompress(stream_data, wbits)
                break
            except zlib.error:
                continue
        target = decoded if decoded is not None else stream_data
        if b"BT" not in target:
            continue
        for bt_m in _PDF_BT_ET_RE.finditer(target):
            bt_block = bt_m.group(1)
            chunks.extend(_extract_pdf_paren_strings(bt_block))
            for hex_m in _PDF_HEX_STR_RE.finditer(bt_block):
                hex_clean = re.sub(rb"\s+", b"", hex_m.group(1))
                if len(hex_clean) % 2 == 1:
                    hex_clean += b"0"
                try:
                    raw_hex = bytes.fromhex(hex_clean.decode("ascii"))
                except ValueError:
                    continue
                if raw_hex.startswith(b"\xfe\xff") or len(raw_hex) % 2 == 0 and b"\x00" in raw_hex:
                    txt = raw_hex.removeprefix(b"\xfe\xff").decode("utf-16-be", errors="ignore").strip()
                else:
                    txt = raw_hex.decode("latin-1", errors="ignore").strip()
                if txt and any(c.isalnum() for c in txt):
                    chunks.append(txt)

    normalized = _WS_RE.sub(" ", " ".join(chunks)).strip().lower()
    return normalized


def normalize_html_text(raw_text: str) -> str:
    """Strip scripts, styles, comments, volatile CSRF/SharePoint inputs, and tags into canonical text."""
    if not raw_text:
        return ""
    cleaned = _SCRIPT_STYLE_RE.sub(" ", raw_text)
    cleaned = _HTML_COMMENT_RE.sub(" ", cleaned)
    cleaned = _VOLATILE_INPUT_RE.sub(" ", cleaned)
    cleaned = html.unescape(_TAG_RE.sub(" ", cleaned))
    return _WS_RE.sub(" ", cleaned).strip().lower()


def compute_page_fingerprint(
    body: bytes,
    headers: Mapping[str, str] | None = None,
    *,
    url: str = "",
) -> dict[str, str]:
    """Compute a stable content fingerprint for HTML, text, or PDF source responses."""
    hdrs = _headers_to_dict(headers)
    ctype = hdrs.get("content-type", "").lower()
    path_lower = urllib.parse.urlparse(url).path.lower() if url else ""
    bytes_hex = hashlib.sha256(body).hexdigest()[:32]

    is_pdf = body.startswith(b"%PDF-") or "application/pdf" in ctype or (
        path_lower.endswith(".pdf") and b"\x00" in body[:512]
    )
    if is_pdf:
        pdf_text = extract_pdf_text(body)
        if len(pdf_text) >= 24:
            text_hex = hashlib.sha256(pdf_text.encode("utf-8")).hexdigest()[:32]
            fp = f"sha256:pdf-text:{text_hex}"
            method = "pdf_text"
        else:
            fp = f"sha256:bytes:{bytes_hex}"
            method = "bytes"
    else:
        # Non-PDF binary payloads fall back to raw bytes digest.
        if b"\x00" in body[:1024]:
            fp = f"sha256:bytes:{bytes_hex}"
            method = "bytes"
        else:
            decoded = body.decode("utf-8", errors="replace")
            norm_text = normalize_html_text(decoded)
            if len(norm_text) >= 16:
                text_hex = hashlib.sha256(norm_text.encode("utf-8")).hexdigest()[:32]
                fp = f"sha256:text:{text_hex}"
                method = "text"
            else:
                fp = f"sha256:bytes:{bytes_hex}"
                method = "bytes"

    return {
        "fingerprint": fp,
        "bytes_sha256": bytes_hex,
        "method": method,
        "etag": hdrs.get("etag", ""),
        "last_modified": hdrs.get("last-modified", ""),
        "content_length": hdrs.get("content-length", "") or str(len(body)),
    }
