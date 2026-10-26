"""B2: ADC-Out, the quantised ADC result derived from the sample node (pure functions, manifest checks and the GUI)."""
from __future__ import annotations

import csv
import os
from pathlib import Path

import numpy as np
import pytest

from qspice_sim import model_loader, runner
from qspice_sim.adc_model import AdcParams, build_sample_table, quantize, stair, stair_at

QSPICE_DIR = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ conversion
@pytest.mark.parametrize("bits, expected_code", [(8, 102), (10, 410), (12, 1638), (16, 26214)])
def test_known_input_gives_the_expected_code_and_voltage(bits, expected_code):
    adc = AdcParams(bits=bits, vref=2.5, vmid=1.25)
    code, v_out, clipped = quantize(np.array([1.0]), adc)
    assert code[0] == expected_code and not clipped[0]
    lsb = 2.5 / 2 ** bits
    assert adc.lsb == pytest.approx(lsb) and v_out[0] == pytest.approx(expected_code * lsb)
    assert abs(v_out[0] - 1.0) <= lsb / 2 + 1e-15              # never further than half an LSB from the input


def test_rounding_is_to_nearest_with_halves_going_up():
    adc = AdcParams(bits=12, vref=2.5, vmid=1.25)
    code, _, _ = quantize(np.array([100.49, 100.5, 100.51, 101.5, 0.49, 0.5]) * adc.lsb, adc)
    assert list(code) == [100, 101, 101, 102, 0, 1]


def test_values_outside_the_range_are_clipped_and_counted():
    adc = AdcParams(bits=12, vref=2.5, vmid=1.25)
    code, v_out, clipped = quantize(np.array([-0.2, 0.0, 1.0, 2.5, 2.6]), adc)
    assert list(code) == [0, 0, 1638, 4095, 4095] and list(clipped) == [True, False, False, False, True]
    assert v_out[0] == 0.0 and v_out[-1] == pytest.approx(4095 * adc.lsb)


def test_vmid_zero_gives_a_bipolar_range_and_keeps_negative_values():
    uni = AdcParams(bits=12, vref=2.5, vmid=1.25)
    bip = AdcParams(bits=12, vref=2.5, vmid=0.0)
    assert (uni.v_low, uni.v_high, uni.lsb) == (0.0, 2.5, pytest.approx(2.5 / 4096))
    assert (bip.v_low, bip.v_high, bip.lsb) == (-2.5, 2.5, pytest.approx(5.0 / 4096))
    v = np.array([-1.0, 0.0, 1.0])
    assert quantize(v, uni)[2].tolist() == [True, False, False] and quantize(v, uni)[1][0] == 0.0     # clipped in unipolar mode
    code, v_out, clipped = quantize(v, bip)
    assert not clipped.any() and v_out == pytest.approx(v, abs=bip.lsb / 2) and code[1] == 2048          # kept in bipolar mode
    assert "bipolar" in bip.describe() and "unipolar" in uni.describe()


def test_adc_parameters_are_validated():
    with pytest.raises(ValueError, match="bits"):
        AdcParams(bits=1, vref=2.5, vmid=1.25)
    with pytest.raises(ValueError, match="reference"):
        AdcParams(bits=12, vref=0.0, vmid=1.25)


def test_sample_table_keeps_channels_apart_and_orders_by_time():
    adc = AdcParams(bits=12, vref=2.5, vmid=1.25)
    samples = [{"t": 3e-6, "label": "b", "address": 4}, {"t": 1e-6, "label": "a", "address": 2}, {"t": 5e-6, "label": "c", "address": 2}]
    table = build_sample_table(samples, np.array([1.0, 0.5, 2.0]), adc)
    assert table.t.tolist() == [1e-6, 3e-6, 5e-6] and table.address.tolist() == [2, 4, 2]
    assert table.channel_mask(2).tolist() == [True, False, True] and table.label == ["a", "b", "c"]
    assert table.v_in.tolist() == [0.5, 1.0, 2.0] and table.noise_v.tolist() == [0.0, 0.0, 0.0]


def test_stair_holds_each_value_until_the_next_conversion():
    x, y = stair(np.array([1.0, 2.0, 4.0]), np.array([10.0, 20.0, 30.0]), t_end=6.0)
    assert x.tolist() == [1.0, 2.0, 2.0, 4.0, 4.0, 6.0] and y.tolist() == [10.0, 10.0, 20.0, 20.0, 30.0, 30.0]
    x, y = stair(np.array([1.0]), np.array([5.0]), t_end=3.0)
    assert x.tolist() == [1.0, 3.0] and y.tolist() == [5.0, 5.0]
    assert stair(np.zeros(0), np.zeros(0), 1.0)[0].size == 0
    at = stair_at(np.array([0.5, 1.0, 1.5, 2.0, 5.0]), np.array([1.0, 2.0, 4.0]), np.array([10.0, 20.0, 30.0]))
    assert np.isnan(at[0]) and at[1:].tolist() == [10.0, 10.0, 20.0, 30.0]


