"""Background thread that runs QSPICE so the GUI stays responsive and the run can be cancelled."""
from __future__ import annotations

import threading
from pathlib import Path

from PyQt6 import QtCore

from .. import runner


class SimWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(float, int)      # elapsed seconds, result-file bytes
    succeeded = QtCore.pyqtSignal(object)         # QRaw
    failed = QtCore.pyqtSignal(str)
    cancelled = QtCore.pyqtSignal()

    def __init__(self, netlist: str, nodes: list[str], workdir: Path, qspice: Path, parent=None):
        super().__init__(parent)
        self._netlist, self._nodes, self._workdir, self._qspice = netlist, nodes, workdir, qspice
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # executed in the worker thread
        try:
            result = runner.run_netlist(self._netlist, self._nodes, self._workdir, self._qspice,
                                        progress=self.progress.emit, cancel=self._cancel)
        except runner.Cancelled:
            self.cancelled.emit()
        except Exception as exc:  # report every failure to the UI instead of killing the thread silently
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)
