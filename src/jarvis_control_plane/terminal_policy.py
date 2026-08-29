"""Compatibility alias for the application terminal policy."""

import sys as _sys

from .application.policies import terminal as _implementation

_sys.modules[__name__] = _implementation
