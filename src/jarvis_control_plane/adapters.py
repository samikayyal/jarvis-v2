"""Compatibility import path for the controlled local adapter surface."""

import sys as _sys

from .persistence import adapters as _implementation

_sys.modules[__name__] = _implementation
