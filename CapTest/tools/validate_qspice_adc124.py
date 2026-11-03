"""Optional validation: run the TestBoard_ADC124 capacitance analysis on QSPICE bursts.

Generates a burst from the TestBoard_ADC124 QSPICE model (one sensor connected to MUX
pin S2 for R consecutive ADC words, `timing.repeat`, a single converted mux, sensor
initially at 0 V, pin leakage off), builds the BurstData124 the firmware would send
(the converted value is the voltage on node `ain` at the model's hold instants, the
connection at the start of the sensor slot, ideal stamps) and checks the extracted
C_total against the capacitances that load the connected node in the model.

Skipped (exit code 0, message printed) when QSPICE64.exe is not installed.
usage: python tools/validate_qspice_adc124.py [--words 420] [--keep]
"""

from __future__ import annotations

import argparse
import copy
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO / "Qspice" / "Simulator"))

from captest import capfit  # noqa: E402
from captest.boards import adc124_protocol as proto  # noqa: E402
from captest.boards import load_driver  # noqa: E402
from captest.settings import Settings  # noqa: E402

CPU_HZ = 600_000_000
PRE_FRAMES = 8
SENSOR = 2                      # model sensor / mux pin S2
GUARD_NS = 200


def load_build_model():
    """The ADC124 model builder, loaded by path under its own name: the 7953 model has a module
    of the same name (build_model) and both validation tools may run in one process."""
    import importlib.util

    name = "build_model_adc124"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, REPO / "Qspice" / "TestBoard_ADC124" / "build_model.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def qspice_available() -> bool:
    try:
        from qspice_sim import runner
        runner.find_qspice()
        return True
    except Exception:
        return False


def model_config(c_pzt_pf: float, words: int):
    build_model = load_build_model()

    cfg = copy.deepcopy(build_model.load_config())
    cfg["forces"] = []
    cfg["forces_sin"] = []
    cfg["circuit"]["initial_sensor_node_v"] = 0.0
    cfg["mux"]["leak_scale"] = 0.0
    # The model derives Cp from the geometry: set eps33_r to obtain the requested Cp.
    sd = cfg["sensor_defaults"]
    area = sd["width_mm"] * 1e-3 * sd["length_mm"] * 1e-3
    eps = c_pzt_pf * 1e-12 * sd["thickness_mm"] * 1e-3 / (build_model.EPS0 * area)
    cfg["sensor_overrides"] = {str(SENSOR): {"eps33_r": eps}}
    cfg["timing"].update({"scan_order": [SENSOR], "repeat": words, "n_mux": 1, "num_cycles": 1,
                          "vmid_visit_after_each": False, "t_vmid_park_us": 0.0, "t_start_us": 20.0,
                          "tail_us": 5.0})
    return cfg


def expected_node_capacitances(cfg: dict, c_pzt_pf: float) -> dict:
    """Capacitances in the model that load the connected node (pF)."""
    m, par, op = cfg["mux"], cfg["parasitics"], cfg["opamp"]
    c_on_extra = m["c_on_pF"] - m["cs_off_pF"] - m["cd_off_pF"]
    sensor = c_pzt_pf + par["sensor_trace_c_pF"] + m["cs_off_pF"] + c_on_extra
    # D node: pin-D capacitance, trace, the eight channels' feedthrough, bias-resistor parasitic, op-amp CM input.
    shared = (m["cd_off_pF"] + par["muxd_trace_c_pF"] + 8 * m["c_feedthrough_pF"] + par["r_bias_par_c_pF"]
              + op["ccm_pF"])
    return {"sensor_side": sensor, "c_d": shared, "total": sensor + shared}


