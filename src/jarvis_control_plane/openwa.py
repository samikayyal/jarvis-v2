"""Compatibility facade for the canonical OpenWA connector implementation."""

from __future__ import annotations

import sys as _sys
from types import ModuleType as _ModuleType

from .integrations.openwa import connector as _connector

for _name, _value in vars(_connector).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


class _OpenWACompatibilityModule(_ModuleType):
    """Keep legacy module monkeypatches visible to the canonical module."""

    _connector_module = _connector

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if not name.startswith("__"):
            setattr(type(self)._connector_module, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        if not name.startswith("__"):
            try:
                delattr(type(self)._connector_module, name)
            except AttributeError:
                pass


_sys.modules[__name__].__class__ = _OpenWACompatibilityModule

del _ModuleType, _OpenWACompatibilityModule, _connector, _name, _sys, _value
