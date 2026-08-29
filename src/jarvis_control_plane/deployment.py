"""Compatibility alias for deployment validation operations."""

import sys as _sys

from .operations import deployment as _implementation

_sys.modules[__name__] = _implementation
