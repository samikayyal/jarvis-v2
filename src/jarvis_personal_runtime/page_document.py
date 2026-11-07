"""Content-preserving HTML conversion for public browser observations."""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString


def convert_html(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select(
        "script, style, noscript, template, [hidden], [aria-hidden=true]"
    ):
        node.decompose()

    def render(node):
        if isinstance(node, NavigableString):
            return re.sub(r"\s+", " ", str(node))
        text = "".join(render(child) for child in node.children)
        name = node.name or ""
        if name == "a" and node.get("href"):
            return f"[{text.strip()}]({urljoin(url, node['href'])})"
        if re.fullmatch(r"h[1-6]", name):
            return f"\n\n{'#' * int(name[1:])} {text.strip()}\n\n"
        if name in {"td", "th"}:
            return text.strip().replace("\n", " ").replace("|", "\\|") + " | "
        if name == "tr":
            return "\n| " + text.strip()
        if name == "br":
            return "\n"
        if name in {
            "p",
            "div",
            "section",
            "article",
            "table",
            "ul",
            "ol",
            "li",
            "pre",
            "blockquote",
        }:
            return "\n\n" + text.strip() + "\n\n"
        return text

    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", render(soup)).strip()
