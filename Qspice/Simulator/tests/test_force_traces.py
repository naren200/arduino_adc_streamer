"""B6: dF, F and Fcal / Fin as derived traces, applied-force overlay and the log scale of the plot panels."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import numpy as np
import pytest

from qspice_sim import model_loader, runner
from qspice_sim.force_calc import FORCE_DEFAULTS, compute_channel, force_ratio, resolve_params
from test_force_calc import INFO, _table

QSPICE_DIR = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ the ratio
def test_ratio_is_one_for_a_perfect_extraction_and_undefined_below_the_threshold():
    sens = 10.0
    force = np.array([0.0, 0.001, 0.02, 0.05, 0.05, -0.04])
    table = _table(1.25 + sens * force, bits=16)
    cf = compute_channel(table, 3, resolve_params(FORCE_DEFAULTS, INFO), FORCE_DEFAULTS, 1.25, lambda ch, t: force, lambda ch: 0.05)
    assert cf.ratio_threshold == pytest.approx(0.0005)                       # 1 % of the largest |Fin| of the run
    assert np.isnan(cf.ratio[0])                                             # exact zero is always masked
    assert cf.ratio[2:] == pytest.approx(1.0, abs=1e-3) and cf.ratio[1] == pytest.approx(1.0, abs=0.03)
    cf2 = compute_channel(table, 3, resolve_params(FORCE_DEFAULTS, INFO), dict(FORCE_DEFAULTS, min_fin_N=0.01), 1.25, lambda ch, t: force)
    assert cf2.ratio_threshold == 0.01 and np.isnan(cf2.ratio[:2]).all() and not np.isnan(cf2.ratio[2:]).any()
    assert np.array_equal(force_ratio(np.array([1.0, 2.0]), np.array([0.0, 4.0]), 0.0)[1:], [0.5]) and np.isnan(force_ratio(
        np.array([1.0]), np.array([0.0]), 0.0)).all()
    under = compute_channel(_table(1.25 + sens * force * 0.5, bits=16), 3, resolve_params(FORCE_DEFAULTS, INFO), FORCE_DEFAULTS, 1.25,
                            lambda ch, t: force)
    assert under.ratio[3] == pytest.approx(0.5, abs=1e-3)                    # below 1 = under-estimated force


# ------------------------------------------------------------------ GUI
pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pyqtgraph as pg  # noqa: E402
from PyQt6 import QtWidgets  # noqa: E402

from derived_helpers import install_result, plot_traces, tick  # noqa: E402
from qspice_sim.gui.main_window import MainWindow  # noqa: E402

SENS = 600e-12 / 60.7e-12


def _force(t):
    return 0.05 * np.clip((t - 1.5e-4) / 2.0e-4, 0.0, 1.0) - 0.02 * (t > 4.2e-4)      # ramp up, then a step below the start


def _ain(t):
    return 1.65 + SENS * _force(t) * 0.9                                              # the circuit delivers 90 % of the charge


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture()
def window(app):
    win = MainWindow([model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124"), model_loader.load_model(QSPICE_DIR / "TestBoard_7953")])
    win.settings.clear()
    win.resize(1500, 900)
    win.show()
    install_result(win, {"ain": _ain, "f2": _force, "f4": _force})
    yield win
    win.close()


def _panel_of(window, node):
    for panel in window.plot._panels:
        if any(tr.node == node for tr, _ in panel["curves"]):
            return panel
    raise KeyError(node)


def _at(window, ch):
    mask = window.sample_table.channel_mask(ch)
    return window.sample_table.t[mask]


def test_force_nodes_are_in_the_tree_and_the_plot_groups_exist(window):
    groups = {g["id"]: g["title"] for g in window.model.plot_groups()}
    assert groups["fcal"] == "Calculated force [N]" and groups["fratio"] == "Fcal / Fin [ratio]"
    labels = {window.node_tree.topLevelItem(i).text(0): window.node_tree.topLevelItem(i) for i in range(window.node_tree.topLevelItemCount())}
    assert {"Calculated force [N]", "Fcal / Fin [ratio]"} <= set(labels)
    assert [labels["Calculated force [N]"].child(j).text(0) for j in range(4)] == ["dF ch0", "F ch0", "dF ch1", "F ch1"]
    assert labels["Fcal / Fin [ratio]"].child(2).text(0) == "Fcal/Fin ch2" and labels["Fcal / Fin [ratio]"].childCount() == 7
    info = window.model.derived_info()
    assert info["fratio_ch2"]["sources"] == ["ain", "f2"] and info["fcal_ch2"]["sources"] == ["ain"]


def test_df_f_and_the_ratio_are_shown_per_channel_and_F_is_the_cumulative_sum(window):
    tick(window, "df_ch2", "fcal_ch2", "fratio_ch2")
    traces = plot_traces(window)
    assert set(traces) == {"df_ch2", "fcal_ch2", "fratio_ch2"}
    assert traces["df_ch2"].group == traces["fcal_ch2"].group == "fcal" and traces["fratio_ch2"].group == "fratio"
    assert len(window.plot._panels) == 2                                       # Calculated force panel and ratio panel
    series = window._derived_series
    t = _at(window, 2)
    assert series["df_ch2"]["t"].tolist() == t.tolist() and len(t) == 4
    assert series["fcal_ch2"]["v"] == pytest.approx(np.cumsum(series["df_ch2"]["v"]))
    # stairs: one value per conversion, held until the next one
    assert traces["fcal_ch2"].v[0::2].tolist() == pytest.approx(series["fcal_ch2"]["v"].tolist())
    # the circuit delivers 90 % of the charge, so the calculated force is about 0.9 of the applied one
    fin = np.interp(t, window.result.qraw.time, _force(window.result.qraw.time))
    ratio = series["fratio_ch2"]["v"]
    expected = np.where((np.abs(fin) >= 0.01 * np.max(np.abs(_force(window.result.qraw.time)))) & (fin != 0),
                        series["fcal_ch2"]["v"] / np.where(fin == 0, 1, fin), np.nan)
    assert np.array_equal(np.isnan(ratio), np.isnan(expected)) and ratio[~np.isnan(ratio)] == pytest.approx(expected[~np.isnan(expected)])
    assert 0.8 < np.nanmax(ratio) < 1.01 and np.isnan(ratio).any() and (~np.isnan(ratio)).any()
    tick(window, "fcal_ch2")                                                   # unticking removes the others
    assert set(plot_traces(window)) == {"fcal_ch2"}


def test_ratio_threshold_setting_masks_more_points_and_replots_without_a_run(window):
    tick(window, "fratio_ch2")
    base = window._derived_series["fratio_ch2"]["v"].copy()
    row = next(r for r in window.force_tab.form.rows if r.path == "force.min_fin_N")
    assert row.editor.placeholderText() == "1 % of max |Fin|"
    row.editor.setText("0.04")
    row.editor.editingFinished.emit()
    assert window.worker is None
    masked = window._derived_series["fratio_ch2"]["v"]
    assert np.isnan(masked).sum() > np.isnan(base).sum()
    assert window.force["min_fin_N"] == 0.04
    row._on_reset()                                                            # back to 1 % of the largest |Fin|
    assert np.array_equal(window._derived_series["fratio_ch2"]["v"], base, equal_nan=True)


def test_an_unsaved_applied_force_is_reported_for_the_ratio_only(window):
    install_result(window, {"ain": _ain})                                       # the force node was not saved
    tick(window, "df_ch2", "fratio_ch2")
    traces = plot_traces(window)
    assert set(traces) == {"df_ch2"}
    assert "applied force f2 was not saved (press Run to include it)" in window.status_label.text()


def test_source_nodes_are_pulled_into_the_run(window):
    window.cfg["forces"] = []
    prepared = runner.prepare(window.model, window.cfg)
    window.viz["nodes"] = {"fcal_ch3"}
    assert window._nodes_to_save(prepared) == ["ain"]
    window.viz["nodes"] = {"fratio_ch3"}
    assert window._nodes_to_save(prepared) == ["ain", "f3"]
    window.viz["overlay_applied"] = True
    window.viz["nodes"] = {"df_ch3", "fcal_ch4"}
    assert window._nodes_to_save(prepared) == ["ain", "f3", "f4"]


def test_overlay_draws_the_applied_force_dashed_on_the_calculated_force_panel(window):
    tick(window, "fcal_ch2")
    assert set(plot_traces(window)) == {"fcal_ch2"}
    window.overlay_chk.setChecked(True)
    traces = plot_traces(window)
    assert set(traces) == {"fcal_ch2", "applied_ch2"} and traces["applied_ch2"].dashed and traces["applied_ch2"].label == "Applied force ch2"
    assert _panel_of(window, "applied_ch2") is _panel_of(window, "fcal_ch2")
    assert traces["applied_ch2"].v == pytest.approx(_force(window.result.qraw.time))
    window.overlay_chk.setChecked(False)
    assert set(plot_traces(window)) == {"fcal_ch2"}


def test_log_scale_shows_magnitudes_and_survives_zeros_and_negatives(window):
    tick(window, "df_ch2", "fcal_ch2", "fratio_ch2", "f2")
    assert not any(p["log"] for p in window.plot._panels)
    window.log_checks["fcal"].setChecked(True)
    assert window.viz["log_groups"] == {"fcal"}
    panels = {tr.group: p for p in window.plot._panels for tr, _ in p["curves"] if tr.group in ("fcal", "fratio")}
    assert panels["fcal"]["log"] and not panels["fratio"]["log"]
    plot = panels["fcal"]["plot"]
    assert plot.getAxis("left").logMode and plot.getAxis("left").labelText == "|Calculated force [N]| (log)"
    for tr, item in panels["fcal"]["curves"]:
        assert np.all((tr.v > 0) | np.isnan(tr.v))                              # |y|, zeros left out, nothing negative
        x, y = item.getData()
        assert y is not None and np.all(np.isfinite(y[np.isfinite(y)]))
    assert any(np.isnan(tr.v).any() for tr, _ in panels["fcal"]["curves"])      # the zeros of dF[0] / the force before the ramp
    window.log_checks["fcal"].setChecked(False)
    assert not any(p["log"] for p in window.plot._panels)
    window.log_checks["fratio"].setChecked(True)                                # the ratio is positive: a direct log view
    ratio_panel = next(p for p in window.plot._panels if p["log"])
    assert ratio_panel["plot"].getAxis("left").logMode
    window.log_checks["fratio"].setChecked(False)


def test_ratio_panel_has_a_marker_at_one_in_linear_and_log_mode(window):
    tick(window, "fratio_ch2")

    def lines():
        plot = window.plot._panels[0]["plot"]
        return [i for i in plot.items if isinstance(i, pg.InfiniteLine)]

    assert [l.value() for l in lines()] == [1.0]
    window.log_checks["fratio"].setChecked(True)
    assert [l.value() for l in lines()] == [0.0]                                # log10(1) on the log axis


def test_log_mode_works_with_every_layout_the_reference_and_markers(window):
    tick(window, "df_ch2", "fcal_ch2", "adcout_ch2", "ain")
    window.log_checks["fcal"].setChecked(True)
    window.log_checks["adc"].setChecked(True)
    window.hold_reference()
    assert {"fcal_ch2", "df_ch2", "adcout_ch2"} <= set(window.reference["traces"])
    for layout in ("node", "overlay", "group"):
        window.layout_combo.setCurrentIndex(window.layout_combo.findData(layout))
        assert window.plot.has_data()
        assert all(p["log"] for p in window.plot._panels)                       # every panel only holds log groups
    window.log_checks["adc"].setChecked(False)
    window.layout_combo.setCurrentIndex(window.layout_combo.findData("overlay"))
    assert [p["log"] for p in window.plot._panels] == [False]                  # a single overlay panel mixing linear and log groups
    window.layout_combo.setCurrentIndex(window.layout_combo.findData("node"))
    assert sorted(p["log"] for p in window.plot._panels) == [False, False, True, True]     # ain, ADC-Out | dF, F
    window.plot._refresh_curves()                                               # decimation of the magnitudes in log mode


def test_log_setting_round_trips_and_images_and_csv_contain_the_new_traces(window, tmp_path, monkeypatch):
    tick(window, "df_ch2", "fcal_ch2", "fratio_ch2")
    window.log_checks["fcal"].setChecked(True)
    window.overlay_chk.setChecked(True)
    target = tmp_path / "settings.json"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "")))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(target), "")))
    window.save_settings()
    saved = json.loads(target.read_text(encoding="utf-8"))["visualization"]
    assert saved["log_groups"] == ["fcal"] and saved["overlay_applied"] is True
    window.log_checks["fcal"].setChecked(False)
    window.overlay_chk.setChecked(False)
    window.load_settings()
    assert window.viz["log_groups"] == {"fcal"} and window.log_checks["fcal"].isChecked() and window.overlay_chk.isChecked()
    assert any(p["log"] for p in window.plot._panels)

    for name in ("img.png", "img.svg"):
        window.plot.save_image(str(tmp_path / name), width=900)
        assert (tmp_path / name).stat().st_size > 2000

    data = tmp_path / "data.csv"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(data), "")))
    window.export_csv()
    head = data.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert {"dF ch2 [N]", "F ch2 [N]", "Fcal/Fin ch2 [ratio]"} <= set(head)
    rows = list(csv.reader(data.open(encoding="utf-8")))
    col = rows[0].index("F ch2 [N]")
    values = [float(r[col]) for r in rows[1:] if r[col] != "nan"]
    assert values and values[-1] == pytest.approx(window._derived_series["fcal_ch2"]["v"][-1])
