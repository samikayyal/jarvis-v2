"""Compatibility alias for application port contracts."""

import sys as _sys

from .application.ports import contracts as _implementation

_sys.modules[__name__] = _implementation
