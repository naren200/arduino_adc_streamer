"""Result plot: stacked pyqtgraph panels with zoom-aware min/max decimation, reference overlay, sample markers, image export."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters  # noqa: F401  (registers the exporters)
from PyQt6 import QtCore, QtWidgets

from ..decimate import envelope

pg.setConfigOptions(background="w", foreground="k", antialias=False)

PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2", "#17becf",
           "#bcbd22", "#7f7f7f", "#393b79", "#ad494a", "#637939", "#a55194"]


@dataclass
class Trace:
    node: str
    label: str
    group: str
    t: np.ndarray
    v: np.ndarray


@dataclass
class Marker:
    node: str
    label: str
    x: np.ndarray
    y: np.ndarray


class PlotPanel(QtWidgets.QWidget):
    cursor_text = QtCore.pyqtSignal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.glw = pg.GraphicsLayoutWidget()
        lay.addWidget(self.glw)
        self._colors: dict[str, str] = {}
        self._panels: list[dict] = []
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._refresh_curves)
        self._proxy: Optional[pg.SignalProxy] = None
        self._empty_hint()

    # ------------------------------------------------------------------ helpers
    def color_for(self, node: str) -> str:
        if node not in self._colors:
            self._colors[node] = PALETTE[len(self._colors) % len(PALETTE)]
        return self._colors[node]

    def _empty_hint(self) -> None:
        self.glw.clear()
        self._panels = []
        plot = self.glw.addPlot()
        plot.hideAxis("bottom")
        plot.hideAxis("left")
        text = pg.TextItem("Set the parameters on the left, then press Run.", color="#888888", anchor=(0.5, 0.5))
        plot.addItem(text)
        text.setPos(0.5, 0.5)
        plot.setXRange(0, 1)
        plot.setYRange(0, 1)

    def clear(self) -> None:
        self._empty_hint()

    def has_data(self) -> bool:
        return bool(self._panels)

    def visible_range(self) -> tuple[float, float]:
        if not self._panels:
            return 0.0, 0.0
        x0, x1 = self._panels[0]["plot"].getViewBox().viewRange()[0]
        return float(x0), float(x1)

    # ------------------------------------------------------------------ plotting
    def show_traces(self, traces: list[Trace], groups: list[dict], layout: str = "group", markers: list[Marker] | None = None,
                    reference: dict[str, tuple[np.ndarray, np.ndarray]] | None = None, line_width: float = 1.2,
                    x_window: tuple[Optional[float], Optional[float]] = (None, None), title: str = "") -> None:
        self.glw.clear()
        self._panels = []
        self._proxy = None
        if not traces:
            self._empty_hint()
            return
        titles = {g["id"]: g["title"] for g in groups}
        if layout == "overlay":
            buckets = [("Selected nodes", traces)]
        elif layout == "node":
            buckets = [(f"{t.label}", [t]) for t in traces]
        else:
            order = [g["id"] for g in groups]
            buckets = []
            for gid in order:
                members = [t for t in traces if t.group == gid]
                if members:
                    buckets.append((titles.get(gid, gid), members))
        markers = markers or []
        reference = reference or {}
        master = None
        for i, (panel_title, members) in enumerate(buckets):
            plot = self.glw.addPlot(row=i, col=0, title=title if (i == 0 and title) else None)
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.setLabel("left", panel_title)
            plot.addLegend(offset=(8, 4), labelTextSize="9pt")
            plot.getViewBox().setAutoVisible(y=True)
            plot.enableAutoRange(axis="y")
            last = i == len(buckets) - 1
            if last:
                plot.setLabel("bottom", "Time", units="s")
            else:
                plot.getAxis("bottom").setStyle(showValues=False)
            if master is None:
                master = plot
                plot.getViewBox().sigXRangeChanged.connect(lambda *_: self._timer.start())
            else:
                plot.setXLink(master)
            curves = []
            for tr in members:
                color = self.color_for(tr.node)
                ref = reference.get(tr.node)
                if ref is not None:
                    plot.plot(ref[0], ref[1], pen=pg.mkPen(color, width=line_width, style=QtCore.Qt.PenStyle.DashLine),
                              name=f"ref: {tr.label}", skipFiniteCheck=True)
                item = plot.plot([], [], pen=pg.mkPen(color, width=line_width), name=tr.label, skipFiniteCheck=True)
                curves.append((tr, item))
            for mk in markers:
                if any(t.node == mk.node for t in members):
                    sp = pg.ScatterPlotItem(mk.x, mk.y, size=6, pen=pg.mkPen("k", width=0.8), brush=pg.mkBrush("#ffffff"), name=mk.label)
                    plot.addItem(sp)
            lo = min(float(np.min(t.v)) for t in members if len(t.v))
            hi = max(float(np.max(t.v)) for t in members if len(t.v))
            if hi - lo <= 1e-12 * max(1.0, abs(hi)):
                # every trace is constant (for example the force with no input): a fixed range around the value instead
                # of the huge auto range pyqtgraph picks for a flat line
                half = 0.5 if hi == 0 else 0.1 * abs(hi)
                plot.setYRange(lo - half, hi + half, padding=0)
            self._panels.append({"plot": plot, "curves": curves})
        t_all_min = min(float(t.t[0]) for t in traces if len(t.t))
        t_all_max = max(float(t.t[-1]) for t in traces if len(t.t))
        x0 = t_all_min if x_window[0] is None else x_window[0]
        x1 = t_all_max if x_window[1] is None else x_window[1]
        if x1 <= x0:
            x0, x1 = t_all_min, t_all_max
        master.setXRange(x0, x1, padding=0.01)
        self._refresh_curves()
        self._proxy = pg.SignalProxy(self.glw.scene().sigMouseMoved, rateLimit=30, slot=self._mouse_moved)

    def _refresh_curves(self) -> None:
        if not self._panels:
            return
        x0, x1 = self.visible_range()
        n = max(400, int(self.glw.width() * 0.9))
        for panel in self._panels:
            for tr, item in panel["curves"]:
                x, y = envelope(tr.t, tr.v, x0, x1, n_buckets=n)
                item.setData(x, y)

    def _mouse_moved(self, evt) -> None:
        pos = evt[0]
        for panel in self._panels:
            vb = panel["plot"].getViewBox()
            if vb.sceneBoundingRect().contains(pos):
                p = vb.mapSceneToView(pos)
                self.cursor_text.emit(f"t = {pg.siFormat(p.x(), suffix='s')}    y = {p.y():.6g}")
                return

    # ------------------------------------------------------------------ export
    def save_image(self, path: str, width: int = 1600) -> None:
        if not self._panels:
            raise ValueError("nothing to save: run a simulation first")
        if path.lower().endswith(".svg"):
            exporter = pg.exporters.SVGExporter(self.glw.ci)
        else:
            exporter = pg.exporters.ImageExporter(self.glw.ci)
            exporter.parameters()["width"] = int(width)
        exporter.export(path)