# ------------------------------------------------------------------ manifest
def test_manifest_validation_checks_the_adc_out_block():
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_7953")
    manifest = {"plot_groups": [{"id": "adc", "title": "ADC"}], "nodes": [{"name": "sh", "group": "adc"}],
                "adc_out": {"node": "sh", "bits_path": "adc.bits", "vref_path": "circuit.vref_v", "vmid_path": "circuit.vmid_v",
                            "channels": 3}}
    assert model_loader.validate_manifest(manifest, model.defaults) == []
    bad = dict(manifest, adc_out=dict(manifest["adc_out"], bits_path="adc.missing", node="nope", group="nogroup", channels=0))
    problems = " | ".join(model_loader.validate_manifest(bad, model.defaults))
    assert "adc.missing" in problems and "nope" in problems and "nogroup" in problems and "channels" in problems
    defaults = dict(model.defaults, adc=dict(model.defaults["adc"], bits=1))
    assert any("at least 2" in p for p in model_loader.validate_manifest(manifest, defaults))
    clash = dict(manifest, nodes=manifest["nodes"] + [{"name": "adcout_ch0", "group": "adc"}])
    assert any("same name" in p for p in model_loader.validate_manifest(clash, model.defaults))
    assert model.problems == [] and len(model.derived_nodes()) == 15


def test_every_model_gives_one_adc_out_node_per_sensor_channel():
    for mid, n in (("TestBoard_7953", 15), ("TestBoard_ADC124", 7)):
        model = model_loader.load_model(QSPICE_DIR / mid)
        assert [d["name"] for d in model.derived_nodes()] == [f"adcout_ch{k}" for k in range(n)]
        assert all(d["sources"] == [model.adc_out["node"]] for d in model.derived_nodes())
        built = model.build(model.default_config())
        assert {s["address"] for s in built["samples"]} == set(range(n))
        assert all(isinstance(s["address"], int) for s in built["samples"])


# ------------------------------------------------------------------ GUI
pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6 import QtWidgets  # noqa: E402

