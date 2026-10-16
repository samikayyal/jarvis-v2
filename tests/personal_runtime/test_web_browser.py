from __future__ import annotations

import asyncio
import http.server
import json
import os
import socket
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis_personal_runtime.web_browser import (
    BrowserWorker,
    PublicUrlError,
    is_public_address,
    validate_public_url,
)
from jarvis_personal_runtime.web_worker import (
    PublicNetworkError,
    PublicNetworkProxy,
    resolve_public_addresses,
)


def test_public_url_validation_rejects_non_http_credentials_and_private_literals() -> (
    None
):
    assert validate_public_url("HTTPS://Example.COM/articles?q=1#part") == (
        "https://example.com/articles?q=1#part"
    )
    for url in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:secret@example.com/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://169.254.169.254/latest/",
        "https://localhost/",
        "https://service.internal/",
    ):
        with pytest.raises(PublicUrlError):
            validate_public_url(url)


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "fe80::1",
        "224.0.0.1",
    ],
)
def test_address_policy_rejects_special_use_addresses(value: str) -> None:
    assert not is_public_address(value)


def test_dns_policy_rejects_loopback_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
        ],
    )
    with pytest.raises(PublicNetworkError, match="non-public"):
        resolve_public_addresses("public.example", 80)


class _Pipe:
    def __init__(self, lines: list[bytes] | None = None) -> None:
        self.lines = list(lines or [])
        self.writes: list[bytes] = []
        self.closed = False
        self.wait_for_line = asyncio.Event()

    def write(self, value: bytes) -> None:
        self.writes.append(value)

    async def drain(self) -> None:
        return None

    async def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        await self.wait_for_line.wait()
        return b""

    async def read(self, _size: int = -1) -> bytes:
        await asyncio.Event().wait()
        return b""


class _Process:
    def __init__(self, response: dict[str, object] | None = None) -> None:
        self.stdin = _Pipe()
        self.stdout = _Pipe(
            [(json.dumps(response, separators=(",", ":")) + "\n").encode()]
            if response is not None
            else None
        )
        self.stderr = _Pipe()
        self.returncode: int | None = None
        self.pid = os.getpid() + 100_000
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.stdout.wait_for_line.set()

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9
        self.stdout.wait_for_line.set()

    async def wait(self) -> int:
        return self.returncode or 0


def test_browser_worker_is_lazy_and_cleans_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "personal-home"))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-browser")

    async def scenario() -> None:
        process: _Process | None = None
        calls: list[dict[str, Any]] = []

        async def factory(*argv: str, **kwargs: object) -> _Process:
            nonlocal process
            calls.append({"argv": argv, **kwargs})
            process = _Process(
                {
                    "outcome": "ok",
                    "title": "A",
                    "url": "https://example.com/",
                    "text": "Evidence",
                }
            )
            return process

        worker = BrowserWorker(tmp_path, 1, 100, process_factory=factory)
        assert not calls
        page = await worker.read("https://example.com")
        assert page == {
            "outcome": "ok",
            "title": "A",
            "url": "https://example.com/",
            "text": "Evidence",
        }
        assert len(calls) == 1
        assert "--profile-dir" in calls[0]["argv"]
        profile = Path(calls[0]["argv"][calls[0]["argv"].index("--profile-dir") + 1])
        assert profile.is_dir()
        environment = calls[0]["env"]
        anonymous_home = Path(environment["HOME"])
        assert anonymous_home.is_dir()
        assert anonymous_home.is_relative_to(profile)
        assert "OPENAI_API_KEY" not in environment
        (anonymous_home / ".browser-cache").write_text("request-only")
        await worker.close()
        assert process is not None and process.terminated
        assert not profile.exists()
        assert not anonymous_home.exists()

    asyncio.run(scenario())


def test_browser_worker_timeout_is_terminal_and_cleans_profile(tmp_path: Path) -> None:
    async def scenario() -> None:
        process: _Process | None = None

        async def factory(*_args: str, **_kwargs: object) -> _Process:
            nonlocal process
            process = _Process()
            return process

        worker = BrowserWorker(tmp_path, 0.01, 100, process_factory=factory)
        result = await worker.read("https://example.com")
        assert result["outcome"] == "timeout"
        assert not worker.usable
        assert process is not None and process.terminated
        assert list(tmp_path.iterdir()) == []
        unavailable = await worker.read("https://example.com")
        assert unavailable["outcome"] == "unavailable"

    asyncio.run(scenario())


