"""Synchronous Selenium worker used by :class:`BrowserWorker`.

This module is a child-process entry point.  It is intentionally not exposed
as a prepared tool or a terminal command.  The parent sends one JSON request at
a time; this process owns the Selenium session, a fresh profile, and a local
restrictive HTTP proxy.  The proxy validates every HTTP request/CONNECT and
pinned DNS answer, so redirects cannot turn a public URL into a private one.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import select
import shutil
import socket
import socketserver
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .web_browser import PublicUrlError, is_public_address, validate_public_url


class PublicNetworkError(RuntimeError):
    """DNS or connection policy rejected a destination."""


def resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve ``host`` and reject the complete answer if any address is private.

    Rejecting a mixed public/private answer matters: choosing the first answer
    would let resolver ordering decide whether an internal destination wins.
    The caller then connects to one of the validated literal addresses, which
    prevents a DNS answer from changing after validation.
    """

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PublicNetworkError("public host did not resolve") from exc
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        address = str(sockaddr[0])
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise PublicNetworkError("resolver returned an invalid address") from exc
        if not is_public_address(parsed):
            raise PublicNetworkError("resolver returned a non-public address")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise PublicNetworkError("public host did not resolve")
    return tuple(addresses)


def connect_public(host: str, port: int, *, timeout: float = 10.0) -> socket.socket:
    """Connect to a validated literal address while preserving the Host/SNI."""

    addresses = resolve_public_addresses(host, port)
    last_error: OSError | None = None
    for address in addresses:
        try:
            connection = socket.create_connection((address, port), timeout=timeout)
            connection.settimeout(timeout)
            return connection
        except OSError as exc:
            last_error = exc
    raise PublicNetworkError("public host could not be reached") from last_error


def _authority(value: str, default_port: int) -> tuple[str, int]:
    """Parse an HTTP CONNECT authority without accepting userinfo."""

    if "@" in value:
        raise PublicNetworkError("proxy authority contains credentials")
    try:
        parsed = urlsplit(f"http://{value}")
        host = parsed.hostname
        port = parsed.port or default_port
    except ValueError as exc:
        raise PublicNetworkError("proxy authority is malformed") from exc
    if host is None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise PublicNetworkError("proxy authority is malformed")
    # The lexical validator also rejects private literals and local names.  A
    # dummy path turns a CONNECT authority into a normal URL for validation.
    validate_public_url(f"https://{value}/")
    return host, port


