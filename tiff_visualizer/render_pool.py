"""Thread pool for off-main-thread plane rendering.

Used for the many-pane paths (shared-axes ticks, playback across a grid):
workers run TiffStack.render (numpy releases the GIL for the heavy ops, so
this parallelizes across cores) and results are delivered back to the main
thread via a queued signal. The UI thread stays free to process clicks.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal

_handler = None
_bridge: "_Bridge | None" = None
_pool: ThreadPoolExecutor | None = None


class _Bridge(QObject):
    done = Signal(object, object, object, int)  # pane, cache key, rgb, request id


def set_handler(fn):
    global _handler
    _handler = fn


def _dispatch(pane, key, rgb, request_id):
    if _handler is not None:
        _handler(pane, key, rgb, request_id)


def _get_bridge() -> _Bridge:
    global _bridge
    if _bridge is None:
        _bridge = _Bridge()
        _bridge.done.connect(_dispatch)  # queued: emitted from workers
    return _bridge


def submit(pane, stack, render_args: tuple, key, request_id: int):
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 4) - 2))
    bridge = _get_bridge()

    def work():
        try:
            rgb = stack.render(*render_args)
        except Exception:  # noqa: BLE001 - a failed frame just stays stale
            return
        bridge.done.emit(pane, key, rgb, request_id)

    _pool.submit(work)
