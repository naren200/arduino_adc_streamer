"""Optional validation: run the capacitance-test analysis on QSPICE bursts.

Generates a burst from the TestBoard_7953 QSPICE model (one sensor connected for
N consecutive frames, other ADC pair parked, sensor initially at 0 V, pin
leakage off), builds the same BurstData the firmware would send (the converted
value is the voltage on node `sh` at the hold instants) and checks the extracted
C_total against the model's node capacitances.

Skipped (exit code 0, message printed) when QSPICE64.exe is not installed.
usage: python tools/validate_qspice.py [--samples 400] [--keep]
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
sys.path.insert(0, str(REPO / "Qspice" / "TestBoard_7953"))

from captest import capfit, protocol  # noqa: E402

CPU_HZ = 600_000_000
PARK_FRAMES = 120


def qspice_available() -> bool:
    try:
        from qspice_sim import runner
        runner.find_qspice()
        return True
    except Exception:
        return False


def model_config(channel: int, c_pzt_pf: float, samples: int, gap_ns: int = 200):
    import build_model

    cfg = copy.deepcopy(build_model.load_config())
    cfg["forces"] = []
    cfg["forces_sin"] = []
    cfg["circuit"]["initial_sensor_node_v"] = 0.0
    cfg["adc"]["leak_scale"] = 0.0
    # The model derives Cp from the geometry: set eps33_r to obtain the requested Cp.
    sd = cfg["sensor_defaults"]
    area = sd["width_mm"] * 1e-3 * sd["length_mm"] * 1e-3
    eps = c_pzt_pf * 1e-12 * sd["thickness_mm"] * 1e-3 / (build_model.EPS0 * area)
    cfg["sensor_overrides"] = {str(channel): {"eps33_r": eps}}
    t = cfg["timing"]
    t.update({"scan_order": [channel], "repeat": samples, "vmid_between_channels": False,
              "other_adc_frames": PARK_FRAMES, "num_cycles": 1, "frame_gap_ns": gap_ns,
              "first_clock_delay_ns": 25.0, "tail_us": 5.0})
    return cfg


def expected_node_capacitances(cfg: dict, c_pzt_pf: float) -> dict:
    """Capacitances in the model that load the connected node (pF)."""
    a, c, op, par = cfg["adc"], cfg["circuit"], cfg["opamp"], cfg["parasitics"]
    sensor = c_pzt_pf + par["sensor_trace_c_pF"] + a["cs_off_pF"]
    # MXO capacitor, the 14 other channels' feedthrough capacitors, op-amp common-mode input.
    shared = c["c_mxo_pF"] + 14 * a["c_feedthrough_pF"] + op["ccm_pF"]
    return {"sensor_side": sensor, "c_d": shared, "total": sensor + shared}


def run_burst(channel: int, c_pzt_pf: float, samples: int, workdir: Path) -> tuple:
    import build_model
    from qspice_sim import runner

    cfg = model_config(channel, c_pzt_pf, samples)
    netlist, sched = build_model.build(cfg)
    qspice = runner.find_qspice()
    raw = runner.run_netlist(netlist, ["sh", "mxo"], workdir, qspice)
    time, sh = raw.time, raw.column("sh")
    frames = [f for f in sched["frames"] if f["stream"]]       # this ADC's stream: VMID, X*N, VMID
    ft = sched["timing"]
    hold_at = [f["cs"] + ft["a_off"] * 0.5 for f in frames]    # mid-hold instant of every frame
    values = np.interp(hold_at, time, sh)
    vref = cfg["circuit"]["vref_v"]
    codes = np.clip(np.rint(values / vref * 4096), 0, 4095).astype(np.int64)
    n = samples
    tags = np.array([protocol.VMID_CHANNEL, protocol.VMID_CHANNEL] + [channel] * n)
    words = ((tags << 12) | codes[:n + 2]).astype(np.uint16)
    cs = np.array([f["cs"] for f in frames[:n + 2]])
    stamps = (np.rint(cs * CPU_HZ).astype(np.int64) & 0xFFFFFFFF).astype(np.uint32)
    park_cs = [f["cs"] for f in sched["frames"] if not f["stream"]][-8:]
    park_codes = np.clip(np.rint(np.interp(
        [c + ft["a_off"] * 0.5 for c in park_cs], time, sh) / vref * 4096), 0, 4095).astype(np.int64)
    vmid_words = ((protocol.VMID_CHANNEL << 12) | park_codes).astype(np.uint16)
    burst = protocol.BurstData(
        adc=2, channel=channel, n_samples=n, words=words, timestamps=stamps,
        vmid_words=vmid_words, cpu_hz=CPU_HZ, spi_clock_hz=int(cfg["timing"]["spi_clk_hz"]),
        gap_ns=200, first_clock_delay_ns=25, boot_id=1, park_us=0, park_frames=PARK_FRAMES,
        period_min_cycles=int(np.diff(stamps).min()), period_sum_cycles=int(np.diff(stamps).sum()),
        period_max_cycles=int(np.diff(stamps).max()))
    return burst, cfg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=400)
    parser.add_argument("--keep", action="store_true", help="keep the QSPICE work directory")
    args = parser.parse_args(argv)
    if not qspice_available():
        print("QSPICE64.exe not found (set QSPICE_EXE): validation skipped.")
        return 0
    workdir = Path(tempfile.mkdtemp(prefix="captest_qspice_"))
    r_bias = 470e3                                     # the model's R_bias (ADC2/ADC3 value)
    settings = capfit.AnalysisSettings(vref=2.5, discard_k=2, v_s0=0.0, c_parasitic_pf=0.0)
    failures = 0
    totals = {}
    for c_pzt in (60.0, 100.0):
        burst, cfg = run_burst(3, c_pzt, args.samples, workdir)
        m = capfit.analyze_burst(burst, r_bias, settings)
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
