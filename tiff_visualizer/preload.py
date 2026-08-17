"""Optional RAM preloading: copy memory-mapped stacks into RAM (within a
user-set budget) so playback and scrubbing never touch the disk."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QThread, Signal

from . import settings as app_settings

_threads: set["_Preloader"] = set()
_reserved: dict[int, int] = {}  # id(stack) -> bytes being loaded right now


def enabled() -> bool:
    return app_settings.settings().value("preload/enabled", True, type=bool)


def budget_gb() -> int:
    return app_settings.settings().value("preload/gb", 4, type=int)


def budget_bytes() -> int:
    return budget_gb() * 1024**3


def loaded_bytes() -> int:
    """RAM used by preloaded stacks, plus loads in flight."""
    from .viewer import _all_panes

    seen, total = set(), 0
    for pane in _all_panes:
        stack = pane.stack
        if id(stack) not in seen and stack.in_memory:
            seen.add(id(stack))
            total += stack.nbytes
    return total + sum(_reserved.values())


def stack_counts() -> tuple[int, int]:
    """(stacks in RAM, total open stacks)."""
    from .viewer import _all_panes

    seen: dict[int, bool] = {}
    for pane in _all_panes:
        seen[id(pane.stack)] = pane.stack.in_memory
    return sum(seen.values()), len(seen)


class _Preloader(QThread):
    done = Signal(object, object)

    def __init__(self, stack):
        super().__init__()
        self.stack = stack

    def run(self):
        try:
            arr = np.array(self.stack.data)
        except Exception:  # noqa: BLE001 - preloading is best-effort
            arr = None
        self.done.emit(self.stack, arr)


def maybe_preload(stack):
    """Copy the stack into RAM in the background if settings and budget allow."""
    if not enabled() or stack.in_memory or id(stack) in _reserved:
        return
    if loaded_bytes() + stack.nbytes > budget_bytes():
        return
    _reserved[id(stack)] = stack.nbytes
    loader = _Preloader(stack)
    _threads.add(loader)

    def on_done(s, arr):
        _reserved.pop(id(s), None)
        if arr is not None:
            s.data = arr  # swapped on the main thread; renders never race it

    loader.done.connect(on_done)
    loader.finished.connect(lambda: _threads.discard(loader))
    loader.start()


def preload_existing():
    """Apply current settings to already-open stacks (after enabling/raising)."""
    from .viewer import _all_panes

    seen = set()
    for pane in _all_panes:
        if id(pane.stack) not in seen:
            seen.add(id(pane.stack))
            maybe_preload(pane.stack)
