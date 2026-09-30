"""
Queued Background Worker
==========================
Shared base for a QThread that runs one kind of work item off the GUI
thread, with a queue bounded to 1: a fresh submission evicts a still-pending
one, so only the latest requested work is ever run. A generation number
travels with every payload/result so the GUI-thread caller can drop a result
that's been superseded by a newer request (this class doesn't interrupt
in-flight work, only work that hasn't started yet -- the generation check is
what makes a stale in-flight result harmless once it does arrive).

Single implementation of this lifecycle: AnalysisComputeWorker
(prepare_analysis_data) and AnalysisSourceLoadWorker
(load_exported_csv_snapshot) both subclass this instead of each keeping
their own copy of the queue/thread/stop plumbing.
"""

from __future__ import annotations

import queue

from PyQt6.QtCore import QThread, pyqtSignal


class QueuedBackgroundWorker(QThread):
    """Runs ``self._process(payload)`` in a dedicated thread; subclasses
    override ``_process`` to do the actual work and shape its result."""

    result_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(int, str)

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

            generation = int(payload.get("generation", 0))
            try:
                result = self._process(payload)
                result["generation"] = generation
                self.result_ready.emit(result)
            except Exception as exc:
                self.error_occurred.emit(generation, str(exc))

    def _process(self, payload: dict) -> dict:
        """Do the work for one payload and return its result dict (without
        "generation" -- run() stamps that in). Must be overridden."""
        raise NotImplementedError

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
