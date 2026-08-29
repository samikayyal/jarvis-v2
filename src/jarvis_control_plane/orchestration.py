"""Compatibility alias for the Agents SDK orchestration adapter."""

import sys as _sys

from .application.orchestration import adapter as _implementation

_sys.modules[__name__] = _implementation
