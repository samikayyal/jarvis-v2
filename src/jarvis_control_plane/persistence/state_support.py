"""Compatibility alias for durable-state helper exports."""

import sys as _sys

from . import adapter_helpers as _implementation

_sys.modules[__name__] = _implementation
