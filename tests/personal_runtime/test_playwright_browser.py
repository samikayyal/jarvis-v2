"""Real Chromium at the public reader browser boundary; no provider calls."""

import asyncio
import http.server
import threading
from contextlib import contextmanager, suppress

from playwright.async_api import Error

from jarvis_personal_runtime import web_worker
from jarvis_personal_runtime.playwright_browser import PlaywrightBrowser


@contextmanager
def public_fixture(monkeypatch, *, slow_page=None):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            query = self.rfile.read(length).decode()
            payload = ("<h1>Public search result</h1><p>" + query + "</p>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/slow" and slow_page is not None:
                slow_page[0].set()
                slow_page[1].wait(20)
            if self.path == "/redirect-private":
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{self.server.server_port}/"
                )
                self.end_headers()
                return
            body = f"""<h1>Public calculator</h1>
            <label>Region<select onchange="document.querySelector('#price').textContent=this.value">
            <option>US</option><option>EU</option></select></label><p id="price">US</p>
            <table><tr><th>Machine</th><th>Price</th></tr><tr><td>small</td><td>2</td></tr></table>
            <label>Search<input type="search" oninput="document.querySelector('#result').textContent=this.value"></label><p id="result"></p>
            <a href="http://other.example:{self.server.server_port}/next">Other domain</a>
            <a target="_blank" href="/popup">Open tab</a>
            <button onclick="document.querySelector('#price').textContent='Calculated'">Calculate</button>
            <button onclick="fetch('http://127.0.0.1:{self.server.server_port}/secret')">Unsafe fetch</button>
            <label>Password<input type="password"></label><button>Sign in</button>
            <a href="/redirect-private">Private redirect</a><a href="/large">Large page</a>
            """
            if slow_page is not None:
                body += '<a href="/slow">Slow page</a>'
            if self.path == "/large":
                body = (
                    "<h1>Large source</h1>"
                    + "<p>Preserved data row.</p>" * 5000
                    + "<p>Needle at the end.</p>"
                )
            if self.path == "/visual":
                body = """<h1>Visual fixture</h1><canvas width="900" height="250"></canvas>
                <script>const c=document.querySelector('canvas').getContext('2d');
                c.fillStyle='white'; c.fillRect(0,0,900,250); c.fillStyle='black';
                c.font='bold 85px Arial'; c.fillText('ORCHID 7391',30,150);</script>"""
            if self.path == "/search-form":
                body = """<form role="search" method="post" action="/search-form">
                <label>Site query<input type="search" name="q"></label>
                <button type="submit">Search site</button></form>"""
            if self.path == "/unrelated-write":
                body = f"""<h1>Public preview</h1><button onclick="fetch('http://other.example:{self.server.server_port}/create',
                {{method:'POST',body:'resource=yes'}})">Search preview</button>
                <form role="search" method="post" action="/create-resource">
                <button type="submit">Create resource</button></form>"""
            if self.path == "/set-state":
                body = """<h1>State fixture</h1><script>document.cookie='research=one';
                localStorage.setItem('private-research-state','one');</script>
                <a href="/show-state">Show state</a><a target="_blank" href="/popup">Open tab</a>"""
            if self.path == "/show-state":
                body = """<h1>State fixture</h1><p id="state"></p><script>
                document.querySelector('#state').textContent = document.cookie +
                (localStorage.getItem('private-research-state') || 'EMPTY');</script>"""
            payload = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            with suppress(
                BrokenPipeError, ConnectionResetError, ConnectionAbortedError
            ):
                self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = web_worker.resolve_public_addresses

    def resolve(host, port):
        if host in {"fixture.example", "other.example"} and port == server.server_port:
            return ("127.0.0.1",)
        return original(host, port)

    monkeypatch.setattr(web_worker, "resolve_public_addresses", resolve)
    try:
        yield f"http://fixture.example:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def element(observation, label):
    return next(e["ref"] for e in observation["elements"] if e["label"] == label)