def test_proxy_blocks_private_connect_targets() -> None:
    proxy = PublicNetworkProxy()
    proxy.start()
    try:
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=2) as client:
            client.sendall(b"CONNECT 127.0.0.1:80 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            response = client.recv(4096)
        assert response.startswith(b"HTTP/1.1 403 Forbidden")
        assert proxy.blocked
    finally:
        proxy.close()


def test_repeated_cancellation_finishes_kill_and_removes_profile(tmp_path):
    async def scenario():
        started = asyncio.Event()
        terminating = asyncio.Event()
        exited = asyncio.Event()

        class StubbornProcess(_Process):
            pid = None

            def terminate(self):
                terminating.set()

            def kill(self):
                self.returncode = -9
                exited.set()

            async def wait(self):
                await exited.wait()
                return self.returncode

        process = StubbornProcess()
        process.pid = None

        async def factory(*args, **kwargs):
            started.set()
            return process

        worker = BrowserWorker(tmp_path, 10, 100, process_factory=factory)
        reading = asyncio.create_task(worker.read("https://example.com"))
        await started.wait()
        reading.cancel()
        await terminating.wait()
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        assert process.returncode == -9
        assert list(tmp_path.iterdir()) == []
        await worker.close()

    asyncio.run(scenario())


def test_posix_cleanup_signals_children_after_the_leader_exits(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from jarvis_personal_runtime import web_browser

    async def scenario():
        signals = []
        process = _Process({"outcome": "ok", "url": "https://example.com", "text": "A"})

        async def factory(*args, **kwargs):
            return process

        worker = BrowserWorker(tmp_path, 1, 100, process_factory=factory)
        await worker.read("https://example.com")
        process.returncode = 0
        monkeypatch.setattr(
            web_browser, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9)
        )
        monkeypatch.setattr(
            web_browser,
            "os",
            SimpleNamespace(
                name="posix", killpg=lambda pid, sig: signals.append((pid, sig))
            ),
        )
        await worker.close()
        assert signals == [
            (process.pid, web_browser.signal.SIGTERM),
            (process.pid, web_browser.signal.SIGKILL),
        ]
        assert list(tmp_path.iterdir()) == []

    asyncio.run(scenario())


class _Driver:
    def __init__(self, final_url: str = "https://example.com/") -> None:
        self.final_url = final_url
        self.commands: list[tuple[str, dict[str, object]]] = []
        self.options: list[object] = []
        self.quit_called = False

    def set_page_load_timeout(self, _timeout: float) -> None:
        return None

    def set_script_timeout(self, _timeout: float) -> None:
        return None

    def execute_cdp_cmd(self, name: str, value: dict[str, object]) -> None:
        self.commands.append((name, value))

    def get(self, _url: str) -> None:
        return None

    @property
    def current_url(self) -> str:
        return self.final_url

    @property
    def title(self) -> str:
        return "Fixture"

    def execute_script(self, script: str) -> object:
        if "contentType" in script:
            return "text/html"
        return "Visible fixture text"

    def quit(self) -> None:
        self.quit_called = True


@pytest.mark.parametrize("headed", [False, True])
def test_selenium_reader_configures_sandbox_proxy_and_scheme_blocks(
    tmp_path: Path,
    headed: bool,
) -> None:
    from jarvis_personal_runtime.web_worker import SeleniumPageReader

    driver = _Driver()
    captured: dict[str, object] = {}

    def factory(*, service: object, options: object) -> _Driver:
        captured["service"] = service
        captured["options"] = options
        return driver

    reader = SeleniumPageReader(
        tmp_path / "profile",
        2,
        100,
        browser_binary="/opt/chromium",
        driver_binary="/opt/chromedriver",
        driver_factory=factory,
        headed=headed,
    )
    try:
        result = reader.read("https://example.com")
        assert result["outcome"] == "ok"
        arguments = set(getattr(captured["options"], "arguments", ()))
        assert ("--headless=new" in arguments) is not headed
        assert ("--disable-blink-features=AutomationControlled" in arguments) is headed
        assert "--enable-sandbox" in arguments
        assert "--no-sandbox" not in arguments
        assert any(
            argument.startswith("--proxy-server=http://127.0.0.1:")
            for argument in arguments
        )
        assert any(argument.startswith("--user-data-dir=") for argument in arguments)
        blocked = next(
            value for name, value in driver.commands if name == "Network.setBlockedURLs"
        )
        blocked_urls = cast(list[str], blocked["urls"])
        assert "file://*" in blocked_urls
        assert "data:*" in blocked_urls
        assert "wss://*" in blocked_urls
    finally:
        reader.close()
    assert driver.quit_called


def test_selenium_reader_does_not_return_a_private_redirect(tmp_path: Path) -> None:
    from jarvis_personal_runtime.web_worker import SeleniumPageReader

    driver = _Driver("https://127.0.0.1/")
    reader = SeleniumPageReader(
        tmp_path / "profile",
        2,
        100,
        browser_binary="/opt/chromium",
        driver_binary="/opt/chromedriver",
        driver_factory=lambda **_kwargs: driver,
    )
    try:
        result = reader.read("https://example.com")
    finally:
        reader.close()
    assert result["outcome"] == "blocked"
    assert result["url"] == "https://example.com/"


class _SignalDriver(_Driver):
    def __init__(self, *, password: bool = False, challenge: bool = False) -> None:
        super().__init__()
        self.password = password
        self.challenge = challenge

    def execute_script(self, script: str) -> object:
        if "contentType" in script:
            return "text/html"
        if "password:" in script:
            return {
                "password": self.password,
                "challenge": self.challenge,
                "body": "Public text",
            }
        return "Public text"


class _SearchDriver(_Driver):
    page_source = '<div id="search"><div class="g"><a href="https://example.com/source"><h3>Primary source</h3></a><div class="VwiC3b">Fresh evidence.</div></div></div>'

    def __init__(self, state="results"):
        super().__init__("https://www.google.com/search?q=news")
        self.state = state
        self.visits = []
        self.clicks = 0
        if state == "traffic":
            self.page_source = "<html>Our systems have detected unusual traffic</html>"

    def get(self, url):
        self.visits.append(url)
        if self.state in {"consent", "unknown_consent"}:
            self.final_url = "https://consent.google.com/m?continue=search"
        elif self.state == "interstitial":
            self.final_url = "https://www.google.com/sorry/index"
        elif self.state == "javascript_required":
            self.final_url = "https://www.google.com/httpservice/retry/enablejs"

    def find_elements(self, by, selector):
        assert by == "css selector"
        assert "Reject all" in selector
        return [self] if self.state == "consent" else []

    def click(self):
        self.clicks += 1
        self.final_url = "https://www.google.com/search?q=news"

    def execute_script(self, script):
        if "contentType" in script:
            return "text/html"
        if "password:" in script:
            return {
                "password": False,
                "challenge": self.state == "captcha",
                "body": "Our systems have detected unusual traffic"
                if self.state in {"traffic", "traffic_article"}
                else "Primary source Fresh evidence.",
            }
        return "Search results"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("results", "ok"),
        ("consent", "ok"),
        ("unknown_consent", "blocked"),
        ("captcha", "blocked"),
        ("traffic", "blocked"),
        ("traffic_article", "ok"),
        ("interstitial", "blocked"),
        ("javascript_required", "blocked"),
    ],
)
def test_selenium_search_results_consent_and_blocking(tmp_path, state, expected):
    from jarvis_personal_runtime.web_worker import SeleniumPageReader

    driver = _SearchDriver(state)
    reader = SeleniumPageReader(
        tmp_path / "profile",
        2,
        100,
        browser_binary="/opt/chromium",
        driver_binary="/opt/chromedriver",
        driver_factory=lambda **kwargs: driver,
    )
    try:
        result = reader.search("latest أخبار", 5)
    finally:
        reader.close()
    assert result["outcome"] == expected
    assert driver.clicks == (1 if state == "consent" else 0)
    assert len(driver.visits) == 2  # reset and exactly one query navigation
    assert "q=latest+%D8%A3%D8%AE%D8%A8%D8%A7%D8%B1" in driver.visits[1]
    if expected == "ok":
        assert result["results"][0]["snippet"] == "Fresh evidence."
    if state == "javascript_required":
        assert result["reason"] == "Google requires JavaScript to search"
    if state == "interstitial":
        assert result["reason"] == "Google blocked automated search traffic"
    assert driver.quit_called


