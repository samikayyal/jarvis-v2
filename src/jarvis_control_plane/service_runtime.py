"""Compatibility alias for service runtime operations."""

import sys as _sys

from .operations import service_runtime as _implementation

_sys.modules[__name__] = _implementation
