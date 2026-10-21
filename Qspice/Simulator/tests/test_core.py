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
