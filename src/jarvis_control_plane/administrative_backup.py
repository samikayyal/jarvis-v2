"""Compatibility alias for administrative backup operations."""

import sys as _sys

from .operations import backup as _implementation

_sys.modules[__name__] = _implementation
