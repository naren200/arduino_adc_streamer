"""PlotPanel: a replot while the mouse is held on a plot must not leave the scene pointing at a deleted ViewBox."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PyQt6 import QtCore, QtTest, QtWidgets  # noqa: E402

from qspice_sim.gui.plot_panel import PlotPanel, Trace  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _traces():
    t = np.linspace(0, 1e-3, 500)
    return [Trace("a", "a", "g", t, np.sin(t * 1e4))], [{"id": "g", "title": "V"}]


def test_replot_during_a_drag_drops_the_drag_state(app):
    panel = PlotPanel()
    panel.resize(600, 400)
    panel.show()
    traces, groups = _traces()
    panel.show_traces(traces, groups)
    QtTest.QTest.qWait(50)
    viewport = panel.glw.viewport()
    QtTest.QTest.mousePress(viewport, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(300, 200))
    QtTest.QTest.mouseMove(viewport, QtCore.QPoint(340, 220))
    QtTest.QTest.mouseMove(viewport, QtCore.QPoint(360, 230))
    scene = panel.glw.scene()
    assert scene.dragItem is not None                                       # the drag is in progress
    panel.show_traces(traces, groups)                                       # a replot while the button is held
    assert scene.dragItem is None and not scene.dragButtons and not scene.clickEvents
    QtTest.QTest.mouseMove(viewport, QtCore.QPoint(380, 240))               # used to raise "ViewBox has been deleted"
    QtTest.QTest.mouseRelease(viewport, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(380, 240))
    panel.close()
