"""Render the documentation screenshots headlessly (offscreen Qt) into ../docs/.

    python Qspice/Simulator/tools/make_screenshots.py

Runs the TestBoard_ADC124 two-pulse preset, holds it as the reference, raises the mux leakage 10x and runs again, so the
result screenshot shows the reference (dashed) against the new run. A last run with an 8-bit ADC and a little noise shows
ADC-Out, the Force tab and the calculated force.
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


def show_tab(win, name: str) -> None:
    names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    win.tabs.setCurrentIndex(names.index(name))
    QtTest.QTest.qWait(150)


def shot(win, name: str) -> None:
    win.grab().save(str(DOCS / name))


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    app = QtWidgets.QApplication([])
    models = [m for m in model_loader.discover([SIM_DIR.parent]) if m.id == "TestBoard_ADC124"]
    win = MainWindow(models)
    win.settings.clear()
    win.resize(1500, 920)
    win.show()
    win._apply_preset(win.preset_combo.findData("two_pulses"))
    win.viz["nodes"] = {"ain", "s2", "s4", "f2", "f4"}
    win._populate_nodes()
    for name in ("Model", "Components", "System", "Simulation"):
        show_tab(win, name)
        shot(win, f"tab_{name.lower()}.png")
    show_tab(win, "Simulation")
    win.run()
    wait_for_run(win)
    win.hold_reference()
    win.cfg["mux"]["ileak_s_off_nA"] = 10.0
    win._refresh_all_forms()
    win._on_param_changed("mux.ileak_s_off_nA")
    show_tab(win, "Components")
    win.run()
    wait_for_run(win)
    shot(win, "result_with_reference.png")

    # ADC-Out (8 bit, 2 mV noise), calculated force and the Force tab
    win.clear_reference()
    win.cfg["mux"]["ileak_s_off_nA"] = 1.0
    win.cfg["adc"]["bits"] = 8
    win.cfg["simulation"]["adc_noise_mV"] = 2.0
    win._refresh_all_forms()
    win.viz["nodes"] = {"ain", "adcout_ch2", "adcout_ch4", "df_ch2", "fcal_ch2", "fratio_ch2"}
    win.viz["show_noisy"] = True
    win.viz["overlay_applied"] = True
    win._populate_nodes()
    win._apply_viz_widgets()
    win.run()
    wait_for_run(win)
    show_tab(win, "Visualization")
    shot(win, "tab_visualization.png")
    show_tab(win, "Force")
    QtTest.QTest.qWait(150)
    shot(win, "tab_force.png")
    # the whole formula panel (long: the tab scrolls it)
    full = QtWidgets.QTextBrowser()
    full.setHtml(win.force_tab.formulas.toHtml())
    full.resize(980, 1500)
    full.show()
    QtTest.QTest.qWait(150)
    full.grab().save(str(DOCS / "tab_force_formulas.png"))
    full.close()
    show_tab(win, "Simulation")
    shot(win, "result_adc_force.png")
    print("screenshots written to", DOCS)


if __name__ == "__main__":
    main()
