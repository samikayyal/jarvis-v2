"""Request-owned Selenium browser boundary.

The runtime talks to :mod:`jarvis_personal_runtime.web_worker` over a small
JSON-lines protocol.  Keeping Selenium in a child process is deliberate:
Selenium calls are synchronous and a wedged browser must never remain running
inside the event-loop process.  The child is started lazily, once per ordinary
request, and is never silently restarted after it dies.

This module also owns the cheap, lexical part of the public URL policy.  The
child repeats that check and applies the address policy at the network
boundary; a final ``current_url`` check alone would be insufficient for
redirects and subresources.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import math
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import Awaitable, Callable
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MAX_PUBLIC_URL_LENGTH = 2048
MAX_TITLE_LENGTH = 512
MAX_RETURNED_URL_LENGTH = 2048
_PROTOCOL_LIMIT = 1_048_576
_ALLOWED_OUTCOMES = frozenset(
    {"ok", "blocked", "unsupported", "extraction_failed", "unavailable", "timeout"}
)


class PublicUrlError(ValueError):
    """A URL is outside the public HTML boundary."""

    def __init__(self, message: str, *, outcome: str = "blocked") -> None:
        super().__init__(message)
        self.outcome = outcome


def _public_ip(value: str) -> IPv4Address | IPv6Address | None:
    """Parse an IP literal, including the common integer IPv4 spelling."""

    try:
        return ipaddress.ip_address(value)
    except ValueError:
        if value.isdigit():
            try:
                integer = int(value, 10)
                if 0 <= integer <= 0xFFFFFFFF:
                    return ipaddress.ip_address(integer)
            except ValueError:
                pass
    return None


def is_public_address(value: str | IPv4Address | IPv6Address) -> bool:
    """Return whether an address is globally routable for this boundary."""

    try:
        address = value if not isinstance(value, str) else ipaddress.ip_address(value)
    except ValueError:
        return False
    # ``is_global`` alone is not sufficient on all supported Python versions:
    # some multicast ranges report as global.  Spell out every special-use
    # category and also inspect IPv4-mapped IPv6 values.
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(
        address.is_global
        and not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
    )


def _hostname(host: str) -> str:
    if not host:
        raise PublicUrlError("URL host is empty")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in host):
        raise PublicUrlError("URL host contains control characters")
    if "\\" in host or "%" in host:
        raise PublicUrlError("URL host uses an unsupported spelling")

    literal = _public_ip(host)
    if literal is not None:
        if not is_public_address(literal):
            raise PublicUrlError("URL address is not public")
        return literal.compressed

    # Browsers and socket libraries accept abbreviated numeric IPv4 spellings
    # such as ``127.1`` as loopback addresses.  Treat an all-numeric dotted
    # host as an address candidate even when ``ipaddress`` does not recognize
    # its legacy form; allowing it through as DNS would make the lexical
    # boundary dependent on platform resolver behavior.
    if all(label.isdigit() for label in host.rstrip(".").split(".")):
        raise PublicUrlError("URL address is not a valid public address")

    try:
        encoded = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise PublicUrlError("URL host is not a valid DNS name") from exc
    if not encoded or "." not in encoded:
        # A single-label name can resolve to a local search-domain or host
        # alias.  Public page reads require an explicitly qualified DNS name.
        raise PublicUrlError("URL host must be a qualified public DNS name")
    if len(encoded) > 253 or any(
        not label
        or len(label) > 63
        or label[0] == "-"
        or label[-1] == "-"
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in label
        )
        for label in encoded.split(".")
    ):
        raise PublicUrlError("URL host is not a valid DNS name")
    lowered = encoded.casefold()
    if lowered == "localhost" or lowered.endswith(
        (".localhost", ".local", ".internal", ".intranet", ".home.arpa")
    ):
        raise PublicUrlError("URL host is reserved for local or internal use")
    return encoded


def validate_public_url(url: str) -> str:
    """Validate and canonicalize one public HTTP(S) URL.

    This function is intentionally lexical and never performs DNS or network
    I/O.  The worker repeats the hostname check after DNS resolution for every
    connection so a DNS answer cannot turn a public name into an internal
    destination between validation and navigation.
    """

    if not isinstance(url, str) or not url.strip():
        raise PublicUrlError("URL must be a non-empty string")
    if len(url) > MAX_PUBLIC_URL_LENGTH:
        raise PublicUrlError("URL exceeds the public-page limit")
    if url != url.strip() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in url
    ):
        raise PublicUrlError("URL contains whitespace or control characters")
    if "\\" in url:
        raise PublicUrlError("URL contains a backslash")
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.casefold()
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise PublicUrlError("URL is malformed") from exc
    if scheme not in {"http", "https"}:
        raise PublicUrlError("only HTTP(S) pages are supported", outcome="unsupported")
    if parts.username is not None or parts.password is not None:
        raise PublicUrlError("URLs with credentials are not supported")
    if host is None:
        raise PublicUrlError("URL host is empty")
    if port is not None and not 1 <= port <= 65535:
        raise PublicUrlError("URL port is invalid")
    canonical_host = _hostname(host)
    authority = f"[{canonical_host}]" if ":" in canonical_host else canonical_host
    if port is not None:
        authority = f"{authority}:{port}"
    # Keep the source identity stable while normalizing scheme and host.  The
    # fragment is harmless to the network boundary and is retained for source
    # citations; browsers never send it to the server.
    return urlunsplit(
        (scheme, authority, parts.path or "/", parts.query, parts.fragment)
    )


def _minimal_environment() -> dict[str, str]:
    """Environment for the worker, excluding inherited credentials/state."""

    # Do not copy os.environ.  In particular this intentionally omits HOME,
    # cloud credentials, OpenAI/OpenWA variables, proxy credentials, and any
    # browser profile variables.  Chrome receives an explicit fresh profile.
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if os.name == "nt":
        # The Windows asyncio subprocess transport needs the system provider
        # variables even though the production service runs on Ubuntu.  Keep
        # this small platform set free of user profile and credential state.
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment.update(
            {
                "SystemRoot": system_root,
                "WINDIR": system_root,
                "TEMP": tempfile.gettempdir(),
                "TMP": tempfile.gettempdir(),
                "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                "PATH": os.pathsep.join(
                    (
                        str(Path(system_root) / "System32"),
                        system_root,
                        str(Path(system_root) / "System32" / "Wbem"),
                    )
                ),
            }
        )
    return environment


class BrowserWorker:
    """One request-owned browser worker.

    ``read`` is safe to call sequentially.  A timeout, malformed worker
    response, child crash, or cancellation makes this instance unusable.  A
    caller must construct a new worker for a later request; this prevents a
    half-dead Selenium session from being hidden behind an automatic retry.
    """

    def __init__(
        self,
        runtime_dir: Path,
        timeout: float,
        max_chars: int,
        *,
        process_factory: Callable[..., Awaitable[Any]] | None = None,
        python_executable: str | None = None,
    ) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("browser timeout must be a finite positive number")
        if not math.isfinite(float(timeout)) or timeout <= 0:
            raise ValueError("browser timeout must be a finite positive number")
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or max_chars < 1
        ):
            raise TypeError("browser character limit must be a positive integer")
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.runtime_dir.chmod(0o700)
        self.timeout = float(timeout)
        self.max_chars = max_chars
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._python_executable = python_executable or sys.executable
        self._process: Any | None = None
        self._profile_dir: Path | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._io_lock = asyncio.Lock()
        self._dead = False
        self._closed = False

    @property
    def usable(self) -> bool:
        return not self._dead and not self._closed

    async def read(self, url: str) -> dict[str, object]:
        """Read one public HTML page and return bounded structured evidence."""

        try:
            canonical = validate_public_url(url)
        except PublicUrlError as exc:
            return self._result(exc.outcome, url, reason=str(exc))
        return await self._operate({"op": "read", "url": canonical}, canonical)

    async def search(self, query: str, limit: int) -> dict[str, object]:
        from .web_search import MAX_QUERY_LENGTH

        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > MAX_QUERY_LENGTH
        ):
            raise ValueError("invalid search query")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("invalid result limit")
        return await self._operate(
            {"op": "search", "query": query, "limit": limit},
            "https://www.google.com/search",
        )

    async def _operate(
        self, payload: dict[str, object], canonical: str
    ) -> dict[str, object]:
        if self._closed:
            return self._result("unavailable", canonical, reason="worker is closed")
        if self._dead:
            return self._result(
                "unavailable", canonical, reason="worker is unavailable"
            )

        try:
            await self._ensure_started()
            async with asyncio.timeout(self.timeout):
                response = await self._request(payload)
        except asyncio.CancelledError:
            self._dead = True
            await self._terminate()
            raise
        except TimeoutError:
            self._dead = True
            await self._terminate()
            return self._result(
                "timeout", canonical, reason="browser operation timed out"
            )
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
            self._dead = True
            await self._terminate()
            return self._result(
                "unavailable", canonical, reason="browser worker unavailable"
            )

        result = (
            response
            if payload["op"] == "search"
            else self._normalize(response, canonical)
        )
        if "outcome" not in result:
            result = {"outcome": "extraction_failed", "results": []}
        # Startup failures and child-reported browser crashes must not turn into
        # a hidden second browser on the next call.
        if result["outcome"] == "unavailable":
            self._dead = True
            await self._terminate()
            result["usable"] = False
        elif result["outcome"] == "timeout" and not self.usable:
            result["usable"] = False
        return result

    async def close(self) -> None:
        """Terminate the worker process group and remove its private profile."""

        if self._closed and self._process is None and self._profile_dir is None:
            return
        self._closed = True
        self._dead = True
        await self._terminate()

    async def _ensure_started(self) -> None:
        if self._process is not None:
            return
        if self._dead or self._closed:
            raise RuntimeError("browser worker is unavailable")
        self._profile_dir = Path(
            tempfile.mkdtemp(prefix=".jarvis-web-", dir=str(self.runtime_dir))
        )
        with contextlib.suppress(OSError):
            self._profile_dir.chmod(0o700)
        try:
            spawn_kwargs: dict[str, Any] = {
                "stdin": asyncio.subprocess.PIPE,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "env": _minimal_environment(),
                "limit": max(65_536, min(_PROTOCOL_LIMIT, self.max_chars * 8 + 8192)),
            }
            if os.name == "posix":
                spawn_kwargs["start_new_session"] = True
            else:
                # CREATE_NEW_PROCESS_GROUP is the Windows analogue used by
                # the fallback cleanup path.  The production runtime is
                # Ubuntu; retaining it here keeps injected test processes
                # deterministic on the development host.
                spawn_kwargs["creationflags"] = 0x00000200
            async with asyncio.timeout(self.timeout):
                self._process = await self._process_factory(
                    self._python_executable,
                    "-m",
                    "jarvis_personal_runtime.web_worker",
                    "--profile-dir",
                    str(self._profile_dir),
                    "--timeout",
                    str(self.timeout),
                    "--max-chars",
                    str(self.max_chars),
                    **spawn_kwargs,
                )
            stderr = getattr(self._process, "stderr", None)
            if stderr is not None:
                self._stderr_task = asyncio.create_task(self._drain_stderr(stderr))
        except BaseException:
            await self._remove_profile()
            self._process = None
            raise

    async def _request(self, payload: dict[str, object]) -> dict[str, object]:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("browser worker pipes are unavailable")
        async with self._io_lock:
            # A startup failure sends its final structured response and then
            # exits immediately.  Do not reject an already-buffered response
            # merely because the child has reaped before this coroutine gets
            # to the pipe; ordinary reads still fail closed on EOF.
            if process.returncode is None:
                process.stdin.write(
                    (json.dumps(payload, separators=(",", ":")) + "\n").encode()
                )
                await process.stdin.drain()
            raw = await asyncio.wait_for(
                process.stdout.readline(), timeout=self.timeout
            )
        if not raw:
            raise RuntimeError("browser worker closed its protocol")
        if len(raw) > _PROTOCOL_LIMIT:
            raise ValueError("browser worker response is too large")
        response = json.loads(raw.decode("utf-8"))
        if not isinstance(response, dict):
            raise TypeError("browser worker response is not an object")
        return response

    def _normalize(
        self, response: dict[str, object], requested: str
    ) -> dict[str, object]:
        raw_outcome = response.get("outcome")
        outcome = raw_outcome if isinstance(raw_outcome, str) else "extraction_failed"
        if outcome not in _ALLOWED_OUTCOMES:
            outcome = "extraction_failed"
        if outcome != "ok":
            # Never echo a private redirect or worker diagnostic as source
            # identity when navigation was blocked or the browser died.
            returned_url = requested
        else:
            candidate = response.get("url")
            try:
                returned_url = (
                    validate_public_url(candidate)
                    if isinstance(candidate, str)
                    else requested
                )
            except PublicUrlError:
                return self._result(
                    "blocked", requested, reason="final URL is outside public scope"
                )
        title = str(response.get("title", ""))[:MAX_TITLE_LENGTH]
        text = (
            str(response.get("text", ""))[: self.max_chars] if outcome == "ok" else ""
        )
        result: dict[str, object] = {
            "outcome": outcome,
            "title": title,
            "url": returned_url[:MAX_RETURNED_URL_LENGTH],
            "text": text,
        }
        reason = response.get("reason")
        if isinstance(reason, str) and reason:
            result["reason"] = reason[:512]
        return result

    def _result(self, outcome: str, url: str, *, reason: str = "") -> dict[str, object]:
        result: dict[str, object] = {
            "outcome": outcome if outcome in _ALLOWED_OUTCOMES else "extraction_failed",
            "title": "",
            "url": str(url)[:MAX_RETURNED_URL_LENGTH],
            "text": "",
        }
        if reason:
            result["reason"] = reason[:512]
        if outcome in {"timeout", "unavailable"}:
            result["usable"] = self.usable
        return result

    async def _drain_stderr(self, stream: Any) -> None:
        with contextlib.suppress(Exception):
            while await stream.read(4096):
                pass

    async def _terminate(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._terminate_owned())
        cancelled = False
        while not self._cleanup_task.done():
            try:
                await asyncio.shield(self._cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        self._cleanup_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _terminate_owned(self) -> None:
        process = self._process
        stderr_task = self._stderr_task
        profile = self._profile_dir
        try:
            await self._finish_termination(process, stderr_task, profile)
        finally:
            if self._process is process:
                self._process = None
            if self._stderr_task is stderr_task:
                self._stderr_task = None
            if self._profile_dir is profile:
                self._profile_dir = None

    async def _finish_termination(
        self,
        process: Any | None,
        stderr_task: asyncio.Task[None] | None,
        profile: Path | None,
    ) -> None:
        failure: BaseException | None = None
        if process is not None:
            try:
                await self._stop_process(process)
            except BaseException as exc:  # noqa: BLE001 - finish remaining cleanup.
                failure = exc
        if stderr_task is not None:
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:  # noqa: BLE001 - finish profile cleanup.
                if failure is None:
                    failure = exc
        if profile is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(profile)
        if failure is not None:
            raise failure

    async def _remove_profile(self) -> None:
        profile, self._profile_dir = self._profile_dir, None
        if profile is None:
            return
        with contextlib.suppress(OSError):
            shutil.rmtree(profile)

    @staticmethod
    async def _stop_process(process: Any) -> None:
        pid = getattr(process, "pid", None)
        if os.name == "posix" and isinstance(pid, int):
            # The leader may already have exited while ChromeDriver/Chromium
            # remains in the same process group.  Signal the group even then.
            group_signal_sent = False
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGTERM)
                group_signal_sent = True
            if not group_signal_sent:
                terminate = getattr(process, "terminate", None)
                if callable(terminate) and getattr(process, "returncode", None) is None:
                    with contextlib.suppress(ProcessLookupError):
                        terminate()
            try:
                async with asyncio.timeout(1):
                    await process.wait()
            except TimeoutError:
                pass
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await process.wait()
            return
        if getattr(process, "returncode", None) is not None:
            return
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            with contextlib.suppress(ProcessLookupError):
                terminate()
        try:
            async with asyncio.timeout(1):
                await process.wait()
                return
        except TimeoutError:
            pass
        kill = getattr(process, "kill", None)
        if callable(kill):
            with contextlib.suppress(ProcessLookupError):
                kill()
        with contextlib.suppress(Exception):
            await process.wait()


__all__ = [
    "MAX_PUBLIC_URL_LENGTH",
    "BrowserWorker",
    "PublicUrlError",
    "is_public_address",
    "validate_public_url",
]