def _relay(
    left: socket.socket, right: socket.socket, *, idle_timeout: float = 30.0
) -> None:
    """Relay two sockets with a bounded idle period and no DNS involvement."""

    sockets = (left, right)
    deadline = time.monotonic() + idle_timeout
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select(sockets, (), (), 0.5)
            if not readable:
                continue
            moved = False
            for source in readable:
                target = right if source is left else left
                try:
                    data = source.recv(65_536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    target.sendall(data)
                except OSError:
                    return
                moved = True
            if moved:
                deadline = time.monotonic() + idle_timeout
    finally:
        for connection in sockets:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                connection.close()


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        self.blocked = threading.Event()
        self.blocked_reason = ""
        self.blocked_destinations: set[tuple[str | None, int]] = set()
        super().__init__(("127.0.0.1", 0), _ProxyHandler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def record_block(self, reason: str, target: str = "") -> None:
        self.blocked_reason = reason[:256]
        self.blocked.set()
        try:
            parsed = urlsplit(target if "://" in target else f"https://{target}")
            self.blocked_destinations.add(
                (
                    parsed.hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                )
            )
        except ValueError:
            pass


class _ProxyHandler(socketserver.StreamRequestHandler):
    server: _ProxyServer

    def handle(self) -> None:
        self.request.settimeout(10)
        try:
            first = self.rfile.readline(8192)
            if not first:
                return
            decoded = first.decode("latin-1").strip().split()
            if len(decoded) != 3:
                self._deny("malformed proxy request")
                return
            method, target, version = decoded
            self.target = target
            if method.upper() == "CONNECT":
                self._connect_tunnel(target)
                return
            self._forward_http(method, target, version)
        except (OSError, PublicNetworkError, PublicUrlError, ValueError):
            # The browser receives a normal deny response where possible.  A
            # malformed or disconnected request must not take down the proxy.
            with contextlib.suppress(OSError):
                self._deny("request outside public HTTP(S) scope")

    def _headers(self) -> list[tuple[str, str]]:
        headers: list[tuple[str, str]] = []
        total = 0
        while True:
            line = self.rfile.readline(8192)
            if not line:
                break
            total += len(line)
            if total > 65_536:
                raise PublicNetworkError("proxy headers exceed limit")
            decoded = line.decode("latin-1").rstrip("\r\n")
            if not decoded:
                break
            if ":" not in decoded:
                raise PublicNetworkError("malformed proxy header")
            name, value = decoded.split(":", 1)
            headers.append((name.strip(), value.lstrip()))
        return headers

    def _connect_tunnel(self, target: str) -> None:
        host, port = _authority(target, 443)
        try:
            upstream = connect_public(host, port)
        except (PublicNetworkError, PublicUrlError) as exc:
            self.server.record_block(str(exc))
            self._deny("destination is outside public scope")
            return
        self._headers()
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        _relay(self.connection, upstream)

    def _forward_http(self, method: str, target: str, version: str) -> None:
        try:
            canonical = validate_public_url(target)
            parsed = urlsplit(canonical)
            host = parsed.hostname
            if host is None:
                raise PublicNetworkError("HTTP target has no host")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if parsed.scheme != "http":
                raise PublicNetworkError("HTTPS must use CONNECT")
            upstream = connect_public(host, port)
        except (PublicNetworkError, PublicUrlError) as exc:
            self.server.record_block(str(exc))
            self._deny("destination is outside public scope")
            return
        headers = self._headers()
        # Forward origin-form requests.  Hop-by-hop proxy headers are dropped;
        # the browser remains responsible for the Host and normal HTTP headers.
        forwarded = [
            f"{method} {parsed.path or '/'}"
            + (f"?{parsed.query}" if parsed.query else "")
            + f" {version}\r\n"
        ]
        saw_host = False
        for name, value in headers:
            lowered = name.casefold()
            if lowered in {
                "proxy-connection",
                "connection",
                "keep-alive",
                "transfer-encoding",
            }:
                continue
            if lowered == "host":
                saw_host = True
            forwarded.append(f"{name}: {value}\r\n")
        if not saw_host:
            forwarded.append(f"Host: {host}\r\n")
        forwarded.append("Connection: close\r\n\r\n")
        upstream.sendall("".join(forwarded).encode("latin-1", "replace"))
        _relay(self.connection, upstream)

    def _deny(self, reason: str) -> None:
        self.server.record_block(reason, getattr(self, "target", ""))
        with contextlib.suppress(OSError):
            self.wfile.write(
                b"HTTP/1.1 403 Forbidden\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            self.wfile.flush()


class PublicNetworkProxy:
    """A localhost proxy that permits public HTTP(S) only."""

    def __init__(self) -> None:
        self._server: _ProxyServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("proxy is not started")
        return self._server.port

    @property
    def blocked(self) -> bool:
        return bool(self._server and self._server.blocked.is_set())

    @property
    def blocked_reason(self) -> str:
        return self._server.blocked_reason if self._server else ""

    def blocked_url(self, url: str) -> bool:
        if self._server is None:
            return False
        parsed = urlsplit(url)
        destination = (
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
        return destination in self._server.blocked_destinations

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = _ProxyServer()
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="jarvis-web-proxy",
            daemon=True,
        )
        self._thread.start()

    def clear_block(self) -> None:
        if self._server is not None:
            self._server.blocked.clear()
            self._server.blocked_reason = ""
            self._server.blocked_destinations.clear()

    def close(self) -> None:
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=1)


def _find_executable(candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and (path.stat().st_mode & 0o111 or sys.platform == "win32"):
            return str(path)
    return None


def find_chromium() -> str | None:
    """Find an explicitly installed Chromium-compatible executable."""

    candidates = (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    )
    return _find_executable(candidates)


def find_chromedriver() -> str | None:
    return _find_executable(
        (
            "/usr/bin/chromedriver",
            "/usr/local/bin/chromedriver",
            "C:\\Program Files\\ChromeDriver\\chromedriver.exe",
            "C:\\Program Files (x86)\\ChromeDriver\\chromedriver.exe",
        )
    )


class SeleniumPageReader:
    """Synchronous page reader and browser restriction setup."""

    def __init__(
        self,
        profile_dir: Path,
        timeout: float,
        max_chars: int,
        *,
        browser_binary: str | None = None,
        driver_binary: str | None = None,
        driver_factory: Any | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.timeout = timeout
        self.max_chars = max_chars
        self.browser_binary = browser_binary or find_chromium()
        self.driver_binary = driver_binary or find_chromedriver()
        self.driver_factory = driver_factory
        self.proxy = PublicNetworkProxy()
        self.driver: Any | None = None
        self.unusable = False

    def _start(self) -> None:
        if self.driver is not None:
            return
        if self.browser_binary is None:
            raise RuntimeError("Chromium executable is unavailable")
        if self.driver_binary is None:
            raise RuntimeError("ChromeDriver executable is unavailable")
        if self.driver_factory is None and not Path(self.browser_binary).is_file():
            raise RuntimeError("configured Chromium executable is unavailable")
        if self.driver_factory is None and not Path(self.driver_binary).is_file():
            raise RuntimeError("configured ChromeDriver executable is unavailable")
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except ImportError as exc:
            raise RuntimeError("Selenium is not installed") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.proxy.start()
        options = Options()
        options.binary_location = self.browser_binary
        for argument in (
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-extensions",
            "--disable-sync",
            "--disable-quic",
            "--dns-prefetch-disable",
            "--disable-javascript",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--enable-sandbox",
            "--proxy-bypass-list=<-loopback>",
            f"--proxy-server=http://127.0.0.1:{self.proxy.port}",
            f"--user-data-dir={self.profile_dir}",
        ):
            options.add_argument(argument)
        options.add_experimental_option(
            "prefs",
            {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
                "autofill.profile_enabled": False,
                "autofill.credit_card_enabled": False,
                "profile.default_content_setting_values.javascript": 2,
                "download.default_directory": str(self.profile_dir / "downloads"),
                "download.prompt_for_download": True,
                "download.directory_upgrade": False,
                "plugins.always_open_pdf_externally": False,
            },
        )
        service = Service(executable_path=self.driver_binary)
        factory = self.driver_factory or webdriver.Chrome
        try:
            self.driver = factory(service=service, options=options)
            self.driver.set_page_load_timeout(self.timeout)
            self.driver.set_script_timeout(self.timeout)
            self._configure_cdp()
        except Exception:
            self.unusable = True
            self.close()
            raise

    def _configure_cdp(self) -> None:
        driver = self.driver
        if driver is None or not hasattr(driver, "execute_cdp_cmd"):
            raise RuntimeError("browser does not expose the required CDP controls")
        # These controls stop non-HTTP schemes before a page's script can use
        # them.  HTTP(S) address restrictions are enforced by the proxy.
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd(
            "Network.setBlockedURLs",
            {
                "urls": [
                    "file://*",
                    "filesystem:*",
                    "data:*",
                    "blob:*",
                    "ftp://*",
                    "ws://*",
                    "wss://*",
                ]
            },
        )
        try:
            driver.execute_cdp_cmd(
                "Browser.setDownloadBehavior",
                {"behavior": "deny"},
            )
        except Exception:  # noqa: BLE001 - older Chromium exposes this on Page.
            try:
                driver.execute_cdp_cmd(
                    "Page.setDownloadBehavior",
                    {"behavior": "deny"},
                )
            except Exception as exc:
                raise RuntimeError("browser download blocking is unavailable") from exc

    def search(self, query: str, limit: int) -> dict[str, object]:
        from urllib.parse import urlencode

        from .web_search import MAX_QUERY_LENGTH, extract_google_results

        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > MAX_QUERY_LENGTH
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
        ):
            return {"outcome": "extraction_failed", "results": []}
        url = "https://www.google.com/search?" + urlencode({"q": query, "num": limit})
        page = self.read(url, search=True)
        if page["outcome"] != "ok":
            return {"outcome": page["outcome"], "results": []}
        try:
            assert self.driver is not None
            return extract_google_results(
                self.driver.page_source, limit, self.max_chars
            )
        except Exception:  # noqa: BLE001 - extraction must not masquerade as an empty search.
            return {"outcome": "extraction_failed", "results": []}

    def read(self, url: str, *, search: bool = False) -> dict[str, object]:
        requested = validate_public_url(url)
        try:
            self._start()
            assert self.driver is not None
            # Clear the prior document before each navigation. A denied
            # download can leave the previous document/current_url visible in
            # some Chromium versions, which would falsely reuse old evidence.
            try:
                self.driver.get("about:blank")
            except Exception:  # noqa: BLE001 - a failed reset makes the worker unsafe.
                self.unusable = True
                return self._result(
                    "unavailable", requested, reason="browser navigation reset failed"
                )
            self.proxy.clear_block()
            try:
                self.driver.get(requested)
            except Exception as exc:  # noqa: BLE001 - Selenium has many version-specific exception types.
                if self.proxy.blocked:
                    return self._blocked(requested)
                if self._is_timeout(exc):
                    with contextlib.suppress(Exception):
                        self.driver.execute_script("window.stop();")
                    return self._result(
                        "timeout", requested, reason="page load timed out"
                    )
                self.unusable = True
                return self._result(
                    "unavailable", requested, reason="browser navigation failed"
                )
            # The proxy also sees denied background connections and subresources.
            # Those must stay denied without discarding a successfully loaded
            # public document. Failed top-level navigation is handled above;
            # private redirects and empty deny responses are checked below.
            if search:
                # Only the known optional-cookie rejection control is allowed.
                # A challenge or an unknown interstitial never triggers clicks.
                current = urlsplit(str(self.driver.current_url))
                if current.hostname == "consent.google.com":
                    from selenium.webdriver.common.by import By

                    buttons = self.driver.find_elements(
                        By.CSS_SELECTOR,
                        'button[aria-label="Reject all"], input[type="submit"][value="Reject all"]',
                    )
                    if len(buttons) != 1:
                        return self._result(
                            "blocked", requested, reason="unrecognized consent screen"
                        )
                    buttons[0].click()
                    current = urlsplit(str(self.driver.current_url))
                if (
                    current.hostname not in {"www.google.com", "google.com"}
                    or current.path != "/search"
                ):
                    return self._result(
                        "blocked", requested, reason="Google search interstitial"
                    )
            final_url = str(getattr(self.driver, "current_url", requested) or requested)
            try:
                final_url = validate_public_url(final_url)
            except PublicUrlError:
                if final_url.startswith(("about:blank", "chrome://downloads")):
                    return self._result(
                        "unsupported",
                        requested,
                        reason="navigation did not produce an HTML page",
                    )
                return self._blocked(requested)
            if self.proxy.blocked_url(final_url):
                return self._blocked(requested)
            content_type = str(
                self.driver.execute_script("return document.contentType || '';") or ""
            ).casefold()
            if content_type not in {"text/html", "application/xhtml+xml", ""}:
                return self._result("unsupported", final_url, reason="page is not HTML")
            signals = self.driver.execute_script(
                """
                return {
                    password: Boolean(document.querySelector(
                        'input[type="password"], input[name*="password" i]'
                    )),
                    challenge: Boolean(document.querySelector(
                        '.g-recaptcha, #captcha, [data-sitekey], '
                        + 'iframe[src*="captcha" i], iframe[src*="challenge" i]'
                    )),
                    body: document.body ? document.body.innerText : ''
                };
                """
            )
            if isinstance(signals, dict):
                if bool(signals.get("password")):
                    return self._result(
                        "unsupported",
                        final_url,
                        reason="authenticated pages are unsupported",
                    )
                if bool(signals.get("challenge")):
                    return self._result(
                        "blocked", final_url, reason="page contains an access challenge"
                    )
                body_text = signals.get("body", "")
            else:
                body_text = ""
            title = str(getattr(self.driver, "title", "") or "")[:512]
            text = body_text or self.driver.execute_script(
                "return document.body ? document.body.innerText : '';"
            )
            if not isinstance(text, str):
                return self._result(
                    "extraction_failed",
                    final_url,
                    reason="visible text was unavailable",
                )
            lowered = text.casefold()
            challenge_markers = (
                "verify you are human",
                "checking your browser",
                "challenge required",
                "access denied",
                "cf-chl-",
            )
            if search:
                # Search snippets can quote challenge messages. The structured
                # extractor distinguishes sources from Google's interstitials.
                challenge_markers = ()
            lowered_title = title.casefold()
            if any(
                marker in lowered or marker in lowered_title
                for marker in challenge_markers
            ):
                return self._result(
                    "blocked", final_url, reason="page contains an access challenge"
                )
            if not text.strip():
                if self.proxy.blocked:
                    return self._blocked(requested)
                return self._result(
                    "extraction_failed", final_url, reason="page has no visible text"
                )
            return {
                "outcome": "ok",
                "title": title,
                "url": final_url[:2048],
                "text": text[: self.max_chars],
            }
        except PublicUrlError:
            raise
        except TimeoutError:
            return self._result(
                "timeout", requested, reason="browser operation timed out"
            )
        except RuntimeError as exc:
            self.unusable = True
            message = str(exc)
            if "executable is unavailable" in message:
                reason = message
            elif "Selenium is not installed" in message:
                reason = "Selenium is unavailable"
            else:
                reason = "Chromium/ChromeDriver startup failed"
            return self._result("unavailable", requested, reason=reason)
        except ImportError:
            self.unusable = True
            return self._result(
                "unavailable", requested, reason="Selenium is unavailable"
            )
        except Exception:  # noqa: BLE001 - Selenium driver failures are unavailable outcomes.
            self.unusable = True
            return self._result(
                "unavailable", requested, reason="browser worker failed"
            )

    @staticmethod
    def _is_timeout(error: BaseException) -> bool:
        return type(error).__name__ in {"TimeoutException", "ScriptTimeoutException"}

    def _blocked(self, requested: str) -> dict[str, object]:
        return self._result(
            "blocked", requested, reason="navigation left public HTTP(S) scope"
        )

    @staticmethod
    def _result(outcome: str, url: str, *, reason: str = "") -> dict[str, object]:
        result: dict[str, object] = {
            "outcome": outcome,
            "title": "",
            "url": url,
            "text": "",
        }
        if reason:
            result["reason"] = reason
        return result

    def close(self) -> None:
        driver, self.driver = self.driver, None
        if driver is not None:
            with contextlib.suppress(Exception):
                driver.quit()
        self.proxy.close()


def _send(payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis request-owned Selenium worker")
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--timeout", required=True, type=float)
    parser.add_argument("--max-chars", required=True, type=int)
    args = parser.parse_args(argv)
    service: SeleniumPageReader | None = None
    try:
        service = SeleniumPageReader(
            Path(args.profile_dir), args.timeout, args.max_chars
        )
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or request.get("op") not in {
                    "read",
                    "search",
                }:
                    _send(
                        {
                            "outcome": "extraction_failed",
                            "reason": "invalid worker request",
                        }
                    )
                    continue
                if request["op"] == "search":
                    response = service.search(
                        request.get("query"), request.get("limit")
                    )
                else:
                    url = request.get("url")
                    if not isinstance(url, str):
                        _send({"outcome": "blocked", "reason": "invalid URL"})
                        continue
                    response = service.read(url)
                _send(response)
                if service.unusable:
                    return 2
            except PublicUrlError as exc:
                _send({"outcome": exc.outcome, "reason": str(exc)})
            except Exception:  # noqa: BLE001 - malformed child requests must not escape the worker.
                _send({"outcome": "unavailable", "reason": "browser worker failed"})
                return 2
    finally:
        if service is not None:
            service.close()
        with contextlib.suppress(OSError):
            shutil.rmtree(args.profile_dir)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    raise SystemExit(main())


__all__ = [
    "PublicNetworkError",
    "PublicNetworkProxy",
    "SeleniumPageReader",
    "connect_public",
    "find_chromedriver",
    "find_chromium",
    "main",
    "resolve_public_addresses",
]
