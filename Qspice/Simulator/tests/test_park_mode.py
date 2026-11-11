"""B7: "Park on VMID" off - the last sampled channel stays connected for the park time (TestBoard_ADC124)."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from qspice_sim import model_loader, runner

QSPICE_DIR = Path(__file__).resolve().parents[2]
US = 1e-6


@pytest.fixture(scope="module")
def model():
    return model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")


def _cfg(model, **timing):
    cfg = model.default_config()
    cfg["timing"].update(timing)
    return cfg


def _single(model, **extra):
    """One mux, one channel, 10 repeats, parked on the channel itself."""
    return _cfg(model, park_on_vmid=False, n_mux=1, repeat=10, scan_order=[0], **extra)


def test_default_is_park_on_vmid_and_the_label_is_park_time(model):
    assert model.default_config()["timing"]["park_on_vmid"] is True
    labels = {i["path"]: i["label"] for sect in ("system",) for g in model.manifest[sect] for i in g["items"]}
    assert labels["timing.park_on_vmid"] == "Park on VMID" and labels["timing.t_vmid_park_us"] == "Park time"


def test_key_missing_in_an_old_settings_file_means_park_on_vmid(model):
    cfg = model.default_config()
    del cfg["timing"]["park_on_vmid"]
    assert model.build(cfg)["netlist"] == model.build(model.default_config())["netlist"]


def test_one_channel_cycle_has_no_settle_and_no_vmid_slot(model):
    bm = model.adapter.bm
    slots, _, periodic = bm.build_schedule(_single(model, num_cycles=5))
    sensors = [s for s in slots if s["kind"] == "sensor"]
    assert not periodic and len(sensors) == 5 and not [s for s in slots if s["kind"] == "vmid"]
    assert all(s["address"] == 0 for s in slots)                                  # the tail also stays on the channel
    park, words, settle = 7 * US, 10 * US, 1 * US
    assert sensors[0]["t1"] - sensors[0]["t0"] == pytest.approx(settle + words + park)    # the first connection pays the settle
    for s in sensors[1:]:
        assert s["t1"] - s["t0"] == pytest.approx(words + park)                   # a visit to the connected channel does not
    t_sample = np.array([s["t_sample"] for s in sensors])
    assert np.diff(t_sample) == pytest.approx(np.full(4, words + park))           # uniform sampling, 17 us = 58.8 kHz
    assert t_sample[0] - sensors[0]["t0"] == pytest.approx(settle + 9 * US + 0.1875 * US)
    info = bm.cycle_info(_single(model))
    assert info["cycle"] == pytest.approx(17 * US) and info["switches"] == 0 and not info["on_vmid"]


def test_vmid_visit_settings_are_ignored_when_parked_on_the_last_channel(model):
    bm = model.adapter.bm
    a = bm.build_schedule(_single(model, vmid_visit_after_each=True, t_vmid_settle_us=50.0, num_cycles=3))
    b = bm.build_schedule(_single(model, vmid_visit_after_each=False, t_vmid_settle_us=1.0, num_cycles=3))
    assert a == b


def test_several_channels_stay_on_the_last_one_for_the_park_time(model):
    bm = model.adapter.bm
    cfg = _cfg(model, park_on_vmid=False, scan_order=[1, 3, 5], num_cycles=2)
    slots, _, _ = bm.build_schedule(cfg)
    sensors = [s for s in slots if s["kind"] == "sensor"]
    busy = bm.adc_timing(cfg["timing"])["busy"]
    durations = [s["t1"] - s["t0"] for s in sensors]
    assert durations[:3] == pytest.approx([busy, busy, busy + 7 * US])           # park after the last address of the sweep
    assert [s["address"] for s in sensors] == [1, 3, 5, 1, 3, 5]
    assert bm.cycle_info(cfg)["cycle"] == pytest.approx(3 * busy + 7 * US) and bm.cycle_info(cfg)["switches"] == 3
    # every visit is a real switch, so every sample is held hold_offset after its own command
    assert all(s["t_sample"] - s["t0"] == pytest.approx(bm.adc_timing(cfg["timing"])["hold_offset"]) for s in sensors)


def test_sampling_rate_sets_the_park_time_and_too_high_a_rate_is_rejected(model):
    bm = model.adapter.bm
    cfg = _single(model, sampling_rate_hz=20000.0, num_cycles=3)
    info = bm.cycle_info(cfg)
    assert info["cycle"] == pytest.approx(50 * US) and info["park"] == pytest.approx(40 * US)
    slots, _, _ = bm.build_schedule(cfg)
    assert np.diff([s["t_sample"] for s in slots if s["kind"] == "sensor"]) == pytest.approx([50 * US, 50 * US])
    with pytest.raises(ValueError, match=r"max 100000\.0 Hz"):                     # 10 words = 10 us
        bm.cycle_info(_single(model, sampling_rate_hz=150000.0))


def _enable_edges(netlist: str, addr: int) -> str:
    return re.search(rf"^Vc{addr} c{addr} 0 (.*?)(?=^V|^\.|\Z)", netlist, re.MULTILINE | re.DOTALL).group(1)


def test_single_channel_enable_is_switched_on_once_and_never_off(model):
    cfg = _single(model, num_cycles=5)
    built = model.build(cfg)["netlist"]
    pwl = re.search(r"^Vc0 c0 0 PWL\((.*?)\)$", built, re.MULTILINE).group(1).split()
    times, values = [float(x) for x in pwl[0::2]], [float(x) for x in pwl[1::2]]
    assert values == [0.0, 0.0, 1.0] and times[1] == pytest.approx(20 * US + cfg["mux"]["switch_on_ns"] * 1e-9)   # one on edge at the first command, nothing after
    assert not model.problems


def test_periodic_single_channel_is_a_step_with_a_uniform_breakpoint_period(model):
    cfg = _single(model, num_cycles=300)
    cfg["simulation"]["periodic_min_cycles"] = 200
    built = model.build(cfg)["netlist"]
    assert re.search(r"^Vc0 c0 0 PWL\(0 0 \S+ 0 \S+ 1\)$", built, re.MULTILINE)
    bp = re.search(r"^Vbp0 bp0 0 PULSE\(0 1m (\S+) 1n 1n 1n (\S+)\)$", built, re.MULTILINE)
    assert float(bp.group(2)) == pytest.approx(17 * US)
    slots, _, periodic = model.adapter.bm.build_schedule(cfg)
    assert periodic and float(bp.group(1)) == pytest.approx(slots[0]["t_sample"])
    t_sample = np.array([s["t_sample"] for s in slots])
    assert np.allclose(np.diff(t_sample), 17 * US)                                 # the breakpoints land on every sample


def test_periodic_several_channels_use_the_normal_pulses(model):
    cfg = _cfg(model, park_on_vmid=False, scan_order=[0, 1], num_cycles=300)
    built = model.build(cfg)["netlist"]
    assert re.search(r"^Vc0 c0 0 PULSE\(", built, re.MULTILINE) and re.search(r"^Vc1 c1 0 PULSE\(", built, re.MULTILINE)
    assert "Vc7_0 c7 0 PWL" in built                                                # VMID only before the first command


def test_derived_values_describe_the_mode(model):
    on = dict(model.summary(model.default_config()))
    assert on["Park mode"] == "on VMID" and on["Scan cycle"] == "119.00 us"
    off = dict(model.summary(_single(model)))
    assert off["Park mode"].startswith("stays on the last channel (addr 0)")
    assert off["Scan cycle"] == "17.00 us" and off["Sampling rate per sensor"].startswith("58,823")
    assert "0 mux switch(es)" in off["Connected time per sensor visit"] and off["Connected duty per sensor"].startswith("100.0 %")
    assert "once per sweep" in off["Park time used"]


def test_samples_and_force_calculation_inputs_keep_working(model):
    built = model.build(_single(model, num_cycles=4))
    assert [s["address"] for s in built["samples"]] == [0, 0, 0, 0] and len(built["samples"]) == 4
    assert np.diff([s["t"] for s in built["samples"]]) == pytest.approx(np.full(3, 17 * US))


def _qspice_or_skip() -> Path:
    try:
        return runner.find_qspice()
    except runner.SimulationError:
        pytest.skip("QSPICE is not installed")


def test_real_qspice_a_connected_channel_has_no_switching_disturbance(tmp_path, model):
    """Same one-channel scan with and without VMID parking: after the first conversion the connected channel sees no
    charge injection or redistribution, the VMID-parked run shows it at every reconnect."""
    qspice = _qspice_or_skip()
    spans = {}
    for park_vmid in (True, False):
        cfg = _cfg(model, park_on_vmid=park_vmid, n_mux=1, repeat=10, scan_order=[0], num_cycles=8, t_vmid_settle_us=1.0,
                   vmid_visit_after_each=True)
        cfg["forces"] = []
        prepared = runner.prepare(model, cfg)
        raw = runner.run_netlist(prepared.netlist, ["ain", "c0"], tmp_path / f"p{int(park_vmid)}", qspice)
        t, ain, c0 = raw.time, raw.column("ain"), raw.column("c0")
        slots = model.adapter.bm.build_schedule(cfg)[0]
        sensors = [s for s in slots if s["kind"] == "sensor"]
        later = (t > sensors[2]["t0"]) & (t < sensors[-1]["t1"])
        spans[park_vmid] = (float(np.ptp(ain[later])), int(np.sum(np.diff((c0[later] > 0.5).astype(int)) != 0)))
        raw.close()
    (p_vmid, edges_vmid), (p_last, edges_last) = spans[True], spans[False]
    assert edges_last == 0 and edges_vmid >= 4                                     # the enable never toggles when parked on the channel
    # measured: ~42 mV peak-to-peak with VMID parking (charge injection at every reconnect), ~0.6 mV when the channel stays connected
    assert p_last < 0.1 * p_vmid and p_last < 2e-3
