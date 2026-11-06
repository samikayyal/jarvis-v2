"""Opt-in real browser fallback against an owned local page, including Windows.

Exercises SeleniumPageReader itself; POSIX worker process isolation remains covered
by test_web_installed_browser.py. No provider calls or personal browser profile.
"""

import asyncio
import http.server
import json
import os
import threading

import pytest

from jarvis_personal_runtime import web_worker

from .test_page_reader import Responses, answer, call, create, setup
from .test_page_reader_coverage import unresolved


@pytest.mark.skipif(
    not os.environ.get("JARVIS_TEST_LOCAL_BROWSER"),
    reason="opt-in local Chromium fixture",
)
def test_reader_fallback_reveals_billing_tab_in_real_browser(tmp_path, monkeypatch):
    from selenium.webdriver.common.selenium_manager import SeleniumManager

    binaries = SeleniumManager().binary_paths(["--browser", "chrome"])

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"""<h1>Product overview</h1><p>The price is one dollar per hour.</p>
            <button role="tab" type="button" aria-controls="billing" onclick="document.getElementById('billing').style.display='block'">Billing</button>
            <div id="billing" style="display:none"><h2>Conditions</h2><p>Minimum billing duration is sixty seconds.</p></div>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = web_worker.resolve_public_addresses
    monkeypatch.setattr(
        web_worker,
        "resolve_public_addresses",
        lambda host, port: (
            ("127.0.0.1",) if host == "fixture.example" else original(host, port)
        ),
    )

    class Browser:
        def __init__(self):
            self.calls = 0

        async def capture(self, url, display):
            self.calls += 1
            engine = web_worker.SeleniumPageReader(
                tmp_path / f"profile-{self.calls}",
                30,
                100000,
                browser_binary=binaries["browser_path"],
                driver_binary=binaries["driver_path"],
                rendered=True,
            )
            try:
                return await asyncio.to_thread(
                    engine.read, url, structured=True, display=display
                )
            finally:
                engine.close()

        async def close(self):
            pass

    async def scenario():
        model = Responses(
            unresolved(display_checked=False),
            call("display_page", control_id="0", option=""),
            answer(chunk="2", quote="Minimum billing duration is sixty seconds."),
        )
        browser = Browser()
        readers = setup(tmp_path, model, browser)
        result = await create(
            readers,
            "Minimum billing duration?",
            url=f"http://fixture.example:{server.server_port}/",
        )
        assert result["answered"], result
        assert browser.calls == 2
        assert "sixty seconds" not in json.dumps(model.requests[0])
        assert "sixty seconds" in result["evidence"][0]["quote"]
        assert "Billing" in " ".join(result["evidence"][0]["display_state"])
        assert len(result["coverage"]["captures"]) == 2

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
