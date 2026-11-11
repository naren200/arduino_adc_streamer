"""Thread-safe hand-off from the GUI thread to the force worker."""

from __future__ import annotations

import threading
from collections import deque


class ForceWorkMailbox:
    """Holds one replaceable batch and an ordered, never-dropped control queue.

    A newer batch replaces the pending one so the newest data wins. Control
    items (engine swap, reset, stop) are never replaced by a batch and are taken
    before it. Posting a control item discards the pending batch, because that
    batch was built for the state the control item is about to replace.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._control: deque[object] = deque()
        self._pending_batch: object | None = None
        self._wake = threading.Event()

    def put_batch(self, batch: object) -> object | None:
        """Store ``batch``; return the batch it replaced, if any."""
        with self._lock:
            replaced, self._pending_batch = self._pending_batch, batch
        self._wake.set()
        return replaced

    def post_control(self, item: object) -> None:
        with self._lock:
            self._pending_batch = None
            self._control.append(item)
        self._wake.set()

    def drop_pending_batch(self) -> None:
        with self._lock:
            self._pending_batch = None

    def take(self, timeout: float = 0.0) -> object | None:
        """Return the next item, waiting up to ``timeout`` seconds; None if nothing arrived."""
        item = self._pop()
        if item is not None or timeout <= 0.0:
            return item
        self._wake.wait(timeout)
        self._wake.clear()
        return self._pop()

    def _pop(self) -> object | None:
        with self._lock:
            if self._control:
                return self._control.popleft()
            batch, self._pending_batch = self._pending_batch, None
            return batch
