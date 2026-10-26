"""Opt-in real Chromium checks, also run against a non-editable wheel install."""

from __future__ import annotations

import asyncio
import http.server
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from jarvis_personal_runtime.web_browser import BrowserWorker

pytestmark = pytest.mark.skipif(
    os.name != "posix"
    or not os.environ.get("JARVIS_TEST_CHROMIUM_BINARY")
    or not os.environ.get("JARVIS_TEST_CHROMEDRIVER_BINARY"),
    reason="requires POSIX and explicitly provisioned Chromium/ChromeDriver",
)


def active_group_members(group: int) -> list[int]:
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == group and fields[0] != "Z":
                members.append(int(entry.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return members


def browser_processes() -> set[int]:
    """Include detached crash handlers without a profile in their command line."""
    processes = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            executable = (entry / "cmdline").read_bytes().split(b"\0", 1)[0]
            if Path(os.fsdecode(executable)).name in {
                "chrome",
                "chromium",
                "google-chrome",
                "chromedriver",
                "chrome_crashpad_handler",
                "Xvfb",
            }:
                processes.add(int(entry.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return processes


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("headed", [False, True])
def test_real_worker_profiles_and_process_tree_cleanup(tmp_path, cancel, headed):
    if headed and any(shutil.which(tool) is None for tool in ("xvfb-run", "xauth")):
        pytest.skip("headed fixture requires xvfb-run and xauth")
    slow_started = threading.Event()
    release = threading.Event()
    cookies = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                cookies.append(self.headers.get("Cookie"))
            if self.path == "/slow":
                slow_started.set()
                release.wait(30)
            body = (
                b"<html><title>Owned fixture</title><body>Owned public evidence"
                b"<script>document.body.textContent='Page script executed';</script>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "fixture=visited; Path=/")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # This temporary child entrypoint exists only in the test. The production
    # worker has no flag, environment setting, or URL exception for fixtures.
    entrypoint = tmp_path / "fixture_worker.py"
    entrypoint.write_text(
        "from jarvis_personal_runtime import web_worker as w\n"
        "original = w.resolve_public_addresses\n"
        "w.resolve_public_addresses = lambda host, port: "
        "('127.0.0.1',) if host == 'fixture.example' else original(host, port)\n"
        f"w.find_chromium = lambda: {os.environ['JARVIS_TEST_CHROMIUM_BINARY']!r}\n"
        f"w.find_chromedriver = lambda: {os.environ['JARVIS_TEST_CHROMEDRIVER_BINARY']!r}\n"
        "raise SystemExit(w.main())\n",
        encoding="utf-8",
    )

    async def scenario():
        profiles = []
        groups = []

        async def factory(*argv, **kwargs):
            profiles.append(Path(argv[argv.index("--profile-dir") + 1]))
            # Preserve the production new-session and minimal-environment setup.
            process = await asyncio.create_subprocess_exec(
                *argv[: argv.index(sys.executable)],
                sys.executable,
                str(entrypoint),
                *argv[argv.index("--profile-dir") :],
                **kwargs,
            )
            groups.append(process.pid)
            return process

        runtime = tmp_path / "runtime"
        for index in range(2):
            existing_browsers = browser_processes()
            display_auth_dirs = set(Path(tempfile.gettempdir()).glob("xvfb-run.*"))
            worker = BrowserWorker(
                runtime, 20, 1000, process_factory=factory, headed=headed
            )
            try:
                result = await worker.read(
                    f"http://fixture.example:{server.server_port}/"
                )
                assert result["outcome"] == "ok", result
                assert result["text"] == "Owned public evidence"
                assert profiles[-1].stat().st_mode & 0o077 == 0
                assert len(active_group_members(groups[-1])) > 2
                if cancel and index == 1:
                    task = asyncio.create_task(
                        worker.read(f"http://fixture.example:{server.server_port}/slow")
                    )
                    assert await asyncio.to_thread(slow_started.wait, 10)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            finally:
                await worker.close()
            assert not active_group_members(groups[-1])
            for _ in range(20):
                if not (browser_processes() - existing_browsers):
                    break
                await asyncio.sleep(0.1)
            assert not (browser_processes() - existing_browsers)
            assert not (
                set(Path(tempfile.gettempdir()).glob("xvfb-run.*")) - display_auth_dirs
            )
            assert not list(runtime.iterdir())
        assert profiles[0] != profiles[1]
        assert cookies == [None, None]

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("headed", [False, True])
def test_real_rendered_content_is_bounded_isolated_and_public_only(tmp_path, headed):
    """Exercise the real protocol/Chrome/proxy, without relying on a live website."""
    private_requests = []
    cookies = []
    rendering_started = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/private":
                private_requests.append(self.path)
            if self.path == "/started":
                rendering_started.set()
            if self.path == "/dynamic":
                cookies.append(self.headers.get("Cookie"))
            script = {
                "/dynamic": "setTimeout(() => {document.getElementById('prices').innerText = 'E2 hourly USD 0.02';}, 350);",
                "/private-fetch": f"fetch('http://127.0.0.1:{self.server.server_port}/private').catch(() => {{document.body.innerText = 'Private request denied';}});",
                "/private-redirect": f"location.href = 'http://127.0.0.1:{self.server.server_port}/private';",
                "/cancel": "fetch('/started');",
            }.get(self.path, "")
            content = (
                "Verify you are human"
                if self.path == "/challenge"
                else "Loading pricing"
            )
            body = (
                f"<html><title>Pricing fixture</title><body><main><h1>VM prices</h1>"
                f"<div id='prices'>{content}</div></main><script>{script}</script></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "fixture=visited; Path=/")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    entrypoint = tmp_path / "render_fixture_worker.py"
    entrypoint.write_text(
        "from jarvis_personal_runtime import web_worker as w\n"
        "original = w.resolve_public_addresses\n"
        "w.resolve_public_addresses = lambda host, port: "
        "('127.0.0.1',) if host == 'fixture.example' else original(host, port)\n"
        f"w.find_chromium = lambda: {os.environ['JARVIS_TEST_CHROMIUM_BINARY']!r}\n"
        f"w.find_chromedriver = lambda: {os.environ['JARVIS_TEST_CHROMEDRIVER_BINARY']!r}\n"
        "raise SystemExit(w.main())\n",
        encoding="utf-8",
    )

    async def scenario():
        groups = []
        profiles = []

        async def factory(*argv, **kwargs):
            profiles.append(Path(argv[argv.index("--profile-dir") + 1]))
            process = await asyncio.create_subprocess_exec(
                *argv[: argv.index(sys.executable)],
                sys.executable,
                str(entrypoint),
                *argv[argv.index("--profile-dir") :],
                **kwargs,
            )
            groups.append(process.pid)
            return process

        before = browser_processes()
        auth_before = set(Path(tempfile.gettempdir()).glob("xvfb-run.*"))
        runtime = tmp_path / "runtime"
        static = BrowserWorker(
            runtime, 15, 8000, process_factory=factory, headed=headed
        )
        rendered = BrowserWorker(
            runtime, 20, 8000, process_factory=factory, headed=headed, rendered=True
        )
        base = f"http://fixture.example:{server.server_port}"
        try:
            first = await static.read(base + "/dynamic#prices")
            assert first["outcome"] == "ok", first
            assert "E2 hourly" not in first["text"]
            assert first["section_found"] is True
            result = await rendered.read(
                base + "/dynamic#prices", wait_for_text="E2 hourly"
            )
            assert result["outcome"] == "ok", result
            assert "E2 hourly USD 0.02" in result["text"]
            assert result["render_wait"] == "matched"
            assert result["render_mode"] == "rendered"
            assert result["source_chars"] == len(result["text"])
            assert cookies == [None, None]  # Rendering never inherits static cookies.
            missing = await static.read(base + "/dynamic#missing")
            assert missing["section_found"] is False
            assert (
                "E2 hourly" not in missing["text"]
            )  # Rendering did not enable static scripts.
            denied = await rendered.read(
                base + "/private-fetch", wait_for_text="Private request denied"
            )
            assert denied["outcome"] == "ok", denied
            assert not private_requests
            blocked = await rendered.read(
                base + "/private-redirect", wait_for_text="private"
            )
            assert blocked["outcome"] == "blocked", blocked
            assert not private_requests
            challenge = await rendered.read(base + "/challenge", wait_for_text="hourly")
            assert challenge["outcome"] == "blocked", challenge
            start = time.monotonic()
            pending = await rendered.read(base + "/never", wait_for_text="E2 hourly")
            assert pending["outcome"] == "ok", pending
            assert pending["render_wait"] == "timeout"
            assert pending["stop_reason"] == "render_wait_timeout"
            assert time.monotonic() - start < 21
            task = asyncio.create_task(
                rendered.read(base + "/cancel", wait_for_text="absent price")
            )
            assert await asyncio.to_thread(rendering_started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await static.close()
            await rendered.close()
        assert all(not p.exists() for p in profiles)
        assert all(not active_group_members(g) for g in groups)
        for _ in range(30):
            if not browser_processes() - before:
                break
            await asyncio.sleep(0.1)
        assert not browser_processes() - before
        assert not set(Path(tempfile.gettempdir()).glob("xvfb-run.*")) - auth_before

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
