"""Compatibility facade for the legacy egress proxy import path."""

from __future__ import annotations

from .operations import egress_proxy as _implementation

MAX_PROXY_HEADER_BYTES = _implementation.MAX_PROXY_HEADER_BYTES
_PROXY_CHUNK_BYTES = _implementation._PROXY_CHUNK_BYTES
_connect_target = _implementation._connect_target
_read_available = _implementation._read_available
_read_headers = _implementation._read_headers
_relay = _implementation._relay
connect_through_proxy = _implementation.connect_through_proxy
serve_egress_proxy = _implementation.serve_egress_proxy


def main(argv: list[str] | None = None) -> int:
    """Delegate the legacy entry point to the operations implementation."""

    original_connect_through_proxy = _implementation.connect_through_proxy
    _implementation.connect_through_proxy = connect_through_proxy
    try:
        return _implementation.main(argv)
    finally:
        _implementation.connect_through_proxy = original_connect_through_proxy


if __name__ == "__main__":
    raise SystemExit(main())
