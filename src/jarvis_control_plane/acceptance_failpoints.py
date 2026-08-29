"""Compatibility alias for the application failpoint implementation.

The module object is deliberately replaced with the implementation module so
legacy imports retain both public and private names.  It also means patches to
legacy helpers/constants affect the globals used by the failpoint classes.
"""

import sys as _sys

from .application import failpoints as _implementation

# Keep the legacy import path and the implementation path backed by one module
# object.  A re-export would not preserve private names or monkeypatching.
_sys.modules[__name__] = _implementation
