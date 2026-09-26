"""Cancellation and progress primitives for long-running work (Qt-free).

Services take a :class:`CancelToken` and an optional progress callback; the UI runs them on a
worker thread (see ``pdfeditor.ui.jobs``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

ProgressFn = Callable[[int, int], None]  # (done, total)


class Cancelled(Exception):
    """Raised inside a job when its token was cancelled."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()


def no_progress(_done: int, _total: int) -> None:
    pass
