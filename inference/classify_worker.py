"""
TouchID Classify Worker
========================
Background worker that runs classify_window() off the GUI thread so a slow
model (CNN/Quad/Penta on modest hardware) never blocks the event loop.
Blocking it there would stall the Signal Stream plot's redraw and the
rolling buffer's get_window() advance, letting classification silently fall
further and further behind wall-clock time -- eventually the "being
inferenced" highlight would land on a window old enough to have already
scrolled off the plot's rolling history window.
"""

from __future__ import annotations

import queue

from PyQt6.QtCore import QThread, pyqtSignal

from inference.pipeline import classify_window


class TouchIdClassifyWorker(QThread):
    """Runs classify_window() in a dedicated thread.

    The queue is bounded to 1: a fresh submission evicts a still-pending one,
    so only the latest available window is ever classified -- old windows are
    dropped rather than queued, which keeps classification from backing up
    behind real time.
    """

    result_ready = pyqtSignal(object, object)  # (probs dict, window_ts)
    error_occurred = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._queue: queue.Queue[object] = queue.Queue(maxsize=1)
        self._running = True

    def submit(self, payload: dict) -> None:
        try:
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
            self._queue.put_nowait(payload)
        except Exception:
            pass

    def run(self) -> None:
        while self._running:
            try:
                payload = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if payload is None:
                break

            try:
                probs = classify_window(
                    payload["window_adc"],
                    payload["window_integrated"],
                    payload["window_shear_lr"],
                    payload["window_shear_tb"],
                    payload["window_normal"],
                    payload["fs"],
                    payload["classifier"],
                )
                self.result_ready.emit(probs, payload["window_ts"])
            except Exception as exc:
                self.error_occurred.emit(str(exc))

    def stop(self) -> None:
        self._running = False
        try:
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
            self._queue.put_nowait(None)
        except Exception:
            pass
