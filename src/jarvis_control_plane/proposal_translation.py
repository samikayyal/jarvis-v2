"""Compatibility alias for the application proposal translator."""

import sys as _sys

from .application.proposals import translation as _implementation

_sys.modules[__name__] = _implementation
