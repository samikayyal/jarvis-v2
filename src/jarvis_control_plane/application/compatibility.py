"""Utilities for preserving monkeypatch-sensitive legacy module seams."""

from __future__ import annotations

import sys
from types import ModuleType


class _MirroredCompatibilityModule(ModuleType):
    """Forward assignments on a facade to the canonical implementation."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for target in self.__dict__.get("_compatibility_mirrors", {}).get(name, ()):
            setattr(target, name, value)


def install_mirrors(
    module_name: str,
    mirrors: dict[str, tuple[ModuleType, ...]],
) -> None:
    """Install assignment forwarding for one already-imported facade module."""

    module = sys.modules[module_name]
    module.__class__ = _MirroredCompatibilityModule
    module.__dict__["_compatibility_mirrors"] = mirrors