def test_dynamic_controls_and_complete_readable_observations(monkeypatch):
    async def scenario(url):
        browser = PlaywrightBrowser(timeout_seconds=5)
        try:
            initial = await browser.open(url)
            assert "# Public calculator" in initial["text"]
            assert "| Machine | Price |" in initial["text"]
            assert "[Other domain](http://other.example:" in initial["text"]
            changed = await browser.act(
                "select", {"ref": element(initial, "Region"), "value": "EU"}
            )
            assert "EU" in changed["text"]
            typed = await browser.act(
                "type", {"ref": element(changed, "Search"), "text": "discount"}
            )
            assert "discount" in typed["text"]
            clicked = await browser.act("click", {"ref": element(typed, "Calculate")})
            assert "Calculated" in clicked["text"]
            assert not any(
                e["label"] in {"Password", "Sign in"} for e in clicked["elements"]
            )
            assert "image" not in clicked
        finally:
            await browser.close()
        assert browser.closed

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_cross_domain_history_tabs_and_explicit_screenshot(monkeypatch):
    async def scenario(url):
        browser = PlaywrightBrowser(timeout_seconds=5)
        try:
            initial = await browser.open(url)
            popup = await browser.act("click", {"ref": element(initial, "Open tab")})
            assert popup["url"].endswith("/popup"), popup
            tabs = (await browser.act("tabs", {}))["tabs"]
            assert len(tabs) == 2
            original_tab = next(t["id"] for t in tabs if not t["active"])
            popup_tab = next(t["id"] for t in tabs if t["active"])
            current = await browser.act("switch_tab", {"tab_id": original_tab})
            cross = await browser.act(
                "click", {"ref": element(current, "Other domain")}
            )
            assert "other.example" in cross["url"]
            back = await browser.act("back", {})
            assert back["url"] == url
            forward = await browser.act("forward", {})
            assert "other.example" in forward["url"]
            reloaded = await browser.act("reload", {})
            assert reloaded["url"] == forward["url"]
            shot = await browser.act("screenshot", {})
            assert shot["image"].startswith("data:image/png;base64,iVBOR")
            scrolled = await browser.act("scroll", {"pixels": 500})
            assert "scroll_y" in scrolled and "image" not in scrolled
            await browser.act("close_tab", {"tab_id": popup_tab})
            assert len((await browser.act("tabs", {}))["tabs"]) == 1
            assert "error" in await browser.act("close_tab", {"tab_id": original_tab})
        finally:
            await browser.close()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_private_redirects_subresources_and_stale_refs_fail_clearly(monkeypatch):
    async def scenario(url):
        browser = PlaywrightBrowser(timeout_seconds=5)
        try:
            initial = await browser.open(url)
            stale = element(initial, "Calculate")
            fresh = await browser.act("inspect", {})
            failure = await browser.act("click", {"ref": stale})
            assert "Stale" in failure["error"]
            blocked = await browser.act(
                "click", {"ref": element(fresh, "Unsafe fetch")}
            )
            assert blocked["blocked_requests"]
            redirect = await browser.act(
                "click", {"ref": element(blocked, "Private redirect")}
            )
            assert "error" in redirect
            assert redirect["uncertain"] is True
        finally:
            await browser.close()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_browser_crash_discloses_lost_state_and_closes(monkeypatch):
    # CDP is used solely by the fixture to provoke an actual Chromium crash;
    # neither the reader nor main Jarvis receives an arbitrary-code interface.
    from jarvis_personal_runtime.playwright_browser import BrowserStateLost

    async def scenario(url):
        browser = PlaywrightBrowser(timeout_seconds=5)
        try:
            await browser.open(url)
            async with browser._page.expect_event("crash", timeout=3000):
                with suppress(Error):
                    await browser._page.goto("chrome://crash", timeout=3000)
            try:
                await browser.act("inspect", {})
            except BrowserStateLost as exc:
                assert "lost" in str(exc)
            else:
                raise AssertionError(
                    "A crashed page must disclose lost interactive state"
                )
        finally:
            await browser.close()
        assert browser.closed

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_public_search_form_can_submit_without_account_authority(monkeypatch):
    async def scenario(url):
        browser = PlaywrightBrowser(timeout_seconds=5)
        try:
            initial = await browser.open(url + "search-form")
            typed = await browser.act(
                "type", {"ref": element(initial, "Site query"), "text": "discount"}
            )
            result = await browser.act("click", {"ref": element(typed, "Search site")})
            assert "Public search result" in result["text"], result
            assert "q=discount" in result["text"]
        finally:
            await browser.close()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_anonymous_context_does_not_inherit_previous_browser_state(monkeypatch):
    async def scenario(url):
        first, second = PlaywrightBrowser(), PlaywrightBrowser()
        try:
            await first.open(url + "set-state")
            fresh = await second.open(url + "show-state")
            assert "EMPTY" in fresh["text"]
            assert "research=one" not in fresh["text"]
        finally:
            await first.close()
            await second.close()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_research_click_cannot_authorize_unrelated_posts_or_resource_creation(
    monkeypatch,
):
    async def scenario(url):
        browser = PlaywrightBrowser(timeout_seconds=5)
        try:
            initial = await browser.open(url + "unrelated-write")
            assert not any(e["label"] == "Create resource" for e in initial["elements"])
            result = await browser.act(
                "click", {"ref": element(initial, "Search preview")}
            )
            assert result["blocked_requests"], result
        finally:
            await browser.close()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))