from derived_helpers import install_result, plot_traces, tick  # noqa: E402
from qspice_sim.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture()
def window(app):
    win = MainWindow([model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124"), model_loader.load_model(QSPICE_DIR / "TestBoard_7953")])
    win.settings.clear()
    win.resize(1500, 900)
    win.show()
    yield win
    win.close()


def _tree_items(window) -> dict[str, QtWidgets.QTreeWidgetItem]:
    out = {}
    for i in range(window.node_tree.topLevelItemCount()):
        parent = window.node_tree.topLevelItem(i)
        for j in range(parent.childCount()):
            out[parent.child(j).text(1)] = parent.child(j)
    return out


def _ain(t):
    return 0.8 + 0.5 * np.sin(2 * np.pi * t / t[-1] * 3)


def test_adc_out_nodes_are_in_the_tree_per_channel_and_the_defaults_follow_the_forces(window):
    items = _tree_items(window)
    assert [items[f"adcout_ch{k}"].text(0) for k in range(7)] == [f"ADC-Out ch{k}" for k in range(7)]
    assert "0 .. 3.3 V" in items["adcout_ch0"].toolTip(0) and "LSB" in items["adcout_ch0"].toolTip(0)
    # default: the first scanned channel plus every channel that has an applied force (pulses on addresses 2 and 4)
    assert {n for n in window.viz["nodes"] if n.startswith("adcout")} == {"adcout_ch0", "adcout_ch2", "adcout_ch4"}
    window.cfg["forces"] = []
    window._nodes_defaults()
    assert {n for n in window.viz["nodes"] if n.startswith("adcout")} == {"adcout_ch0"}
    window.load_model(window.models[1])
    assert len([n for n in _tree_items(window) if n.startswith("adcout_ch")]) == 15


def test_ticking_a_derived_node_adds_its_source_to_the_save_list(window):
    prepared = runner.prepare(window.model, window.cfg)
    window.viz["nodes"] = {"muxd"}
    assert window._nodes_to_save(prepared) == ["muxd"]
    window.viz["nodes"] = {"muxd", "adcout_ch2"}
    assert window._nodes_to_save(prepared) == ["ain", "muxd"]      # the sample node is pulled in; adcout_ch2 is no QSPICE node
    window.viz["nodes"] = {"adcout_ch2", "adcout_ch4"}
    assert window._nodes_to_save(prepared) == ["ain"]


def test_adc_out_trace_per_channel_with_stairs_and_replots_without_a_run(window):
    install_result(window, {"ain": _ain})
    tick(window, "ain", "adcout_ch2")
    traces = plot_traces(window)
    assert set(traces) == {"ain", "adcout_ch2"}
    assert traces["adcout_ch2"].kind == "stair" and traces["adcout_ch2"].label == "ADC-Out ch2"
    table = window.sample_table
    mask = table.channel_mask(2)
    assert mask.sum() == 4                                          # four scan cycles in the default configuration
    v = traces["adcout_ch2"].v                                      # one stair value per conversion of channel 2
    assert v[0::2].tolist() == pytest.approx(table.v_out[mask].tolist())
    assert traces["adcout_ch2"].t[0::2].tolist() == pytest.approx(table.t[mask].tolist())
    adc = window.model.adc_params(window.result.cfg, window.cfg)
    ideal = np.interp(table.t[mask], window.result.qraw.time, _ain(window.result.qraw.time))
    assert np.max(np.abs(table.v_out[mask] - ideal)) <= adc.lsb / 2 + 1e-12
    # changing the resolution re-derives the trace from the same run (no simulation)
    fine = traces["adcout_ch2"].v.copy()
    window.cfg["adc"]["bits"] = 4
    window._on_param_changed("adc.bits")
    assert window.worker is None and window.result is not None
    coarse = plot_traces(window)["adcout_ch2"].v
    assert not np.array_equal(fine, coarse) and len(np.unique(coarse)) <= 16
    tick(window, "ain")                                             # unticking removes it
    assert set(plot_traces(window)) == {"ain"}


def test_channel_without_a_conversion_is_reported(window):
    window.cfg["timing"]["scan_order"] = [0, 1]
    install_result(window, {"ain": _ain})
    tick(window, "adcout_ch0", "adcout_ch5")
    assert set(plot_traces(window)) == {"adcout_ch0"}
    assert "no conversion in the run: ADC-Out ch5" in window.status_label.text()


def test_missing_sample_node_is_reported_like_any_unsaved_node(window):
    install_result(window, {"muxd": _ain})
    tick(window, "muxd", "adcout_ch2")
    assert set(plot_traces(window)) == {"muxd"}
    assert "press Run to include them" in window.status_label.text() and "ADC-Out ch2" in window.status_label.text()


def test_clipped_samples_are_counted_in_the_status_bar(window):
    install_result(window, {"ain": lambda t: 5.0 + 0 * t})          # far above VDD = 3.3 V
    tick(window, "adcout_ch2")
    assert "4 sample(s) clipped" in window.status_label.text()
    assert window.sample_table.code[window.sample_table.channel_mask(2)].tolist() == [4095] * 4


def test_export_samples_and_export_data_contain_adc_out(window, tmp_path, monkeypatch):
    install_result(window, {"ain": _ain})
    tick(window, "ain", "adcout_ch2")
    path = tmp_path / "samples.csv"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))
    window.export_samples()
    rows = list(csv.reader(path.open(encoding="utf-8")))
    assert rows[0] == ["t", "label", "address", "v_in", "noise_mv", "code", "v_out", "clipped"]
    assert len(rows) == 1 + 28                                      # 7 sensors x 4 cycles
    row = next(r for r in rows[1:] if r[2] == "2")
    adc = window.sample_table.adc
    assert float(row[6]) == pytest.approx(adc.v_low + int(row[5]) * adc.lsb, rel=1e-6)
    assert abs(float(row[6]) - float(row[3])) <= adc.lsb
    data = tmp_path / "data.csv"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(data), "")))
    window.export_csv()
    head = data.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert head[:2] == ["time_s", "V(ain)"] and "ADC-Out ch2 [V]" in head and "ADC-Out ch2 code" in head


def test_bipolar_mode_keeps_negative_values_and_unsupported_models_warn(window):
    window.cfg["circuit"]["vmid_v"] = 0.0
    install_result(window, {"ain": lambda t: -1.0 + 0 * t})
    tick(window, "adcout_ch2")
    assert "clipped" not in window.status_label.text()
    adc = window.sample_table.adc
    assert adc.bipolar and window.sample_table.v_out[window.sample_table.channel_mask(2)][0] == pytest.approx(-1.0, abs=adc.lsb)
    assert window._range_warnings() == []                           # the models support the bipolar supply
    window.model.manifest["adc_out"] = dict(window.model.adc_out, bipolar_rails=False)
    assert "no bipolar analog supply" in window._range_warnings()[0]
