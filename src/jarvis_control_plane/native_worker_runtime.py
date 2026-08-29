"""Compatibility facade for the legacy native_worker_runtime module."""

from .application.compatibility import install_mirrors
from .workers import runtime as _runtime
from .workers.runtime import (
    UbuntuWorkerRuntimeConfig,
    WindowsWorkerRuntimeConfig,
    load_ubuntu_config,
    load_windows_config,
    main,
    run_ubuntu_worker,
    run_windows_service,
    run_windows_worker_loop,
)

_bounded_int = _runtime._bounded_int
_canonical_text = _runtime._canonical_text
_install_signal_handlers = _runtime._install_signal_handlers
_load_object = _runtime._load_object

__all__ = [
    "UbuntuWorkerRuntimeConfig",
    "WindowsWorkerRuntimeConfig",
    "load_ubuntu_config",
    "load_windows_config",
    "main",
    "run_ubuntu_worker",
    "run_windows_service",
    "run_windows_worker_loop",
]

install_mirrors(
    __name__,
    {
        "_bounded_int": (_runtime,),
        "_canonical_text": (_runtime,),
        "_install_signal_handlers": (_runtime,),
        "_load_object": (_runtime,),
    },
)


if __name__ == "__main__":  # pragma: no cover - exercised as a module
    raise SystemExit(main())
