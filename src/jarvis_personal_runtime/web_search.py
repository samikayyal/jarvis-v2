"""Conservative extraction of known Google organic-result HTML layouts."""

from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .web_browser import PublicUrlError, validate_public_url

MAX_QUERY_LENGTH = 2048
MAX_SNIPPET_LENGTH = 2000


def destination_url(value: str) -> str:
    """Unwrap Google's ordinary result redirect, then apply public URL policy."""
    url = urljoin("https://www.google.com", value)
    parts = urlsplit(url)
    if parts.hostname in {"google.com", "www.google.com"}:
        # Current organic cards can carry opaque redirect tokens. Preserve the
        # actual returned link rather than guessing a URL from display text.
        # Reading it consumes the normal page allowance and validates redirects
        # through the same public-network proxy as any other page navigation.
        if parts.path == "/goto" and parse_qs(parts.query).get("url"):
            return validate_public_url(url)
        if parts.path != "/url":
            raise PublicUrlError("not a source destination")
        query = parse_qs(parts.query)
        url = (query.get("q") or query.get("url") or [""])[0]
    return validate_public_url(url)


def extract_google_results(html: str, limit: int, max_chars: int) -> dict[str, object]:
    """Unknown layouts fail closed; only explicit empty-result markers are empty."""
    if len(html) > 2_000_000:
        return {"outcome": "extraction_failed", "results": []}
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select('[hidden], [aria-hidden="true"]'):
        node.decompose()
    if soup.select_one(
        '#captcha, .g-recaptcha, #challenge-form, [role="dialog"][aria-modal="true"], form[action*="consent.google.com"]'
    ):
        return {"outcome": "blocked", "results": []}
    # Exclude whole containers before looking for headings, including citations
    # inside synthesized panels. Never treat an arbitrary heading/link as organic.
    for node in soup.select(
        "script, style, noscript, "
        "#tads, #tadsb, #bottomads, [data-text-ad], [data-ad-client], "
        '[data-attrid], [data-async-type="folsrch"], .kp-wholepage'
    ):
        node.decompose()
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    remaining = max_chars
    for card in soup.select("#search .g, #search .tF2Cxc, #main .Gx5Zad"):
        heading = card.select_one("h3")
        if heading is None:
            continue
        link = heading.find_parent("a") or heading.select_one("a")
        if link is None:
            continue
        try:
            url = destination_url(str(link.get("href", "")))
        except (PublicUrlError, ValueError):
            continue
        title = heading.get_text(" ", strip=True)[:512]
        if not title or url in seen:
            continue
        snippet_node = card.select_one(".VwiC3b, .IsZvec, .aCOpRe, .s3v9rd")
        snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        snippet = snippet[: min(MAX_SNIPPET_LENGTH, remaining)]
        remaining -= len(snippet)
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "evidence_type": "search_result",
            }
        )
        seen.add(url)
        if len(results) >= limit:
            break
    if results:
        return {"outcome": "ok", "results": results}
    # Non-modal date/filter/feedback dialogs also exist on ordinary result
    # pages. Their presence is not evidence of an access challenge.
    # Google's dedicated empty-results message, not arbitrary source prose.
    empty = soup.select_one("#topstuff")
    text = empty.get_text(" ", strip=True).casefold() if empty else ""
    if (
        empty
        and not empty.select_one("h3")
        and (
            (text.startswith("your search") and "did not match any documents" in text)
            or text.startswith("no results found for")
        )
    ):
        return {"outcome": "no_results", "results": []}
    # A result snippet can discuss blocking. Only classify a known interstitial
    # message once organic extraction has found no sources.
    body = soup.get_text(" ", strip=True).casefold()
    if not soup.select_one("h3") and (
        "our systems have detected unusual traffic" in body
        or "before you continue to google" in body
        or "your computer or network may be sending automated queries" in body
    ):
        return {"outcome": "blocked", "results": []}
    return {"outcome": "extraction_failed", "results": []}
