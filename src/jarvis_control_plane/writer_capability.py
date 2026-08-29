"""Compatibility alias for the diagnostic writer capability."""

import sys as _sys

from .diagnostics import capability as _implementation

_sys.modules[__name__] = _implementation
