"""Compatibility facade for the legacy Ubuntu compound runner module."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

from .workers.ubuntu_worker_runner import (
    _OPERATORS,
    COMPOUND_RESULT_MARKER,
    _decode_plan,
    _run_pipeline,
    _run_plan,
    main,
)

__all__ = [
    "COMPOUND_RESULT_MARKER",
    "_OPERATORS",
    "_decode_plan",
    "_run_pipeline",
    "_run_plan",
    "base64",
    "json",
    "main",
    "os",
    "subprocess",
    "sys",
]


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
