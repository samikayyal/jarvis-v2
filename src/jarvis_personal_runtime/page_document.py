"""Content-preserving HTML conversion and heading-addressed reader chunks."""

import re
from urllib.parse import urljoin

import tiktoken
from bs4 import BeautifulSoup, NavigableString

ENCODING = tiktoken.get_encoding("o200k_base")


def tokens(text: str) -> int:
    return len(ENCODING.encode(text, disallowed_special=()))


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
            return f"\n\n{'#' * int(name[1])} {text.strip()}\n\n"
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


def split_document(text: str, threshold: int) -> list[dict]:
    """Group sections; split long paragraphs/rows with their structural context."""
    if tokens(text) <= threshold:
        return [{"id": "1", "heading": "Whole document", "text": text}]
    chunks = []
    heading = ""
    headers = ""
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(
                {
                    "id": str(len(chunks) + 1),
                    "heading": " / ".join(
                        re.findall(r"^#{1,6} .+", current, re.MULTILINE)
                    )
                    or heading
                    or "Document",
                    "text": current.strip(),
                }
            )
        current = ""

    for block in re.split(r"\n\s*\n", text):
        is_heading = block.startswith("#")
        if is_heading:
            headers = ""
            # Keep a section's heading with the section, not the preceding chunk.
            if tokens(current + "\n\n" + block) > threshold // 2:
                flush()
            heading = block
        rows = block.splitlines() if block.startswith("|") else [block]
        if block.startswith("|"):
            headers = rows[0]
        for row in rows:
            if tokens(current + "\n\n" + row) <= threshold:
                current += "\n\n" + row
                continue
            flush()
            prefix = "\n\n".join(
                part for part in (heading, headers if row != headers else "") if part
            )
            # Very large headings/rows still obey the configured chunk bound.
            if tokens(prefix) > threshold // 3:
                prefix = ENCODING.decode(
                    ENCODING.encode(prefix, disallowed_special=())[: threshold // 3]
                )
            encoded = ENCODING.encode(row, disallowed_special=())
            size = max(1, threshold - tokens(prefix) - 8)
            for offset in range(0, len(encoded), size):
                if current:
                    flush()
                current = (
                    prefix + "\n\n" + ENCODING.decode(encoded[offset : offset + size])
                )
    flush()
    return chunks
