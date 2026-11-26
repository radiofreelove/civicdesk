from __future__ import annotations

import functools
import http.server
import resource
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"


class QuietSiteHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128


@pytest.fixture(scope="session")
def built_site() -> Path:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build.py")], cwd=ROOT, check=True)
    return PUBLIC


@pytest.fixture(scope="session")
def site_server(built_site: Path) -> str:
    handler = functools.partial(QuietSiteHandler, directory=str(built_site))
    httpd = ThreadingTCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(scope="session")
def playwright_session():
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(hard, 65536) if hard != resource.RLIM_INFINITY else 65536
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError):
        pass

    with sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="function")
def browser_instance(playwright_session):
    browser = playwright_session.chromium.launch(args=["--disable-dev-shm-usage"])
    try:
        yield browser
    finally:
        browser.close()
