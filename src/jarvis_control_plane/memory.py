"""Compatibility alias for durable-memory application behavior."""

import sys as _sys

from .application import memory as _implementation

_sys.modules[__name__] = _implementation
