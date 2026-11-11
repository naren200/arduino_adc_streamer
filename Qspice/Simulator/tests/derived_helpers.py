"""Test helpers: a result set without QSPICE so derived traces can be tested headless and fast."""
from __future__ import annotations

import copy

import numpy as np

from qspice_sim import runner
from qspice_sim.gui.main_window import ResultSet


class FakeQRaw:
    """Minimal stand-in for QRaw (time axis plus named node columns)."""

    def __init__(self, time: np.ndarray, columns: dict[str, np.ndarray]):
        self.time = time
        self._cols = {k.lower(): v for k, v in columns.items()}
        self.n_points = len(time)

    def has(self, node: str) -> bool:
        return node.lower() in self._cols

    def column(self, node: str) -> np.ndarray:
        return self._cols[node.lower()]

    def nodes(self) -> list[str]:
        return list(self._cols)

    def close(self) -> None:
        pass


def install_result(window, funcs: dict, n: int = 40001) -> ResultSet:
    """Replace the window's result by one whose nodes are the given functions of time (seconds)."""
    prepared = runner.prepare(window.model, window.cfg)
    t = np.linspace(0.0, prepared.t_stop, n)
    cols = {name: np.asarray(f(t), dtype=float) for name, f in funcs.items()}
    if window.result is not None:
        window.result.qraw.close()
    window.result = ResultSet(FakeQRaw(t, cols), prepared, sorted(cols), copy.deepcopy(window.cfg), 0.0)
    window._set_result_actions(True)
    return window.result


def plot_traces(window) -> dict:
    """{node: Trace} of everything currently drawn."""
    return {tr.node: tr for panel in window.plot._panels for tr, _ in panel["curves"]}


def tick(window, *nodes: str, only: bool = True) -> None:
    """Set the ticked nodes of the Visualization tab (replaces the selection unless only=False)."""
    window.viz["nodes"] = set(nodes) | (set() if only else set(window.viz["nodes"]))
    window._populate_nodes()
    window._replot()
