"""Compatibility alias for manual-administration operations."""

import sys as _sys

from .operations import manual_admin as _implementation

_sys.modules[__name__] = _implementation
