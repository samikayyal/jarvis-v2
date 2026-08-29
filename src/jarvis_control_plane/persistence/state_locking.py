"""Lock decorators shared by durable-state adapters."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any


def _locked_durable_state(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize one durable-state transaction on its shared state boundary."""

    @wraps(method)
    def locked(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


_locked_sqlite_state = _locked_durable_state