def test_search_enables_only_google_scripts_and_page_read_disables_them(tmp_path):
    from jarvis_personal_runtime.web_worker import SeleniumPageReader

    driver = _SearchDriver()
    captured = {}

    def factory(*, service, options):
        captured["options"] = options
        return driver

    reader = SeleniumPageReader(
        tmp_path / "profile",
        2,
        100,
        browser_binary="/opt/chromium",
        driver_binary="/opt/chromedriver",
        driver_factory=factory,
    )
    try:
        assert reader.search("news", 5)["outcome"] == "ok"
        options = captured["options"]
        assert "--disable-javascript" not in options.arguments
        prefs = options.experimental_options["prefs"]
        assert prefs["profile.default_content_setting_values.javascript"] == 2
        assert prefs["profile.content_settings.exceptions.javascript"] == {
            "https://www.google.com:443,*": {"setting": 1},
            "https://consent.google.com:443,*": {"setting": 1},
        }
        commands = [
            value
            for name, value in driver.commands
            if name == "Emulation.setScriptExecutionDisabled"
        ]
        assert commands[-1] == {"value": False}
        reader.read("https://example.com/")
        commands = [
            value
            for name, value in driver.commands
            if name == "Emulation.setScriptExecutionDisabled"
        ]
        assert commands[-1] == {"value": True}
    finally:
        reader.close()


