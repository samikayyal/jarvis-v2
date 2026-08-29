"""Compatibility alias for controlled endurance operations."""

import sys as _sys

from .operations import endurance as _implementation

_sys.modules[__name__] = _implementation
