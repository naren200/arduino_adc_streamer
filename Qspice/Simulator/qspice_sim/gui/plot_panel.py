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
    kind: str = "line"          # "stair": derived value held until the next conversion (NaN = undefined, drawn as a gap)
    dashed: bool = False


@dataclass
class Marker:
    node: str                   # shown on the panel that holds the trace of this node
    label: str
    x: np.ndarray
    y: np.ndarray
    color: str | None = None    # None: white marker with a black edge


def log_magnitude(v: np.ndarray) -> np.ndarray:
    """|v| with zeros left out (NaN): what a log axis can show of a bipolar signal."""
    out = np.abs(np.asarray(v, dtype=float))
    out[out == 0] = np.nan
    return out


SI_UNITS = ("V", "A", "N", "s", "F", "Hz")


def split_units(title: str) -> tuple[str, str | None]:
    """"ADC input [V]" -> ("ADC input", "V"): the axis then scales itself with an SI prefix (1 mV) instead of "(x0.001)"."""
    for u in SI_UNITS:
        suffix = f" [{u}]"
        if title.endswith(suffix):
            return title[:-len(suffix)], u
    return title, None


class Log10Axis(pg.AxisItem):
    """Y axis of a logarithmic panel: the ticks show log10 of the value (0 = 1, 1 = 10, -1 = 0.1), like the plotted position,
    instead of the pyqtgraph default that labels a log axis with the real values and mixes 10^n with plain numbers."""

    def tickStrings(self, values, scale, spacing):
        return [f"{v:g}" for v in values]


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

    def _clear_layout(self) -> None:
        """Remove the panels. A replot can arrive while the mouse button is held on a plot (a drag or zoom in progress); the scene
        then still points at the deleted ViewBox and pyqtgraph raises "wrapped C/C++ object of type ViewBox has been deleted" on
        every following mouse event, so the drag state is dropped first."""
        scene = self.glw.scene()
        scene.dragItem = None
        scene.dragButtons = []
        scene.lastDrag = None
        scene.clickEvents = []
        scene.hoverItems = type(scene.hoverItems)() if hasattr(scene, "hoverItems") else {}
        self.glw.clear()

    def _empty_hint(self) -> None:
        self._clear_layout()
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
                    x_window: tuple[Optional[float], Optional[float]] = (None, None), title: str = "",
                    log_groups: set[str] | None = None, hlines: dict[str, float] | None = None) -> None:
        """Draw the traces. ``log_groups``: plot groups shown with a logarithmic Y axis (a panel is logarithmic when all its
        traces belong to such groups; bipolar values are drawn as |y|, zeros are left out). ``hlines``: {group: y} reference lines."""
        self._clear_layout()
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
        log_groups = log_groups or set()
        hlines = hlines or {}
        master = None
        for i, (panel_title, members) in enumerate(buckets):
            is_log = bool(members) and all(t.group in log_groups for t in members)
            plot = self.glw.addPlot(row=i, col=0, title=title if (i == 0 and title) else None,
                                    axisItems={"left": Log10Axis("left")} if is_log else None)
            plot.showGrid(x=True, y=True, alpha=0.25)
            if is_log:
                plot.setLabel("left", f"log10 |{panel_title}|")
            else:
                text, units = split_units(panel_title)
                plot.setLabel("left", text, units=units)      # with a unit pyqtgraph writes "(mV)", without it "(x0.001)"
            if is_log:
                plot.setLogMode(y=True)
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
            if is_log:        # a log axis shows |y|; the envelope is then taken of the magnitude
                members = [Trace(t.node, t.label, t.group, t.t, log_magnitude(t.v), t.kind, t.dashed) for t in members]
            for tr in members:
                color = self.color_for(tr.node)
                ref = reference.get(tr.node)
                gaps = tr.kind == "stair" or is_log                       # NaN = undefined: draw a gap
                if ref is not None and is_log:
                    ref = (ref[0], log_magnitude(ref[1]))
                if ref is not None:
                    plot.plot(ref[0], ref[1], pen=pg.mkPen(color, width=line_width, style=QtCore.Qt.PenStyle.DashLine),
                              name=f"ref: {tr.label}", skipFiniteCheck=not gaps, connect="finite" if gaps else "all")
                style = QtCore.Qt.PenStyle.DashLine if tr.dashed else QtCore.Qt.PenStyle.SolidLine
                item = plot.plot([], [], pen=pg.mkPen(color, width=line_width, style=style), name=tr.label,
                                 skipFiniteCheck=not gaps, connect="finite" if gaps else "all")
                curves.append((tr, item))
            for gid in {t.group for t in members}:
                if gid in hlines:                                          # for example the ratio Fcal / Fin = 1
                    y = hlines[gid]
                    plot.addItem(pg.InfiniteLine(pos=float(np.log10(y)) if is_log else y, angle=0, movable=False,
                                                 pen=pg.mkPen("#888888", width=1, style=QtCore.Qt.PenStyle.DotLine)))
            for mk in markers:
                if any(t.node == mk.node for t in members):
                    brush = pg.mkBrush(mk.color or "#ffffff")
                    mx, my = mk.x, mk.y
                    if is_log:                      # scatter items do not know log mode: place them at log10 |y|
                        keep = np.isfinite(my) & (my != 0)
                        mx, my = mx[keep], np.log10(np.abs(my[keep]))
                    sp = pg.ScatterPlotItem(mx, my, size=6, pen=pg.mkPen("k", width=0.8), brush=brush, name=mk.label)
                    plot.addItem(sp)
            finite = [t.v[np.isfinite(t.v)] for t in members]
            finite = [a for a in finite if len(a)]
            lo = min(float(np.min(a)) for a in finite) if finite else 0.0
            hi = max(float(np.max(a)) for a in finite) if finite else 0.0
            if finite and not is_log and hi - lo <= 1e-12 * max(1.0, abs(hi)):
                # every trace is constant (for example the force with no input): a fixed range around the value instead
                # of the huge auto range pyqtgraph picks for a flat line
                half = 0.5 if hi == 0 else 0.1 * abs(hi)
                plot.setYRange(lo - half, hi + half, padding=0)
            self._panels.append({"plot": plot, "curves": curves, "log": is_log})
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
                y = 10 ** p.y() if panel.get("log") else p.y()
                self.cursor_text.emit(f"t = {pg.siFormat(p.x(), suffix='s')}    " + (f"|y| = {y:.6g} (log10 = {p.y():.4g})" if panel.get("log") else f"y = {y:.6g}"))
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
