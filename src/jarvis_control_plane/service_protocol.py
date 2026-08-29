"""Compatibility alias for authenticated service protocol operations."""

import sys as _sys

from .operations import service_protocol as _implementation

_sys.modules[__name__] = _implementation
