"""B8: force from the generated charge - leakage and charge-sharing compensation."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import numpy as np
import pytest

from qspice_sim import model_loader, runner
from qspice_sim.adc_model import AdcParams, build_sample_table
from qspice_sim.force_calc import (FORCE_DEFAULTS, REF_FIRST, LeakParams, SensorParams, compute_all, compute_channel,
                                   extract_force, extract_force_leaky, formulas_html, resolve_params)

QSPICE_DIR = Path(__file__).resolve().parents[2]
VMID = 1.65
D33 = 600e-12
CP = 60.7e-12


def _leak(**kw) -> LeakParams:
    base = dict(c_side=5e-12, c_node=13e-12, r_conn=470e3, v_conn=VMID, i_conn=1e-9, r_float=10e9, v_float=0.0, i_float=1e-9)
    base.update(kw)
    return LeakParams(**base)


def _params(leak: LeakParams | None) -> SensorParams:
    return SensorParams(d33=D33, c_sensor=CP, c_extra=0.0, leak=leak)


def _windows(n: int, period: float = 119e-6, same: bool = False):
    """Connection windows of n visits: connected 5 us per period, conversion 1.16 us after the connection."""
    k = np.arange(n)
    t_on = np.full(n, 20e-6) if same else 20e-6 + period * k
    t = 20e-6 + period * k + 1.19e-6
    t_off = np.full(n, 20e-6 + period * n) if same else t_on + 5e-6
    return t, t_on, t_off


def _rk4(v, d, c, r, vr, i, q, steps=400):
    """Integrate C dV/dt = -(V - vr)/r - i + q over d (independent of the closed form of the implementation)."""
    h = d / steps

    def f(x):
        return (-(x - vr) / r - i + q) / c

    for _ in range(steps):
        k1 = f(v)
        k2 = f(v + h / 2 * k1)
        k3 = f(v + h / 2 * k2)
        k4 = f(v + h * k3)
        v += h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
    return v


def _forward(p: SensorParams, t, t_on, t_off, dq, vmid=VMID):
    """Node voltage at every conversion for the given generated charge per interval (uniform arrival)."""
    L, cs, ct, rho = p.leak, p.c_float, p.c_conn, p.rho
    out = np.zeros(len(t))
    # first conversion: charge arrives just before the connection, then connected until the conversion
    v = rho * (vmid + dq[0] / cs) + (1 - rho) * L.v_conn
    out[0] = _rk4(v, t[0] - t_on[0], ct, L.r_conn, L.v_conn, L.i_conn, 0.0)
    for k in range(1, len(t)):
        q = dq[k] / (t[k] - t[k - 1])
        if t_on[k] <= t[k - 1]:
            out[k] = _rk4(out[k - 1], t[k] - t[k - 1], ct, L.r_conn, L.v_conn, L.i_conn, q)
            continue
        v = _rk4(out[k - 1], t_off[k - 1] - t[k - 1], ct, L.r_conn, L.v_conn, L.i_conn, q)
        v = _rk4(v, t_on[k] - t_off[k - 1], cs, L.r_float, L.v_float, L.i_float, q)
        v = rho * v + (1 - rho) * L.v_conn
        out[k] = _rk4(v, t[k] - t_on[k], ct, L.r_conn, L.v_conn, L.i_conn, q)
    return out


# ------------------------------------------------------------------ the maths
def test_without_leakage_and_node_capacitance_it_is_the_ideal_formula():
    leak = _leak(c_side=0.0, c_node=0.0, r_conn=np.inf, r_float=np.inf, i_conn=0.0, i_float=0.0)
    p = _params(leak)
    t, t_on, t_off = _windows(6)
    v = VMID + np.array([0.0, 0.1, 0.35, 0.35, 0.2, 0.5])
    dF, F = extract_force_leaky(v, t, t_on, t_off, p, VMID)
    ideal = extract_force(v, _params(None), VMID)
    assert dF == pytest.approx(ideal[0], abs=1e-12) and F == pytest.approx(ideal[1], abs=1e-12)
    assert extract_force_leaky(v, t, t_on, t_off, p, VMID, first_is_zero=True)[0][0] == 0.0


@pytest.mark.parametrize("leak", [
    _leak(),                                                                    # ADC124-like
    _leak(r_float=40e6, i_float=3e-9, c_node=25e-12),                           # strong leakage while floating
    _leak(r_conn=2e6, i_conn=0.0, r_float=np.inf, i_float=0.0),                 # weak bias resistor, no floating leakage
])
@pytest.mark.parametrize("same", [False, True])
def test_the_generated_charge_is_recovered_through_the_phases(leak, same):
    p = _params(leak)
    t, t_on, t_off = _windows(8, same=same)
    rng = np.random.default_rng(7)
    dq = rng.normal(0.0, 2e-12, 8)
    dq[3] = 0.0                                                                  # a quiet interval
    v = _forward(p, t, t_on, t_off, dq)
    dF, F = extract_force_leaky(v, t, t_on, t_off, p, VMID)
    assert dF * D33 == pytest.approx(dq, abs=2e-15 + 1e-6 * np.abs(dq).max())
    assert F == pytest.approx(np.cumsum(dq) / D33, rel=1e-5, abs=1e-6)


def test_decay_alone_is_no_force_and_the_ideal_formula_would_see_one():
    p = _params(_leak(r_conn=300e3))
    t, t_on, t_off = _windows(10)
    dq = np.zeros(10)
    dq[1] = 30e-12                                                               # 0.05 N step, then constant force
    v = _forward(p, t, t_on, t_off, dq)
    dF, F = extract_force_leaky(v, t, t_on, t_off, p, VMID)
    assert F[1:] == pytest.approx(0.05, rel=1e-5) and np.all(np.abs(dF[2:]) < 1e-7)
    ideal = extract_force(v, _params(None), VMID)[1]
    assert ideal[-1] < 0.4 * 0.05                                                # the ideal model loses most of it


def test_first_sample_reference_starts_at_zero():
    p = _params(_leak())
    t, t_on, t_off = _windows(5)
    dq = np.array([5e-12, 2e-12, 0.0, -3e-12, 1e-12])
    v = _forward(p, t, t_on, t_off, dq)
    first = extract_force_leaky(v, t, t_on, t_off, p, VMID, first_is_zero=True)[0]
    assert first[0] == 0.0 and first[1:] == pytest.approx(extract_force_leaky(v, t, t_on, t_off, p, VMID)[0][1:], rel=1e-3, abs=1e-6)


# ------------------------------------------------------------------ settings and fall-back
INFO = {"d33_C_per_N": D33, "cp_F": CP, "cextra_F": 0.0,
        "leak": {"c_side_F": 5e-12, "c_node_F": 13e-12, "r_conn_ohm": 470e3, "v_conn_V": VMID, "i_conn_A": 1e-9,
                 "r_float_ohm": 10e9, "v_float_V": 0.0, "i_float_A": 1e-9}}


def _table(v, windows=True, address=3, same=False):
    t, t_on, t_off = _windows(len(v), same=same)
    samples = [{"t": float(t[k]), "label": "x", "address": address} for k in range(len(v))]
    if windows:
        for k, s in enumerate(samples):
            s.update(t_on=float(t_on[k]), t_off=float(t_off[k]), node_reset=True)
    return build_sample_table(samples, np.asarray(v, dtype=float), AdcParams(bits=16, vref=3.3, vmid=VMID))


def test_overrides_replace_the_model_values_and_follow_when_empty():
    p = resolve_params(FORCE_DEFAULTS, INFO).leak
    assert (p.c_side, p.c_node, p.r_conn, p.r_float, p.i_float) == pytest.approx((5e-12, 13e-12, 470e3, 10e9, 1e-9))
    assert p.i_conn == pytest.approx(1e-9)
    s = dict(FORCE_DEFAULTS, c_side_pF=7.0, cextra_pF=20.0, r_conn_ohm=1e6, r_float_ohm=1e8, i_float_nA=2.0, i_conn_nA=-3.0)
    q = resolve_params(s, INFO).leak
    assert (q.c_side, q.c_node, q.r_conn, q.r_float, q.i_float, q.i_conn) == pytest.approx((7e-12, 20e-12, 1e6, 1e8, 2e-9, -3e-9))
    assert resolve_params(dict(FORCE_DEFAULTS, i_conn_nA=0.0), INFO).leak.i_conn == 0.0          # zero is a value, not "follow the model"
    assert resolve_params(dict(FORCE_DEFAULTS, leak_comp=False), INFO).leak is None        # checkbox off = ideal model
    assert resolve_params(FORCE_DEFAULTS, {k: v for k, v in INFO.items() if k != "leak"}).leak is None   # model without data
    for bad in (dict(r_conn_ohm=0.0), dict(r_float_ohm=-1.0), dict(c_side_pF=-1.0)):
        with pytest.raises(ValueError):
            resolve_params(dict(FORCE_DEFAULTS, **bad), INFO)


def test_compute_uses_the_compensation_and_falls_back_to_the_ideal_formula_without_windows():
    v = VMID + np.array([0.3, 0.27, 0.25, 0.23])
    leaky = compute_channel(_table(v), 3, resolve_params(FORCE_DEFAULTS, INFO), dict(FORCE_DEFAULTS, source="ideal ADC input"), VMID)
    assert leaky.leak_used and not leaky.leak_missing
    ideal = compute_channel(_table(v), 3, resolve_params(dict(FORCE_DEFAULTS, leak_comp=False), INFO),
                            dict(FORCE_DEFAULTS, leak_comp=False, source="ideal ADC input"), VMID)
    assert not ideal.leak_used and ideal.F[-1] < leaky.F[-1]                 # the compensated force keeps what the ideal one loses
    nowin = compute_channel(_table(v, windows=False), 3, resolve_params(FORCE_DEFAULTS, INFO),
                            dict(FORCE_DEFAULTS, source="ideal ADC input"), VMID)
    assert not nowin.leak_used and "connection times" in nowin.leak_missing and nowin.F == pytest.approx(ideal.F)


def test_formulas_describe_the_leakage_model_with_live_numbers():
    html = formulas_html(resolve_params(FORCE_DEFAULTS, INFO), 0.8e-3, 3, "VMID", "ADC-Out")
    for text in ("leakage and charge-sharing compensation", "dQ[k]", "&tau;", "4.7e+05", "&rho;", "e<sup>"):
        assert text in html
    assert "ideal charge model" in formulas_html(resolve_params(dict(FORCE_DEFAULTS, leak_comp=False), INFO), None, 3, "VMID", "x").lower()


def test_formulas_explain_the_breakdown_and_both_connection_cases():
    p = resolve_params(FORCE_DEFAULTS, INFO)
    v = VMID + np.array([0.3, 0.27, 0.25, 0.23])
    settings = dict(FORCE_DEFAULTS, source="ideal ADC input")
    switched = compute_channel(_table(v), 3, p, settings, VMID)
    const = compute_channel(_table(v, same=True), 3, p, settings, VMID)
    assert (switched.n_switched, switched.n_const) == (3, 0) and (const.n_switched, const.n_const) == (0, 3)
    html = formulas_html(p, 0.8e-3, 3, "VMID", "ADC-Out", const)
    for text in ("Charge from the PZT", "Leakage through the bias resistor", "Net leakage into the mux node",
                 "Insulation leakage", "Mux off-leakage", "Charge sharing", "Mux switching", "Constant connection",
                 "h[k] = ", "R<sub>conn</sub>&middot;(1 &minus; a)", "0 conversion(s) follow the mux switching",
                 "3 follow on a constant connection", "4.7e+05 ohm", "&rho; = 0.8"):
        assert text in html, text
    assert "3 conversion(s) follow the mux switching" in formulas_html(p, None, 3, "VMID", "x", switched)
    assert "G" not in [w.strip("(),.:") for w in html.replace("&middot;", " ").replace("/", " ").split()]   # no gain in any formula


def test_no_front_end_gain_in_the_settings_and_old_settings_with_a_gain_still_load(window, tmp_path, monkeypatch):
    from PyQt6 import QtWidgets as W

    assert "gain" not in FORCE_DEFAULTS and "gain" not in dict(resolve_params(FORCE_DEFAULTS, INFO).__dict__)
    assert "Front-end gain G" not in [r.item["label"] for r in window.force_tab.form.rows]
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"format": 1, "model": "TestBoard_ADC124", "config": window.cfg,
                               "force": {"gain": 2.0, "d33_pC_per_N": 900.0}}), encoding="utf-8")
    monkeypatch.setattr(W.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(old), "")))
    window.load_settings()
    assert "gain" not in window.force and window.force["d33_pC_per_N"] == 900.0


# ------------------------------------------------------------------ the models
@pytest.mark.parametrize("model_id", ["TestBoard_ADC124", "TestBoard_7953"])
def test_models_report_leak_data_from_their_config_and_connection_windows(model_id):
    model = model_loader.load_model(QSPICE_DIR / model_id)
    cfg = model.default_config()
    leak = model.sensor_info(cfg)[2]["leak"]
    assert leak["r_conn_ohm"] == pytest.approx(470e3) and leak["v_conn_V"] == cfg["circuit"]["vmid_v"]
    assert leak["r_float_ohm"] == pytest.approx(1e10) and leak["v_float_V"] == 0.0 and leak["i_float_A"] > 0
    assert 3e-12 < leak["c_side_F"] < 10e-12 and 10e-12 < leak["c_node_F"] < 20e-12
    cfg["circuit"]["r_bias_ohm"] = 1e6
    assert model.sensor_info(cfg)[2]["leak"]["r_conn_ohm"] == pytest.approx(1e6)
    cfg = model.default_config()
    samples = model.build(cfg)["samples"]
    assert samples and all(s["t_on"] < s["t"] < s["t_off"] for s in samples)
    if model_id == "TestBoard_7953":                                      # sensors follow each other without a VMID frame by default
        assert samples[0]["node_reset"] and not samples[1]["node_reset"]
        cfg["timing"]["vmid_between_channels"] = True
        samples = model.build(cfg)["samples"]
    assert all(s["node_reset"] for s in samples)                          # VMID between the channels


def test_floating_leakage_only_acts_while_released_and_connected_leakage_acts_always():
    v = VMID + np.array([0.3, 0.27, 0.25, 0.23, 0.22])
    settings = dict(FORCE_DEFAULTS, source="ideal ADC input")

    def run(same, **kw):
        s = dict(settings, **kw)
        return compute_channel(_table(v, same=same), 3, resolve_params(s, INFO), s, VMID).F

    # a sensor that stays connected never floats: I_float (0 or 1000 nA) changes nothing
    assert run(True, i_float_nA=0.0) == pytest.approx(run(True, i_float_nA=1000.0)) and run(True) == pytest.approx(run(True, i_float_nA=0.0))
    # with mux switching it does
    assert not np.allclose(run(False, i_float_nA=0.0), run(False, i_float_nA=1000.0))
    # the connected leakage acts in both
    for same in (True, False):
        assert not np.allclose(run(same, i_conn_nA=0.0), run(same, i_conn_nA=-6.0))


def test_adc124_connected_leakage_counts_the_off_channels_of_the_mux():
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    cfg = model.default_config()
    assert model.sensor_info(cfg)[2]["leak"]["i_conn_A"] == pytest.approx(-6e-9)             # 1 nA drain leakage - 7 x 1 nA S->D
    cfg["mux"].update(ileak_d_nA=2.0, ileak_s_off_nA=0.5, leak_scale=2.0)
    leak = model.sensor_info(cfg)[2]["leak"]
    assert leak["i_conn_A"] == pytest.approx((2.0 - 7 * 0.5) * 2e-9) and leak["i_float_A"] == pytest.approx(1e-9)


def test_7953_pin_leakage_goes_to_ground_so_no_off_channel_current_reaches_the_node():
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_7953")
    leak = model.sensor_info(model.default_config())[2]["leak"]
    assert leak["i_conn_A"] == pytest.approx(leak["i_float_A"]) and leak["i_conn_A"] > 0     # the pin leakage of the datasheet only


def test_adc124_windows_follow_the_park_mode_and_the_vmid_visit():
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    cfg = model.default_config()
    cfg["timing"]["vmid_visit_after_each"] = False
    resets = [s["node_reset"] for s in model.build(cfg)["samples"]]
    assert resets[0] and not any(resets[1:7])                             # a sensor straight after another one: the node keeps its charge
    cfg = copy.deepcopy(model.default_config())
    cfg["timing"].update(park_on_vmid=False, n_mux=1, repeat=10, scan_order=[0], num_cycles=4)
    samples = model.build(cfg)["samples"]
    assert len({s["t_on"] for s in samples}) == 1 and len({s["t_off"] for s in samples}) == 1     # one connection for the whole run
    assert samples[0]["t_off"] > samples[-1]["t"]


# ------------------------------------------------------------------ GUI
pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6 import QtWidgets  # noqa: E402

from qspice_sim.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture()
def window(app):
    win = MainWindow([model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")])
    win.settings.clear()
    win.resize(1500, 900)
    win.show()
    yield win
    win.close()


def _row(window, path):
    return next(r for r in window.force_tab.form.rows if r.path == path)


def test_force_tab_has_the_leakage_fields_with_model_hints_and_default_on(window):
    assert window.force["leak_comp"] is True and _row(window, "force.leak_comp").editor.isChecked()
    labels = [r.item["label"] for r in window.force_tab.form.rows]
    for name in ("Compensate leakage and charge sharing", "Sensor-side parasitic capacitance", "Leak resistance, sensor connected",
                 "Leak resistance, sensor floating", "Leakage current, sensor floating", "Leakage current, sensor connected"):
        assert name in labels
    hints = {p: _row(window, p).editor.placeholderText() for p in
             ("force.r_conn_ohm", "force.r_float_ohm", "force.i_float_nA", "force.c_side_pF", "force.cextra_pF")}
    assert hints["force.r_conn_ohm"] == "model: 4.7e+05 ohm" and hints["force.r_float_ohm"] == "model: 1e+10 ohm"
    assert hints["force.i_float_nA"] == "model: 1 nA" and hints["force.c_side_pF"] == "model: 5 pF"
    assert hints["force.cextra_pF"] == "model: 13.1 pF"                   # the mux node capacitance in the compensated model
    assert "leakage and charge-sharing compensation" in window.force_tab.formulas.toPlainText()
    _row(window, "force.leak_comp").editor.setChecked(False)
    assert window.force["leak_comp"] is False and "ideal charge model" in window.force_tab.formulas.toPlainText().lower()
    assert _row(window, "force.cextra_pF").editor.placeholderText() == "model: 0 pF"


def test_changing_a_leakage_value_replots_without_a_run_and_round_trips_through_the_settings(window, tmp_path):
    from derived_helpers import install_result, tick

    sens = D33 / CP
    install_result(window, {"ain": lambda t: 1.65 + sens * 0.05 * (t > 2e-4) * 0.8, "f2": lambda t: 0.05 * (t > 2e-4)})
    tick(window, "ain", "fcal_ch2")
    base = window.force_results[2].F.copy()
    assert window.force_results[2].leak_used
    row = _row(window, "force.r_conn_ohm")
    row.editor.setText("1e6")
    row.editor.editingFinished.emit()
    assert window.force["r_conn_ohm"] == 1e6 and window.worker is None
    assert not np.allclose(window.force_results[2].F, base) and row.reset.isEnabled()
    assert {k: window.force[k] for k in ("leak_comp", "r_conn_ohm")} == {"leak_comp": True, "r_conn_ohm": 1e6}
    row._on_reset()
    assert window.force["r_conn_ohm"] is None and np.allclose(window.force_results[2].F, base)
