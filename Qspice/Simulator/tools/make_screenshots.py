"""Render the documentation screenshots headlessly (offscreen Qt) into ../docs/.

    python Qspice/Simulator/tools/make_screenshots.py

Runs the TestBoard_ADC124 two-pulse preset, holds it as the reference, raises the mux leakage 10x and runs again, so the
result screenshot shows the reference (dashed) against the new run.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

SIM_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SIM_DIR))

from PyQt6 import QtTest, QtWidgets  # noqa: E402

from qspice_sim import model_loader  # noqa: E402
from qspice_sim.gui.main_window import MainWindow  # noqa: E402

DOCS = SIM_DIR / "docs"


def wait_for_run(win):
    QtTest.QTest.qWait(300)
    while win.worker is not None:
        QtTest.QTest.qWait(200)
    QtTest.QTest.qWait(400)


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    app = QtWidgets.QApplication([])
    win = MainWindow(model_loader.discover([SIM_DIR.parent]))
    win.settings.clear()
    win.resize(1500, 920)
    win.show()
    win._apply_preset(win.preset_combo.findData("two_pulses"))
    win.viz["nodes"] = {"ain", "s2", "s4", "f2", "f4"}
    win._populate_nodes()
    for i, name in enumerate(["model", "components", "system", "simulation", "visualization"]):
        win.tabs.setCurrentIndex(i)
        QtTest.QTest.qWait(150)
        win.grab().save(str(DOCS / f"tab_{name}.png"))
    win.tabs.setCurrentIndex(3)
    win.run()
    wait_for_run(win)
    win.hold_reference()
    win.cfg["mux"]["ileak_s_off_nA"] = 10.0
    win._refresh_all_forms()
    win._on_param_changed("mux.ileak_s_off_nA")
    win.tabs.setCurrentIndex(1)
    win.run()
    wait_for_run(win)
    win.grab().save(str(DOCS / "result_with_reference.png"))
    print("screenshots written to", DOCS)


if __name__ == "__main__":
    main()
