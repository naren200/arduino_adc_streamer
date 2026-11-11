"""Basic_Model: one ideal PZT -> ideal unity buffer (bias resistor to ground or VMID) -> ideal ADC.

Also covers the optional adapter hook ``adc_range`` (a range that is neither 0 .. Vref nor -Vref .. +Vref) that the model needs.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from qspice_sim import model_loader, runner
from qspice_sim.adc_model import AdcParams, build_sample_table, quantize
from qspice_sim.force_calc import FORCE_DEFAULTS, compute_all

QSPICE_DIR = Path(__file__).resolve().parents[2]
SENS = 600e-12 / 60.7e-12                 # d33 / Cp [V/N], 9.88 V/N at the default 0.7 x 0.7 x 0.25 mm element


@pytest.fixture(scope="module")
def model():
    return model_loader.load_model(QSPICE_DIR / "Basic_Model")


@pytest.fixture()
def cfg(model):
    return model.default_config()


def _qspice_or_skip() -> Path:
    try:
        return runner.find_qspice()
    except runner.SimulationError:
        pytest.skip("QSPICE is not installed")


# ------------------------------------------------------------------ the model and its defaults
def test_model_is_discovered_without_problems():
    ids = {m.id: m for m in model_loader.discover([QSPICE_DIR])}
    assert {"Basic_Model", "TestBoard_7953", "TestBoard_ADC124"} <= set(ids)
    assert ids["Basic_Model"].problems == []


def test_defaults_are_ground_bias_3v3_rail_12_bit(cfg):
    assert cfg["circuit"]["bias_to"] == "ground" and cfg["circuit"]["vdd_v"] == 3.3 and cfg["circuit"]["vmid_v"] == 1.65
    assert cfg["adc"]["bits"] == 12 and cfg["adc"]["sampling_rate_hz"] > 0


def test_ground_bias_swings_between_minus_and_plus_half_the_rail(model, cfg):
    adc = model.adc_params(cfg)
    assert (adc.v_low, adc.v_high, adc.bipolar) == (pytest.approx(-1.65), pytest.approx(1.65), True)
    assert adc.lsb == pytest.approx(3.3 / 4096) and model.rest_voltage(cfg) == 0.0


def test_vmid_bias_swings_between_zero_and_the_rail(model, cfg):
    cfg["circuit"]["bias_to"] = "vmid"
    adc = model.adc_params(cfg)
    assert (adc.v_low, adc.v_high, adc.bipolar) == (0.0, pytest.approx(3.3), False)
    assert adc.lsb == pytest.approx(3.3 / 4096) and model.rest_voltage(cfg) == pytest.approx(1.65)


def test_resolution_and_rail_follow_the_settings(model, cfg):
    cfg["adc"]["bits"] = 16
    cfg["circuit"]["vdd_v"] = 5.0
    adc = model.adc_params(cfg)
    assert (adc.v_low, adc.v_high) == (-2.5, 2.5) and adc.lsb == pytest.approx(5.0 / 65536)
    live = model.default_config()
    live["adc"]["bits"] = 8                                   # the live resolution replots, the range comes from the run
    assert model.adc_params(cfg, live).bits == 8 and model.adc_params(cfg, live).v_high == 2.5


def test_invalid_parameters_are_rejected_with_a_readable_message(model, cfg):
    for path, value, text in (("circuit.bias_to", "supply", "bias_to"), ("circuit.vdd_v", 0.0, "vdd_v"), ("circuit.r_bias_ohm", 0.0, "r_bias"),
                              ("adc.bits", 1, "bits"), ("adc.sampling_rate_hz", 0.0, "sampling_rate"), ("adc.sampling_rate_hz", 1e9, "50 MHz"),
                              ("simulation.run_duration_ms", 0.0, "run_duration"), ("simulation.max_step_us", -1.0, "max_step"),
                              ("simulation.reltol", 0.0, "reltol"), ("sensor_defaults.cp_pF", -3.0, "positive")):
        bad = model.default_config()
        section, key = path.split(".")
        bad[section][key] = value
        with pytest.raises(ValueError, match=text):
            model.build(bad)


# ------------------------------------------------------------------ the optional adc_range hook of the simulator
def test_adc_params_with_an_explicit_lower_limit():
    adc = AdcParams(bits=12, vref=1.65, vmid=0.0, low=-1.65)
    assert (adc.v_low, adc.v_high, adc.lsb) == (-1.65, 1.65, pytest.approx(3.3 / 4096)) and adc.bipolar
    code, v_out, clipped = quantize(np.array([-2.0, -1.65, 0.0, 1.65, 2.0]), adc)
    assert code.tolist() == [0, 0, 2048, 4095, 4095] and clipped.tolist() == [True, False, False, False, True]
    assert v_out[2] == pytest.approx(0.0, abs=1e-12) and "unipolar" in AdcParams(bits=12, vref=3.3, vmid=1.65, low=0.0).describe()
    with pytest.raises(ValueError, match="above the lower"):
        AdcParams(bits=12, vref=1.0, vmid=0.0, low=1.0)


def test_models_without_the_hook_keep_their_range_rule():
    adc124 = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    assert not adc124.has_adc_range and not model_loader.load_model(QSPICE_DIR / "TestBoard_7953").has_adc_range
    cfg = adc124.default_config()
    assert adc124.adc_params(cfg).low is None and adc124.rest_voltage(cfg) == 1.65
    assert (adc124.adc_params(cfg).v_low, adc124.adc_params(cfg).v_high) == (0.0, 3.3)
    cfg["circuit"]["vmid_v"] = 0.0
    assert (adc124.adc_params(cfg).v_low, adc124.adc_params(cfg).v_high) == (-3.3, 3.3) and adc124.rest_voltage(cfg) == 0.0


# ------------------------------------------------------------------ the netlist: ideal, no parasitics, no mux
def test_netlist_is_ideal_and_has_only_the_pzt_capacitance(model, cfg):
    text = model.build(cfg)["netlist"]
    cap_lines = [ln for ln in text.splitlines() if ln.startswith(("C", "c"))]
    assert [ln.split()[0] for ln in cap_lines] == ["Cp0"]                          # the PZT is the only capacitor
    assert "Ebuf ain 0 p0 0 1" in text and "Rbias p0 0 " in text                   # ideal buffer, bias to ground
    for absent in ("Xch", "TMUX", "OPA365", "Ri0", "Rp0", "Cpar", "Vvmid", "R21", "C29"):
        assert absent not in text


def test_bias_to_vmid_connects_the_resistor_to_a_vmid_source(model, cfg):
    cfg["circuit"]["bias_to"] = "vmid"
    text = model.build(cfg)["netlist"]
    assert "Vvmid vmid 0 1.65" in text and "Rbias p0 vmid " in text and ".ic V(p0)=1.65" in text
    cfg["circuit"]["initial_sensor_node_v"] = 1.0
    assert ".ic V(p0)=1.0" in model.build(cfg)["netlist"]


def test_sensor_capacitance_and_override(model, cfg):
    sp = model.adapter.bm.sensor_params(cfg)
    assert sp["cp"] == pytest.approx(60.7e-12, rel=2e-3) and sp["d33"] == pytest.approx(600e-12)
    cfg["sensor_defaults"].update(cp_pF=100.0, force_gain=0.5)
    sp = model.adapter.bm.sensor_params(cfg)
    assert sp["cp"] == pytest.approx(100e-12) and sp["d33"] == pytest.approx(300e-12) and sp["cp_source"] == "override"
    assert "Cp0 p0 0 1.000000000e-10" in model.build(cfg)["netlist"]
    assert model.field_hints(model.default_config())["sensor_defaults.cp_pF"] == "calculated: 60.7 pF"


def test_a_force_on_another_address_is_rejected(model, cfg):
    cfg["forces"][0]["address"] = 1
    with pytest.raises(ValueError, match="one sensor"):
        model.build(cfg)


# ------------------------------------------------------------------ the sampling rate
@pytest.mark.parametrize("rate, duration_ms, first_us, n", [(50e3, 1.0, 0.0, 51), (10e3, 1.0, 0.0, 11), (20e3, 5.0, 25.0, 100),
                                                            (1e3, 1.0, 0.0, 2), (100e3, 0.05, 0.0, 6)])
def test_sampling_rate_sets_the_conversion_instants(model, cfg, rate, duration_ms, first_us, n):
    cfg["adc"].update(sampling_rate_hz=rate, t_first_sample_us=first_us)
    cfg["simulation"]["run_duration_ms"] = duration_ms
    built = model.build(cfg)
    t = np.array([s["t"] for s in built["samples"]])
    assert len(t) == n and t[0] == pytest.approx(first_us * 1e-6)
    assert np.diff(t) == pytest.approx(1.0 / rate) and t[-1] <= built["t_stop"] * (1 + 1e-9)
    assert built["t_stop"] == pytest.approx(duration_ms * 1e-3)


def test_conversions_describe_one_connection_that_spans_the_run(model, cfg):
    built = model.build(cfg)
    assert all(s["address"] == 0 and s["t_on"] == 0.0 and s["t_off"] == built["t_stop"] and s["node_reset"] for s in built["samples"])


def test_the_sample_instants_are_solver_breakpoints_at_the_sampling_period(model, cfg):
    cfg["adc"]["sampling_rate_hz"] = 8e3
    text = model.build(cfg)["netlist"]
    line = next(ln for ln in text.splitlines() if ln.startswith("Vbp"))
    assert line.split()[-1] == "1.250000000e-04)" and "PULSE(0 1m 0.000000000e+00 1n 1n 1n" in line


def test_no_sample_in_the_run_is_an_error(model, cfg):
    cfg["adc"].update(t_first_sample_us=2000.0)
    with pytest.raises(ValueError, match="no ADC sample"):
        model.build(cfg)
    cfg["adc"].update(t_first_sample_us=0.0, sampling_rate_hz=50e6)
    cfg["simulation"]["run_duration_ms"] = 100.0
    with pytest.raises(ValueError, match="ADC samples in the run"):
        model.build(cfg)


def test_automatic_step_follows_the_rate_and_the_run_and_a_typed_step_wins(model, cfg):
    bm = model.adapter.bm
    assert bm.max_step(cfg) == pytest.approx(1e-6)                                  # min(20 us / 20, 1 ms / 1000)
    cfg["adc"]["sampling_rate_hz"] = 1e6
    assert bm.max_step(cfg) == pytest.approx(50e-9)
    cfg["simulation"]["run_duration_ms"] = 20000.0
    assert bm.max_step(cfg) == pytest.approx(10e-6)                                 # run / 2e6 caps the number of points
    cfg["adc"]["sampling_rate_hz"] = 0.05
    assert bm.max_step(cfg) == pytest.approx(20e-3)                                 # run / 1000: slow sampling needs no fine step
    cfg["simulation"]["max_step_us"] = 3.0
    assert bm.max_step(cfg) == pytest.approx(3e-6) and runner.prepare(model, model.default_config()).max_step == pytest.approx(1e-6)


# ------------------------------------------------------------------ warnings, summary, force data
def test_no_warning_for_the_default_pulse_and_a_clip_warning_for_a_large_force(model, cfg):
    assert model.build(cfg)["warnings"] == []
    cfg["forces"][0]["amp_N"] = 0.3
    (warn,) = model.build(cfg)["warnings"]
    assert "upper and lower limit" in warn and "-1.65 .. 1.65" in warn                # 2.96 V open circuit around 0 V
    cfg["circuit"]["bias_to"] = "vmid"
    (warn,) = model.build(cfg)["warnings"]
    assert "upper and lower limit" in warn and "0 .. 3.3" in warn
    cfg["forces"][0]["amp_N"] = 0.15                                                  # 1.48 V: inside +-1.65 V
    assert model.build(cfg)["warnings"] == []


def test_summary_reports_the_rate_range_and_time_constant(model, cfg):
    s = dict(model.summary(cfg))
    assert s["ADC sampling rate"].startswith("50,000.0 Hz") and s["ADC conversions in the run"] == "51"
    assert "-1.65 .. 1.65 V" in s["ADC input range"] and "ground" in s["Bias resistor returns to"]
    assert "28.55 us" in s["Bias time constant R x Cp"] and s["Force to reach the ADC limits"] == "+0.167 N / -0.167 N"
    cfg["circuit"]["bias_to"] = "vmid"
    assert "0 .. 3.3 V" in dict(model.summary(cfg))["ADC input range"]


def test_sensor_info_describes_the_bias_resistor_as_the_only_leak(model, cfg):
    info = model.sensor_info(cfg)
    assert sorted(info) == [0] and info[0]["cp_F"] == pytest.approx(60.7e-12, rel=2e-3) and info[0]["cextra_F"] == 0.0
    leak = info[0]["leak"]
    assert leak["r_conn_ohm"] == 470e3 and leak["v_conn_V"] == 0.0 and leak["i_conn_A"] == 0.0 and leak["c_node_F"] == 0.0
    cfg["circuit"].update(bias_to="vmid", r_bias_ohm=1e6)
    leak = model.sensor_info(cfg)[0]["leak"]
    assert leak["r_conn_ohm"] == 1e6 and leak["v_conn_V"] == pytest.approx(1.65) and leak["v_float_V"] == pytest.approx(1.65)
    assert model.forced_channels(cfg) == {0}


def test_scenarios_apply_and_build(model):
    assert set(model.scenarios) == {"single_pulse", "bias_vmid", "sine_1khz", "slow_sampling"}
    from qspice_sim.config_utils import apply_overrides
    for name, sc in model.scenarios.items():
        built = model.build(apply_overrides(model.default_config(), sc["overrides"]))
        assert built["samples"] and built["warnings"] == [], name


# ------------------------------------------------------------------ real QSPICE
@pytest.mark.parametrize("bias", ["ground", "vmid"])
def test_qspice_follows_the_rc_response_and_the_force_is_recovered(model, cfg, tmp_path, bias):
    """The simulated ADC input matches an independent integration of C dV/dt = d33 dF/dt - (V - Vbias)/R in either bias mode."""
    qspice = _qspice_or_skip()
    cfg["circuit"]["bias_to"] = bias
    cfg["adc"]["bits"] = 16
    prepared = runner.prepare(model, cfg)
    raw = runner.run_netlist(prepared.netlist, ["ain", "f0"], tmp_path, qspice)
    try:
        bm = model.adapter.bm
        sp, r, rest = bm.sensor_params(cfg), cfg["circuit"]["r_bias_ohm"], model.rest_voltage(cfg)
        t = np.array([s["t"] for s in prepared.samples])
        v = np.interp(t, raw.time, raw.column("ain"))
        tt = np.linspace(0.0, prepared.t_stop, 400_001)
        force = np.interp(tt, raw.time, raw.column("f0"))
        a = np.exp(-(tt[1] - tt[0]) / (r * sp["cp"]))
        ref = np.empty_like(tt)
        ref[0] = rest
        for i in range(1, len(tt)):
            ref[i] = rest + (ref[i - 1] - rest) * a + sp["d33"] * (force[i] - force[i - 1]) / sp["cp"] * (1 + a) / 2
        assert np.max(np.abs(v - np.interp(t, tt, ref))) < 0.2e-3                      # solver tolerance, a fraction of a 12-bit LSB
        assert v[0] == pytest.approx(rest) and np.max(v) - rest == pytest.approx(0.53, abs=0.02)   # 0.99 V open circuit, drained during the 40 us rise
        table = build_sample_table(prepared.samples, v, model.adc_params(cfg))
        assert table.clipped_count() == 0
        fin = lambda ch, tq: np.interp(tq, raw.time, raw.column("f0"))                  # noqa: E731
        comp = compute_all(table, [0], model.sensor_info(cfg), FORCE_DEFAULTS, rest, fin)[0]
    finally:
        raw.close()
    assert comp.F.max() == pytest.approx(0.1, rel=0.01) and abs(comp.F[-1]) < 1e-3     # the bias decay is compensated: 0.1 N, back to zero


def test_qspice_slow_sampling_misses_what_the_bias_resistor_drained(model, cfg, tmp_path):
    qspice = _qspice_or_skip()
    cfg["adc"]["sampling_rate_hz"] = 5e3
    cfg["simulation"]["run_duration_ms"] = 2.0
    prepared = runner.prepare(model, cfg)
    raw = runner.run_netlist(prepared.netlist, ["ain"], tmp_path, qspice)
    try:
        t = np.array([s["t"] for s in prepared.samples])
        v = np.interp(t, raw.time, raw.column("ain"))
    finally:
        raw.close()
    assert len(t) == 11 and np.max(np.abs(v)) < 0.25                                    # the 0.5 V peaks fall between the 200 us samples


def test_qspice_slow_sampling_with_the_automatic_step_stays_accurate(model, cfg, tmp_path):
    """100 Hz sampling over 100 ms gives a 100 us automatic step; the force corners and conversions are breakpoints, so the
    RC response of the 40 us rise and the decay is still resolved."""
    qspice = _qspice_or_skip()
    cfg["adc"].update(sampling_rate_hz=100.0, t_first_sample_us=500.0)
    cfg["simulation"]["run_duration_ms"] = 100.0
    cfg["forces"][0].update(t_start_us=400.0, rise_us=40.0, width_us=100.0, fall_us=40.0)
    prepared = runner.prepare(model, cfg)
    assert prepared.max_step == pytest.approx(100e-6)
    raw = runner.run_netlist(prepared.netlist, ["ain", "f0"], tmp_path, qspice)
    try:
        sp, r = model.adapter.bm.sensor_params(cfg), cfg["circuit"]["r_bias_ohm"]
        tt = np.linspace(0.0, 2e-3, 400_001)                                            # everything happens in the first 2 ms
        force = np.interp(tt, raw.time, raw.column("f0"))
        a = np.exp(-(tt[1] - tt[0]) / (r * sp["cp"]))
        ref = np.zeros_like(tt)
        for i in range(1, len(tt)):
            ref[i] = ref[i - 1] * a + sp["d33"] * (force[i] - force[i - 1]) / sp["cp"] * (1 + a) / 2
        grid = np.linspace(380e-6, 700e-6, 200)
        assert np.max(np.abs(np.interp(grid, raw.time, raw.column("ain")) - np.interp(grid, tt, ref))) < 0.5e-3
        assert np.max(np.interp(grid, raw.time, raw.column("ain"))) > 0.3                # the transient was really simulated
    finally:
        raw.close()


# ------------------------------------------------------------------ GUI
pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6 import QtWidgets  # noqa: E402

from derived_helpers import install_result, tick  # noqa: E402
from qspice_sim.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture()
def window(app):
    win = MainWindow([model_loader.load_model(QSPICE_DIR / "Basic_Model"), model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")])
    win.settings.clear()
    win.resize(1500, 900)
    win.show()
    yield win
    win.close()


def test_gui_shows_the_range_of_the_bias_choice_and_has_no_bipolar_warning(window):
    assert window.model.id == "Basic_Model" and window._range_warnings() == []
    assert "-1.65 .. 1.65 V" in window._adc_tooltip()
    window.cfg["circuit"]["bias_to"] = "vmid"
    assert "0 .. 3.3 V" in window._adc_tooltip() and window._range_warnings() == []
    assert window.model.channels == [0] and window.force_tab is not None


def test_gui_force_tab_and_adc_out_follow_the_rest_voltage(window):
    """A held 0.5 V on a ground bias is a 0.5 V step; the same voltage on a VMID bias is -1.15 V below the rest level."""
    window.force["leak_comp"] = False
    for bias, rest in (("ground", 0.0), ("vmid", 1.65)):
        window.cfg["circuit"]["bias_to"] = bias
        install_result(window, {"ain": lambda t, rest=rest: np.full_like(t, rest + 0.5), "f0": lambda t: np.zeros_like(t)})
        tick(window, "adcout_ch0", "fcal_ch0")
        window._update_force_tab()
        table = window.sample_table
        assert table.adc.v_low == (-1.65 if bias == "ground" else 0.0)
        assert table.v_out == pytest.approx(rest + 0.5, abs=table.adc.lsb)
        assert window.force_results[0].F[0] == pytest.approx(0.5 / SENS, rel=3e-3)      # first sample starts from the rest voltage (12-bit LSB = 0.16 %)
