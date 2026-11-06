"""Anonymous persistent public-research browser. No model-supplied JavaScript."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
from urllib.parse import urldefrag
from uuid import uuid4

from playwright.async_api import Error, async_playwright
from playwright.async_api import TimeoutError as BrowserTimeout

from .page_document import convert_html
from .web_browser import PublicUrlError, _minimal_environment, validate_public_url
from .web_worker import PublicNetworkProxy

# Kept entirely runtime-owned. Element handles refer to the observed nodes, not
# selectors invented by a model or positional indices that can silently drift.
CAPTURE = r"""() => {
  const visible = e => !!e.getClientRects().length &&
    getComputedStyle(e).visibility !== 'hidden';
  const root = document.body || document.documentElement;
  const clone = root.cloneNode(true);
  const originals = [root, ...root.querySelectorAll('*')];
  const copies = [clone, ...clone.querySelectorAll('*')];
  originals.forEach((e, i) => { if (!visible(e)) copies[i].remove(); });
  return clone.outerHTML;
}"""
DESCRIBE = r"""e => {
  const labelText = l => {
    const clone = l.cloneNode(true);
    clone.querySelectorAll('input,select,textarea,button').forEach(n => n.remove());
    return clone.textContent;
  };
  const label = (e.getAttribute('aria-label') ||
    (e.labels && [...e.labels].map(labelText).join(' ')) ||
    e.innerText || e.getAttribute('placeholder') || e.name || e.id || '').trim();
  const type = (e.getAttribute('type') || '').toLowerCase();
  const form = e.closest('form');
  const scope = [label, e.name, e.id, e.getAttribute('href'),
    form && form.getAttribute('action')].filter(Boolean).join(' ');
  const forbidden = /\b(log[ -]?in|sign[ -]?in|sign[ -]?up|register|password|checkout|purchase|buy|add to cart|delete|upload|subscribe|create|save|send|invite|book|reserve|place order|payment)\b/i;
  const sensitiveForm = form && form.querySelector(
    'input[type=password],input[type=email],input[type=file],input[autocomplete=cc-number]');
  const allowed = !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' &&
    !e.disabled && !sensitiveForm && !forbidden.test(scope) &&
    !['password','email','file','hidden'].includes(type) && !e.isContentEditable;
  const research = /search|filter|calculat/i.test(scope) || type === 'search' ||
    !!(form && (form.getAttribute('role') === 'search' || form.querySelector('input[type=search]')));
  const submit = (e.tagName === 'BUTTON' || e.tagName === 'INPUT') && e.type === 'submit';
  const method = form && (e.getAttribute('formmethod') || form.method).toLowerCase();
  const enctype = form && (e.getAttribute('formenctype') || form.enctype).toLowerCase();
  const post = allowed && research && submit && method === 'post' &&
    enctype === 'application/x-www-form-urlencoded' ? {
      url:e.hasAttribute('formaction') ? e.formAction : form.action,
      body:new URLSearchParams([...new FormData(form, e)].map(([k,v]) =>
        [k, String(v).replace(/\r?\n/g,'\r\n')])).toString()
    } : null;
  return {label, kind:e.tagName.toLowerCase(), type, allowed,
    post,
    href:e.href || '', target:e.target || '', options:e.tagName === 'SELECT' ?
      [...e.options].map(o => ({label:o.text, value:o.value, disabled:o.disabled})) : []};
}"""


class BrowserStateLost(RuntimeError):
    """The interactive state is gone; it must not be silently reconstructed."""


class PlaywrightBrowser:
    def __init__(self, *, timeout_seconds: float = 15):
        self.timeout_seconds = timeout_seconds
        self.closed = False
        self._lost = False
        self._playwright = None
        self._startup = None
        self._browser = None
        self._context = None
        self._page = None
        self._proxy = PublicNetworkProxy()
        self._elements = {}
        self._tabs = {}
        self._post_permit = None
        self._blocked = []

    async def open(self, url: str) -> dict:
        url = validate_public_url(url)
        try:
            self._proxy.start()
            self._startup = asyncio.create_task(async_playwright().start())
            self._playwright = await asyncio.shield(self._startup)
            environment: dict[str, str | float] = {**_minimal_environment()}
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                chromium_sandbox=os.name != "nt",
                env=environment,
                proxy={
                    "server": f"http://127.0.0.1:{self._proxy.port}",
                    "bypass": "<-loopback>",
                },
                args=[
                    "--disable-quic",
                    "--disable-background-networking",
                    "--disable-extensions",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            self._browser.on("disconnected", self._state_lost)
            self._context = await self._browser.new_context(
                accept_downloads=False,
                service_workers="block",
            )
            self._context.set_default_timeout(self.timeout_seconds * 1000)
            await self._context.route("**/*", self._route)
            await self._context.route_web_socket("**/*", lambda ws: ws.close())
            self._context.on("page", self._new_page)
            self._page = await self._context.new_page()
            cdp = await self._context.new_cdp_session(self._page)
            await cdp.send("Browser.setDownloadBehavior", {"behavior": "deny"})
            await self._page.goto(url, wait_until="domcontentloaded")
            await self._settle()
            return await self.observe()
        except BaseException:
            await self.close()
            raise

    def _state_lost(self, *_):
        if not self.closed:
            self._lost = True

    def _new_page(self, page):
        self._tabs[uuid4().hex[:10]] = page
        page.on("crash", self._state_lost)

    async def _route(self, route):
        request = route.request
        try:
            validate_public_url(request.url)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                permit = self._post_permit
                if not (
                    permit is not None
                    and request.method == "POST"
                    and request.is_navigation_request()
                    and request.frame == permit[2]
                    and request.url == permit[0]
                    and request.post_data == permit[1]
                    and request.headers.get("content-type", "").startswith(
                        "application/x-www-form-urlencoded"
                    )
                ):
                    raise PublicUrlError(
                        "Write blocked: only the exact observed public research form submission is permitted"
                    )
                self._post_permit = None
            await route.continue_()
        except PublicUrlError as exc:
            self._blocked.append(str(exc))
            await route.abort("blockedbyclient")

    def _check(self):
        if self._lost or (self._browser and not self._browser.is_connected()):
            raise BrowserStateLost(
                "Browser crashed or disconnected; interactive state was lost."
            )
        if self.closed or self._context is None or self._page is None:
            raise BrowserStateLost(
                "Browser is closed; interactive state is unavailable."
            )

    async def _settle(self):
        self._check()
        assert self._page is not None
        await self._page.wait_for_load_state("domcontentloaded")
        # Bound background requests on sites that never become network-idle.
        with contextlib.suppress(BrowserTimeout):
            await self._page.wait_for_load_state(
                "networkidle", timeout=min(1000, self.timeout_seconds * 1000)
            )

    async def observe(self) -> dict:
        self._check()
        assert self._page is not None
        validate_public_url(self._page.url)
        if self._proxy.blocked_url(self._page.url):
            raise PublicUrlError("Navigation was blocked by the public-network proxy")
        for handle in self._elements.values():
            await handle.dispose()
        self._elements.clear()
        elements = []
        handles = await self._page.query_selector_all(
            "a[href], button, input, select, textarea, summary, [role=button], [role=tab]"
        )
        for handle in handles:
            description = await handle.evaluate(DESCRIBE)
            if not description.pop("allowed"):
                await handle.dispose()
                continue
            description.pop("post")
            ref = uuid4().hex[:12]
            self._elements[ref] = handle
            elements.append({"ref": ref, **description})
        result = {
            "url": self._page.url,
            "title": await self._page.title(),
            "text": convert_html(await self._page.evaluate(CAPTURE), self._page.url),
            "elements": elements,
        }
        if self._blocked or self._proxy.blocked:
            result["blocked_requests"] = [*self._blocked, self._proxy.blocked_reason]
            self._blocked.clear()
            self._proxy.clear_block()
        return result

    async def act(self, action: str, arguments: dict) -> dict:
        self._check()
        assert self._page is not None and self._context is not None
        attempted = False
        before = set(self._context.pages)
        try:
            if action == "inspect":
                return await self.observe()
            if action == "screenshot":
                return {
                    "url": self._page.url,
                    "image": "data:image/png;base64,"
                    + base64.b64encode(
                        await self._page.screenshot(type="png")
                    ).decode(),
                }
            if action == "tabs":
                return {
                    "tabs": [
                        {"id": key, "url": page.url, "active": page == self._page}
                        for key, page in self._tabs.items()
                        if not page.is_closed()
                    ]
                }
            if action in {"switch_tab", "close_tab"}:
                page = self._tabs.get(arguments["tab_id"])
                if page is None or page.is_closed():
                    raise ValueError("Unknown or closed tab; list tabs again.")
                if action == "close_tab":
                    if len(self._context.pages) == 1:
                        raise ValueError(
                            "Cannot close the last tab; cancel the reader instead."
                        )
                    await page.close()
                    if self._page == page:
                        self._page = self._context.pages[-1]
                else:
                    self._page = page
            elif action in {"click", "type", "select"}:
                handle = self._elements.get(arguments["ref"])
                if handle is None or not await handle.evaluate("e => e.isConnected"):
                    raise ValueError(
                        "Stale element reference; inspect and choose a fresh reference."
                    )
                description = await handle.evaluate(DESCRIBE)
                if not description["allowed"]:
                    raise ValueError("Control is outside public research authority.")
                if action == "click":
                    if description["href"]:
                        validate_public_url(description["href"])
                    if description["post"]:
                        post = description["post"]
                        self._post_permit = (
                            urldefrag(validate_public_url(post["url"]))[0],
                            post["body"],
                            await handle.owner_frame(),
                        )
                    attempted = True
                    if description["target"] == "_blank":
                        async with self._context.expect_page() as popup:
                            await handle.click()
                        self._page = await popup.value
                    else:
                        await handle.click()
                elif action == "type":
                    if description["kind"] not in {"input", "textarea"}:
                        raise ValueError(
                            "Choose an editable public search/filter/calculator control."
                        )
                    attempted = True
                    await handle.fill(arguments["text"])
                else:
                    attempted = True
                    await handle.select_option(label=arguments["value"])
            elif action == "scroll":
                await self._page.mouse.wheel(0, arguments["pixels"])
                await self._settle()
                return {
                    "url": self._page.url,
                    "scroll_y": await self._page.evaluate("window.scrollY"),
                }
            elif action in {"back", "forward", "reload"}:
                operation = {
                    "back": self._page.go_back,
                    "forward": self._page.go_forward,
                    "reload": self._page.reload,
                }[action]
                await operation(wait_until="domcontentloaded")
            else:
                raise ValueError("Unknown reader browser action.")
            await self._settle()
            new_pages = [p for p in self._context.pages if p not in before]
            if new_pages:
                self._page = new_pages[-1]
                await self._settle()
            return await self.observe()
        except (Error, PublicUrlError, ValueError) as exc:
            if self._lost or self._browser is None or not self._browser.is_connected():
                raise BrowserStateLost(
                    "Browser crashed; interactive state was lost."
                ) from exc
            return {
                "error": str(exc),
                "action": action,
                "uncertain": attempted or isinstance(exc, Error),
                "next_step": "Inspect current state before choosing another action; do not blindly replay.",
            }
        finally:
            self._post_permit = None

    async def close(self):
        self.closed = True
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        startup, self._startup = self._startup, None
        self._context = None
        self._page = None
        try:
            if browser:
                await browser.close()
        finally:
            try:
                if playwright is None and startup is not None:
                    # Driver startup is shielded so cancellation cannot orphan
                    # its subprocess before the public stop handle is returned.
                    with contextlib.suppress(Error):
                        playwright = await startup
                if playwright:
                    await playwright.stop()
            finally:
                await asyncio.to_thread(self._proxy.close)
