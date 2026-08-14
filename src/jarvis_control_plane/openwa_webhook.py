"""Idempotently provision the single signed OpenWA inbound webhook."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

TARGET_URL = "http://inbound-receiver:9011/webhook"
TARGET_EVENTS = ["message.received"]


class WebhookProvisionError(RuntimeError):
    """Safe provisioning failure that contains no credential values."""


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookProvisionError(
            f"credential document is unreadable: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise WebhookProvisionError(f"credential document is invalid: {path}")
    return value


def _secret(document: Mapping[str, object], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise WebhookProvisionError(f"credential field is missing: {field}")
    return value


def _session_id(config_path: Path) -> str:
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        value = config["deployment"]["openwa_internal_session_id"]
    except (
        OSError,
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
    ) as exc:
        raise WebhookProvisionError(
            "OpenWA session ID is unavailable from active config"
        ) from exc
    if not isinstance(value, str) or not value or value.strip() != value:
        raise WebhookProvisionError("OpenWA session ID in active config is invalid")
    return value


def _http_json(
    method: str,
    url: str,
    api_key: str,
    payload: Mapping[str, object] | None,
    *,
    opener: Callable[..., Any] = urlopen,
) -> object:
    data = (
        None
        if payload is None
        else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    request = Request(
        url,
        data=data,
        method=method,
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=10) as response:
            raw = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise WebhookProvisionError(f"OpenWA webhook API {method} failed") from exc
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookProvisionError(
            "OpenWA webhook API returned malformed JSON"
        ) from exc


def provision_webhook(
    *,
    api_base_url: str,
    session_id: str,
    api_key: str,
    signing_secret: str,
    opener: Callable[..., Any] = urlopen,
) -> str:
    base = api_base_url.rstrip("/")
    endpoint = f"{base}/sessions/{quote(session_id, safe='')}/webhooks"
    listed = _http_json("GET", endpoint, api_key, None, opener=opener)
    if not isinstance(listed, list) or any(
        not isinstance(item, dict) for item in listed
    ):
        raise WebhookProvisionError("OpenWA webhook list response is invalid")

    exact = [item for item in listed if item.get("url") == TARGET_URL]
    conflicts = [
        item
        for item in listed
        if item.get("url") != TARGET_URL
        and isinstance(item.get("events"), list)
        and "message.received" in item["events"]
    ]
    if len(exact) > 1 or conflicts:
        raise WebhookProvisionError(
            "OpenWA has duplicate or conflicting inbound webhooks"
        )

    desired: dict[str, object] = {
        "url": TARGET_URL,
        "events": TARGET_EVENTS,
        "secret": signing_secret,
        "headers": {},
        "filters": None,
        "retryCount": 3,
    }
    if exact:
        webhook_id = exact[0].get("id")
        if not isinstance(webhook_id, str) or not webhook_id:
            raise WebhookProvisionError("existing OpenWA webhook ID is invalid")
        desired["active"] = True
        _http_json(
            "PUT",
            f"{endpoint}/{quote(webhook_id, safe='')}",
            api_key,
            desired,
            opener=opener,
        )
        result = "updated"
    else:
        _http_json("POST", endpoint, api_key, desired, opener=opener)
        result = "created"

    verified = _http_json("GET", endpoint, api_key, None, opener=opener)
    matching = (
        [
            item
            for item in verified
            if isinstance(item, dict) and item.get("url") == TARGET_URL
        ]
        if isinstance(verified, list)
        else []
    )
    if len(matching) != 1:
        raise WebhookProvisionError("OpenWA webhook verification count is not one")
    row = matching[0]
    if (
        row.get("events") != TARGET_EVENTS
        or row.get("active") is not True
        or row.get("retryCount") != 3
    ):
        raise WebhookProvisionError(
            "OpenWA webhook verification differs from reviewed settings"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base-url", default="http://127.0.0.1:2785/api")
    parser.add_argument("--config", type=Path, default=Path("/etc/jarvis/jarvis.toml"))
    parser.add_argument(
        "--api-credential",
        type=Path,
        default=Path("/etc/jarvis/credentials/openwa/credentials.json"),
    )
    parser.add_argument(
        "--signing-credential",
        type=Path,
        default=Path("/etc/jarvis/credentials/openwa-inbound/credentials.json"),
    )
    args = parser.parse_args(argv)
    try:
        result = provision_webhook(
            api_base_url=args.api_base_url,
            session_id=_session_id(args.config),
            api_key=_secret(_read_json(args.api_credential), "api_key"),
            signing_secret=_secret(
                _read_json(args.signing_credential), "openwa_signing_secret"
            ),
        )
    except WebhookProvisionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OpenWA Jarvis inbound webhook {result} and verified (count=1)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
