"""Conservative display-only conversion of assistant prose for WhatsApp."""

from __future__ import annotations

import re


def _inline(text: str) -> str:
    parts = re.split(r"(`[^`\n]+`)", text)
    for index in range(0, len(parts), 2):
        part = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s]+)\)", r"\1 (\2)", parts[index])
        parts[index] = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", part)
    return "".join(parts)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def format_whatsapp(text: str) -> str:
    """Convert prose Markdown; frozen approval previews must bypass this function."""
    lines = text.splitlines()
    result: list[str] = []
    fenced = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            fenced = not fenced
            result.append("```")
        elif fenced:
            result.append(line)
        elif (
            "|" in line
            and index + 1 < len(lines)
            and len(_cells(lines[index + 1])) == len(_cells(line))
            and all(
                re.fullmatch(r":?-{3,}:?", cell) for cell in _cells(lines[index + 1])
            )
        ):
            headers = _cells(line)
            rows: list[list[str]] = []
            cursor = index + 2
            while cursor < len(lines) and "|" in lines[cursor]:
                cells = _cells(lines[cursor])
                if len(cells) != len(headers):
                    break
                rows.append(cells)
                cursor += 1
            if rows:
                for cells in rows:
                    result.append(
                        "- "
                        + "; ".join(
                            f"{_inline(header)}: {_inline(cell)}"
                            for header, cell in zip(headers, cells, strict=True)
                        )
                    )
                index = cursor
                continue
            result.append(_inline(line))
        else:
            heading = re.fullmatch(r"\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?", line)
            result.append(
                f"*{_inline(heading[1]).strip('*')}*" if heading else _inline(line)
            )
        index += 1
    if fenced:
        result.append("```")
    return "\n".join(result)
