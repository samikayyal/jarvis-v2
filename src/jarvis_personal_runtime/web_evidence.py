"""Bounded extraction facts shared by the worker protocol and prepared tools."""

from __future__ import annotations


def is_access_challenge(body: str, title: str = "", *, challenge: bool = False) -> bool:
    """Recognize a challenge interstitial, not prose discussing access errors."""
    if challenge:
        return True
    messages = (
        "verify you are human",
        "verify that you are human",
        "checking your browser",
        "our systems have detected unusual traffic",
        "access denied",
        "challenge required",
    )
    if title.strip().casefold().rstrip(".!…") in messages:
        return True
    # Real interstitials are short. Long articles and quoted phrases are evidence,
    # not proof of a challenge. Structured CAPTCHA signals still stop any page.
    if len(body) > 1200:
        return False
    lines = [
        line.strip().casefold().rstrip(".!…")
        for line in body.splitlines()
        if line.strip()
    ]
    return any(line in messages for line in lines[:3])


def extraction_metadata(source: dict, text: str) -> dict[str, object]:
    """Copy known facts only; a matching anchor is not proof of section coverage."""
    raw_length = source.get("source_chars")
    length = raw_length if type(raw_length) is int and raw_length >= 0 else None
    truncated = source.get("truncated") is True or (
        length is not None and length > len(text)
    )
    wait = source.get("render_wait")
    stop = source.get("stop_reason")
    result: dict[str, object] = {
        "truncated": truncated,
        "more_content_available": truncated,
        "source_chars": length,
        "extraction_method": "body_prefix",
        "section_found": source.get("section_found")
        if type(source.get("section_found")) is bool
        else None,
        "render_mode": "rendered"
        if source.get("render_mode") == "rendered"
        else "static",
        "render_wait": wait
        if isinstance(wait, str) and wait in {"matched", "timeout"}
        else "not_requested",
        "stop_reason": stop
        if isinstance(stop, str)
        and stop
        in {"page_text_limit", "evidence_limit", "output_limit", "render_wait_timeout"}
        else ("page_text_limit" if truncated else None),
    }
    return result
