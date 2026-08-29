"""Compatibility alias for offline upgrade rehearsal operations."""

import sys as _sys

from .operations import upgrade_rehearsal as _implementation

_sys.modules[__name__] = _implementation
