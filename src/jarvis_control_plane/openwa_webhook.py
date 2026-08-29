"""Compatibility facade for OpenWA webhook provisioning."""

from __future__ import annotations

import sys

from .integrations.openwa.webhook import (
    TARGET_EVENTS,
    TARGET_URL,
    WebhookProvisionError,
    _http_json,
    _read_json,
    _secret,
    _session_id,
    main,
    provision_webhook,
)

__all__ = [
    "TARGET_EVENTS",
    "TARGET_URL",
    "WebhookProvisionError",
    "_http_json",
    "_read_json",
    "_secret",
    "_session_id",
    "main",
    "provision_webhook",
]


if __name__ == "__main__":
    sys.exit(main())