def test_browser_worker_search_uses_owned_process_and_cleans_up(tmp_path):
    async def scenario():
        process = _Process({"outcome": "no_results", "results": []})

        async def factory(*args, **kwargs):
            return process

        worker = BrowserWorker(tmp_path, 1, 100, process_factory=factory)
        assert await worker.search("news", 5) == {
            "outcome": "no_results",
            "results": [],
        }
        assert json.loads(process.stdin.writes[0]) == {
            "op": "search",
            "query": "news",
            "limit": 5,
        }
        await worker.close()
        assert process.terminated
        assert not list(tmp_path.iterdir())

    asyncio.run(scenario())


def test_browser_worker_search_timeout_is_terminal(tmp_path):
    async def scenario():
        process = _Process()

        async def factory(*args, **kwargs):
            return process

        worker = BrowserWorker(tmp_path, 0.01, 100, process_factory=factory)
        assert (await worker.search("news", 5))["outcome"] == "timeout"
        assert process.terminated
        assert not worker.usable
        assert not list(tmp_path.iterdir())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("password", "challenge", "outcome"),
    [(True, False, "unsupported"), (False, True, "blocked")],
)
def test_reader_discloses_auth_and_challenge_boundaries(
    tmp_path: Path, password: bool, challenge: bool, outcome: str
) -> None:
    from jarvis_personal_runtime.web_worker import SeleniumPageReader

    reader = SeleniumPageReader(
        tmp_path / "profile",
        2,
        100,
        browser_binary="/opt/chromium",
        driver_binary="/opt/chromedriver",
        driver_factory=lambda **_kwargs: _SignalDriver(
            password=password, challenge=challenge
        ),
    )
    try:
        result = reader.read("https://example.com")
    finally:
        reader.close()
    assert result["outcome"] == outcome


@pytest.mark.skipif(
    not os.environ.get("JARVIS_TEST_CHROMIUM_BINARY")
    or not os.environ.get("JARVIS_TEST_CHROMEDRIVER_BINARY"),
    reason="compatible Chromium and ChromeDriver paths were not provided",
)
def test_real_browser_fixture_extracts_and_blocks_private_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise Selenium and the proxy when a compatible browser is available."""

    from jarvis_personal_runtime import web_worker

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{self.server.server_port}/private"
                )
                self.end_headers()
                return
            if self.path in {"/public-redirect", "/private-dns-redirect"}:
                host = (
                    "example.com"
                    if self.path == "/public-redirect"
                    else "private.example"
                )
                self.send_response(302)
                self.send_header(
                    "Location", f"http://{host}:{self.server.server_port}/target"
                )
                self.end_headers()
                return
            if self.path == "/download":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header(
                    "Content-Disposition", 'attachment; filename="file.bin"'
                )
                self.end_headers()
                self.wfile.write(b"download contents")
                return
            body = (
                b"<html><head><title>Fixture page</title></head>"
                b"<body><h1>Fixture evidence</h1><p>Public text.</p>"
                + f'<img src="http://127.0.0.1:{self.server.server_port}/private">'.encode()
                + b"<p style='display:none'>Hidden secret</p>"
                b"<script>document.body.innerText='Script replaced text';</script>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    original_resolve = web_worker.resolve_public_addresses
    original_getaddrinfo = socket.getaddrinfo

    def fixture_dns(host, port, *args, **kwargs):
        if host == "private.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fixture_dns)
    monkeypatch.setattr(
        web_worker,
        "resolve_public_addresses",
        lambda host, port: (
            ("127.0.0.1",) if host == "example.com" else original_resolve(host, port)
        ),
    )
    reader = web_worker.SeleniumPageReader(
        tmp_path / "profile",
        10,
        1000,
        browser_binary=os.environ["JARVIS_TEST_CHROMIUM_BINARY"],
        driver_binary=os.environ["JARVIS_TEST_CHROMEDRIVER_BINARY"],
    )
    port = server.server_address[1]
    try:
        page = reader.read(f"http://example.com:{port}/")
        public_redirect = reader.read(f"http://example.com:{port}/public-redirect")
        private_dns = reader.read(f"http://example.com:{port}/private-dns-redirect")
        blocked = reader.read(f"http://example.com:{port}/redirect")
        download = reader.read(f"http://example.com:{port}/download")
    finally:
        reader.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    assert page["outcome"] == "ok"
    assert page["title"] == "Fixture page"
    assert "Public text." in str(page["text"])
    assert "Hidden secret" not in str(page["text"])
    assert "Script replaced" not in str(page["text"])
    assert blocked["outcome"] == "blocked"
    assert public_redirect["outcome"] == "ok"
    assert public_redirect["url"] == f"http://example.com:{port}/target"
    assert private_dns["outcome"] == "blocked"
    assert hits.count("/target") == 1
    assert blocked["url"] == f"http://example.com:{port}/redirect"
    assert "/private" not in hits
    assert download["outcome"] in {"unsupported", "unavailable"}
    assert not download["text"]