def run_burst(c_pzt_pf: float, words: int, workdir: Path):
    build_model = load_build_model()
    from qspice_sim import runner

    cfg = model_config(c_pzt_pf, words)
    netlist, slots = build_model.build(cfg)
    raw = runner.run_netlist(netlist, ["ain", "muxd"], workdir, runner.find_qspice())
    time, ain = raw.time, raw.column("ain")
    timing = build_model.adc_timing(cfg["timing"])
    slot = [s for s in slots if s["kind"] == "sensor"][0]
    t0 = slot["t0"]
    word, acq, settle = timing["word"], timing["acq"], timing["settle"]
    # Returned frame k (k = -PRE_FRAMES .. words-1) is held at t0 + settle + k*word + acq.
    ks = np.arange(-PRE_FRAMES, words)
    hold = t0 + settle + ks * word + acq
    codes = np.clip(np.rint(np.interp(hold, time, ain) / cfg["circuit"]["vdd_v"] * 4096), 0, 4095)
    f_spi = float(cfg["timing"]["adc_spi_clk_hz"])
    ends = hold - acq + 16.0 / f_spi                        # end of each 16-clock frame
    pops = ends + 100e-9                                    # the stamp latency the analysis assumes
    start = 0
    stamps = (np.rint(pops * CPU_HZ).astype(np.int64) & 0xFFFFFFFF).astype(np.uint32)
    enable = t0                                             # the model connects at the start of the slot
    sw1 = int(np.rint((enable + GUARD_NS * 1e-9) * CPU_HZ))
    sw0 = int(np.rint((enable - 0.2e-6) * CPU_HZ))
    deltas = np.diff(stamps.astype(np.int64))
    burst = proto.BurstData124(
        array=2, address=SENSOR, input_mask=0b0001, pre_frames=PRE_FRAMES, words=codes.astype(np.uint16),
        stamps=stamps, cpu_hz=CPU_HZ, spi_clock_hz=int(f_spi), boot_id=1, start_cycles=start,
        lead_cycles=start, switch_begin_cycles=sw0, switch_end_cycles=sw1,
        period_min_cycles=int(deltas.min()), period_sum_cycles=int(deltas.sum()),
        period_max_cycles=int(deltas.max()))
    return burst, cfg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--words", type=int, default=420)
    parser.add_argument("--keep", action="store_true", help="keep the QSPICE work directory")
    args = parser.parse_args(argv)
    if not qspice_available():
        print("QSPICE64.exe not found (set QSPICE_EXE): validation skipped.")
        return 0
    workdir = Path(tempfile.mkdtemp(prefix="captest_qspice124_"))
    driver = load_driver("TestBoard_ADC124")
    settings = Settings.for_board("TestBoard_ADC124")
    settings.c_parasitic_pf = 0.0
    ref = (2, 1, SENSOR)                                    # MUX1 -> IN1 is converted; R_bias is the model's 470 k
    settings.bias[driver.key(ref)] = 470e3
    failures, totals = 0, {}
    for c_pzt in (60.0, 100.0):
        burst, cfg = run_burst(c_pzt, args.words, workdir)
        m = driver.analyze(burst, [ref], settings, {ref: None})[ref]
        expect = expected_node_capacitances(cfg, c_pzt)
        totals[c_pzt] = m.c_total * 1e12
        print("Cp=%5.1f pF: C_total %.2f pF (model nodes %.2f), C_D %.2f pF (model %.2f), "
              "ratio %.3f (model %.3f), tau %.2f us, flags %s"
              % (c_pzt, m.c_total * 1e12, expect["total"], m.c_d * 1e12, expect["c_d"], m.ratio,
                 expect["c_d"] / expect["total"], m.tau * 1e6, m.flags))
        if abs(m.c_total * 1e12 / expect["total"] - 1) > 0.05:
            print("  FAIL: C_total differs from the model's node capacitance by more than 5 %")
            failures += 1
    delta = totals[100.0] - totals[60.0]
    print("Delta C_total for Delta Cp = 40 pF: %.2f pF" % delta)
    if abs(delta - 40.0) > 1.2:
        print("  FAIL: sensitivity to Cp is off by more than 3 %")
        failures += 1
    if not args.keep:
        for path in workdir.glob("*"):
            path.unlink(missing_ok=True)
        workdir.rmdir()
    print("PASS" if not failures else "%d check(s) failed" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
