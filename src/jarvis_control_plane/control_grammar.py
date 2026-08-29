"""Compatibility alias for deterministic command handling."""

import sys as _sys

from .application.commands import handling as _implementation

_sys.modules[__name__] = _implementation
