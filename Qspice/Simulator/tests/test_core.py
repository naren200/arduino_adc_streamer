"""Core (non-GUI) tests: parameter parsing, netlist tools, decimation, model discovery and a real QSPICE run."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from qspice_sim import config_utils, model_loader, netlist_tools, runner
from qspice_sim.decimate import envelope

QSPICE_DIR = Path(__file__).resolve().parents[2]

NETLIST = """* test
.subckt CELL a b
R1 a x 1k
C1 x b 1n
.ends CELL
V1 in 0 PULSE(0 1 0 1n 1n 1m 2m)
R1 in out 1k
C1 out 0 1u
X1 out mid CELL
Bq 0 mid I=1u
.tran 0 3m 0 10u
.save V(out) V(mid)
+ V(in)
.end
"""


def test_parse_number_suffixes():
    assert config_utils.parse_number("1e10") == 1e10
    assert config_utils.parse_number("470k") == 470e3
    assert config_utils.parse_number("1.5n") == pytest.approx(1.5e-9)
    assert config_utils.parse_number("2meg") == 2e6
    assert config_utils.parse_number("3m") == pytest.approx(3e-3)
    assert config_utils.parse_number("3M") == 3e6
    with pytest.raises(ValueError):
        config_utils.parse_number("abc")
    with pytest.raises(ValueError):
        config_utils.parse_number("nan")


def test_paths_and_overrides():
    cfg = {"a": {"b": 1}}
    assert config_utils.get_path(cfg, "a.b") == 1
    assert config_utils.get_path(cfg, "a.c", None) is None
    with pytest.raises(KeyError):
        config_utils.get_path(cfg, "a.c")
    out = config_utils.apply_overrides(cfg, {"a.b": 2, "x.y": 3})
    assert out == {"a": {"b": 2}, "x": {"y": 3}} and cfg == {"a": {"b": 1}}


def test_netlist_tools():
    assert netlist_tools.discover_nodes(NETLIST) == ["in", "mid", "out"]  # subcircuit internals and ground excluded
    assert netlist_tools.parse_tran(NETLIST) == pytest.approx((3e-3, 10e-6))
    stripped = netlist_tools.strip_save(NETLIST)
    assert ".save" not in stripped and "V(in)" not in stripped
    saved = netlist_tools.set_save(NETLIST, ["out", "in"])
    lines = saved.splitlines()
    assert lines.count(".save V(out) V(in)") == 1 and lines.index(".save V(out) V(in)") < lines.index(".end")


def test_envelope_keeps_spikes_and_small_windows_are_raw():
    t = np.linspace(0, 1, 1_000_000)
    v = np.zeros_like(t)
    v[123_456] = 5.0
    x, y = envelope(t, v, 0.0, 1.0, n_buckets=500)
    assert len(x) <= 1100 and y.max() == 5.0
    x, y = envelope(t, v, 0.1234, 0.1235, n_buckets=500)
    assert len(x) < 1000 and np.all(np.diff(x) >= 0)


def test_discovers_testboard_model_without_problems():
    models = model_loader.discover([QSPICE_DIR])
    ids = [m.id for m in models]
    assert "TestBoard_ADC124" in ids
    model = models[ids.index("TestBoard_ADC124")]
    assert model.problems == []
    prepared = runner.prepare(model, model.default_config())
    assert "ain" in prepared.nodes_in_netlist and prepared.max_step == pytest.approx(5e-9)
    assert any("Scan cycle" in label for label, _ in model.summary(model.default_config()))


def test_adapter_rejects_impossible_sampling_rate():
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    cfg = model.default_config()
    cfg["timing"]["sampling_rate_hz"] = 1e6
    with pytest.raises(ValueError, match="too high"):
        model.build(cfg)


def test_sampling_rate_sets_cycle_length():
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    cfg = model.default_config()
    cfg["timing"]["sampling_rate_hz"] = 5000.0
    cfg["timing"]["num_cycles"] = 2
    samples = model.build(cfg)["samples"]
    n = len(cfg["timing"]["scan_order"])
    assert samples[n]["t"] - samples[0]["t"] == pytest.approx(1 / 5000.0, rel=1e-6)


def _qspice_or_skip() -> Path:
    try:
        return runner.find_qspice()
    except runner.SimulationError:
        pytest.skip("QSPICE is not installed")


def test_real_qspice_run_returns_selected_nodes(tmp_path):
    qspice = _qspice_or_skip()
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    cfg = model.default_config()
    cfg["timing"]["num_cycles"] = 1
    prepared = runner.prepare(model, cfg)
    raw = runner.run_netlist(prepared.netlist, ["ain", "s2"], tmp_path, qspice)
    assert sorted(raw.nodes()) == ["ain", "s2"] and raw.n_points > 1000
    assert raw.time[-1] == pytest.approx(prepared.t_stop, rel=1e-3)
    assert 1.5 < raw.column("ain")[-1] < 1.8  # buffer sits near VMID with no force
    raw.close()


def test_real_qspice_leakage_changes_the_baseline(tmp_path):
    qspice = _qspice_or_skip()
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    finals = []
    for scale in (0.0, 10.0):
        cfg = model.default_config()
        cfg["forces"] = []
        cfg["timing"]["num_cycles"] = 3
        cfg["mux"]["leak_scale"] = scale
        prepared = runner.prepare(model, cfg)
        raw = runner.run_netlist(prepared.netlist, ["ain"], tmp_path / f"s{scale}", qspice)
        t, v = raw.time, raw.column("ain")
        finals.append(float(np.interp(prepared.samples[-1]["t"], t, v)))
        raw.close()
    assert finals[0] - finals[1] > 0.02  # 10x leakage pulls the sampled baseline down by tens of mV


def test_cancel_stops_a_running_simulation(tmp_path):
    import threading
    import time

    qspice = _qspice_or_skip()
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    cfg = model.default_config()
    cfg["timing"]["run_duration_s"] = 0.01
    prepared = runner.prepare(model, cfg)
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    t0 = time.time()
    with pytest.raises(runner.Cancelled):
        runner.run_netlist(prepared.netlist, ["ain"], tmp_path, qspice, cancel=cancel)
    assert time.time() - t0 < 15


def test_periodic_mode_run_and_control_nodes(tmp_path):
    qspice = _qspice_or_skip()
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    cfg = model.default_config()
    cfg["forces"] = []
    cfg["timing"]["run_duration_s"] = 0.0008
    cfg["simulation"]["periodic_min_cycles"] = 1
    prepared = runner.prepare(model, cfg)
    assert "PULSE(0 1" in prepared.netlist and "c7_0" in prepared.nodes_in_netlist
    raw = runner.run_netlist(prepared.netlist, ["ain", "c2", "c7"], tmp_path, qspice)
    c2, c7 = raw.column("c2"), raw.column("c7")
    assert c2.max() == pytest.approx(1.0, abs=0.01) and c7.max() == pytest.approx(1.0, abs=0.01)
    both_on = np.sum((c2 > 0.5) & (c7 > 0.5))
    assert both_on == 0  # break-before-make: S2 and the VMID channel are never on together
    raw.close()


@pytest.mark.parametrize("vmid_visit", [True, False])
def test_vmid_channel_is_off_during_the_first_sensor_visit(vmid_visit):
    """Regression: the initial VMID park must end at the first address command (it used to ramp down slowly)."""
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    bm = model.adapter.bm
    cfg = model.default_config()
    cfg["timing"]["vmid_visit_after_each"] = vmid_visit
    slots, _, _ = bm.build_schedule(cfg)
    c7 = bm.control_pwl(cfg, slots)[7]
    t = np.array([p[0] for p in c7])
    v = np.array([p[1] for p in c7])
    assert np.all(np.diff(t) >= 0)
    first_start, first_end = slots[0]["t0"], slots[0]["t1"]
    probes = np.linspace(first_start + 1e-6, first_end - 1e-6, 20)
    assert np.interp(probes, t, v).max() == 0.0


def test_without_vmid_visit_a_discharged_sensor_charges_slowly_through_r13(tmp_path):
    qspice = _qspice_or_skip()
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    cfg = model.default_config()
    cfg["forces"] = []
    cfg["circuit"]["initial_sensor_node_v"] = 0.0
    cfg["timing"]["vmid_visit_after_each"] = False
    cfg["timing"]["num_cycles"] = 1
    prepared = runner.prepare(model, cfg)
    raw = runner.run_netlist(prepared.netlist, ["s2", "c7"], tmp_path, qspice)
    t, s2, c7 = raw.time, raw.column("s2"), raw.column("c7")
    visit_end = 20e-6 + 3 * 5e-6                    # sensor 2 is connected from 30 us to 35 us (5 us each, no park between sensors)
    assert c7[(t > 21e-6) & (t < visit_end)].max() < 1e-3          # VMID channel stays off during the whole scan
    assert np.interp(visit_end, t, s2) < 1.0                       # tau = R13*(Cp+C_D) ~ 38 us: far from VMID after 5 us
    assert np.interp(visit_end, t, s2) > 0.1
    raw.close()


def test_stale_locked_result_file_is_never_used(tmp_path):
    """Regression: a result file still held open by another process made QSPICE fail and the old data was shown as new."""
    qspice = _qspice_or_skip()
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    cfg = model.default_config()
    cfg["timing"]["num_cycles"] = 1
    prepared = runner.prepare(model, cfg)
    first = runner.run_netlist(prepared.netlist, ["ain"], tmp_path, qspice)
    n_first = first.n_points
    cfg["timing"]["num_cycles"] = 2
    prepared2 = runner.prepare(model, cfg)
    # `first` still maps its file, exactly like a window that keeps showing the previous result
    second = runner.run_netlist(prepared2.netlist, ["ain"], tmp_path, qspice)
    assert second.n_points > n_first * 1.5
    assert second.path != first.path
    first.close()
    second.close()


def test_without_vmid_visit_sensors_stay_connected_only_busy_time_and_park_once_per_sweep():
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    bm = model.adapter.bm
    cfg = model.default_config()
    cfg["timing"]["vmid_visit_after_each"] = False
    cfg["timing"]["num_cycles"] = 2
    slots, _, _ = bm.build_schedule(cfg)
    t = cfg["timing"]
    busy = bm.adc_timing(t)["busy"]
    sensors = [s for s in slots if s["kind"] == "sensor"]
    vmid = [s for s in slots if s["kind"] == "vmid"]
    assert all(s["t1"] - s["t0"] == pytest.approx(busy) for s in sensors)       # not extended by the park time
    assert len(vmid) == 2                                                          # one park per sweep
    assert vmid[0]["t1"] - vmid[0]["t0"] == pytest.approx(t["t_vmid_park_us"] * 1e-6)
    assert vmid[0]["t0"] == pytest.approx(sensors[6]["t1"])                        # right after the last address
    cycle = slots[8]["t0"] - slots[0]["t0"]                                     # 7 sensors + 1 park per cycle
    assert cycle == pytest.approx(7 * busy + t["t_vmid_park_us"] * 1e-6)
    cfg["timing"]["sampling_rate_hz"] = 10000.0
    slots, _, _ = bm.build_schedule(cfg)
    assert slots[8]["t0"] - slots[0]["t0"] == pytest.approx(1e-4, rel=1e-9)


def test_mux_group_timing_is_derived_from_mux_count_and_spi_clock():
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    bm = model.adapter.bm
    t = model.default_config()["timing"]
    d = bm.adc_timing(t)                                   # defaults: 4 muxes, 16 MHz, 16-clock word, 3-clock acquisition
    assert d["word"] == pytest.approx(1e-6) and d["acq"] == pytest.approx(187.5e-9)
    assert d["wait_others"] == pytest.approx(3e-6) and d["busy"] == pytest.approx(5e-6)
    t["n_mux"] = 1
    assert bm.adc_timing(t)["busy"] == pytest.approx(2e-6) and bm.adc_timing(t)["wait_others"] == 0.0
    t["n_mux"], t["adc_spi_clk_hz"] = 4, 8e6                # half the clock: 2 us words
    d = bm.adc_timing(t)
    assert d["word"] == pytest.approx(2e-6) and d["busy"] == pytest.approx(9e-6) and d["acq"] == pytest.approx(375e-9)
    cfg = model.default_config()
    cfg["timing"]["n_mux"], cfg["timing"]["adc_spi_clk_hz"] = 4, 8e6
    cfg["timing"]["num_cycles"] = 1
    slots, _, _ = bm.build_schedule(cfg)
    assert slots[0]["t1"] - slots[0]["t0"] == pytest.approx(9e-6)
    assert slots[0]["t_sample"] - slots[0]["t0"] == pytest.approx(1e-6 + 375e-9)   # hold = settle + acquisition clocks
    cfg["timing"]["n_mux"] = 0
    with pytest.raises(ValueError, match="n_mux"):
        bm.build_schedule(cfg)


def test_adc124_repeats_extend_the_connected_time_and_the_retained_hold():
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    bm = model.adapter.bm
    cfg = model.default_config()
    assert cfg["timing"]["repeat"] == 1                                       # default
    t = cfg["timing"]
    t["repeat"] = 3
    d = bm.adc_timing(t)                                                      # 4 muxes x 3 words in a row, 16 MHz
    assert d["busy"] == pytest.approx(1e-6 + 12e-6)                           # settle + n_mux * repeat * word
    assert d["hold_offset"] == pytest.approx(1e-6 + 2e-6 + 187.5e-9)          # settle + (repeat - 1) words + acquisition
    assert d["wait_others"] == pytest.approx(9e-6)                            # (n_mux - 1) * repeat words
    t["num_cycles"] = 1
    slots, _, _ = bm.build_schedule(cfg)
    assert slots[0]["t1"] - slots[0]["t0"] == pytest.approx(13e-6)
    assert slots[0]["t_sample"] - slots[0]["t0"] == pytest.approx(3.1875e-6)
    t["repeat"] = 0
    with pytest.raises(ValueError, match="repeat"):
        bm.adc_timing(t)
