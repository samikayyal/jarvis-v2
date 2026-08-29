"""Compatibility alias for the canonical Gmail write connector."""

import sys as _sys

from .integrations.google.gmail import connector as _implementation

_sys.modules[__name__] = _implementation
