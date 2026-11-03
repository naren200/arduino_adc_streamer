"""capfit unit tests: fit accuracy, realistic noise, tag/missing/non-uniform data."""

import numpy as np
import pytest

from captest import capfit, protocol
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings, key
from captest.sim import SimConfig, SimLink, SimulatedDevice


def synthetic(tau=40e-6, v0=0.2, vinf=1.25, n=400, step=1.1e-6, noise=0.0, seed=1, jitter=0.0):
    rng = np.random.default_rng(seed)
    t = step * (np.arange(n) + 1) + rng.normal(0, jitter, n).cumsum() * 0
    t = np.sort(t + rng.uniform(-jitter, jitter, n))
    v = vinf - (vinf - v0) * np.exp(-t / tau) + rng.normal(0, noise, n)
    return t, v


def test_noiseless_recovery_within_one_percent():
    t, v = synthetic()
    fit = capfit.fit_exponential(t, v)
    assert fit.ok
    assert fit.tau == pytest.approx(40e-6, rel=0.01)
    assert fit.v0 == pytest.approx(0.2, rel=0.01)
    assert fit.vinf == pytest.approx(1.25, rel=0.01)


def test_numpy_only_fallback_matches(monkeypatch):
    monkeypatch.setattr(capfit, "_curve_fit", None)
    t, v = synthetic(tau=25e-6, v0=0.3)
    fit = capfit.fit_exponential(t, v)
    assert fit.method == "varpro"
    assert fit.tau == pytest.approx(25e-6, rel=0.01)


def test_non_uniform_timestamps():
    t, v = synthetic(jitter=0.3e-6, tau=30e-6)
    fit = capfit.fit_exponential(t, v)
    assert fit.tau == pytest.approx(30e-6, rel=0.01)


def run_channel(r_bias, c_pzt_pf, c_d_pf=12.0, noise=1.0, seed=3, adc=2, channel=0, **cfg):
    table = {key(adc, channel): r_bias}
    device = SimulatedDevice(
        SimConfig(c_pzt_pf=c_pzt_pf, c_pzt_spread=0.0, c_d_pf=c_d_pf, noise_lsb=noise,
                  r_bias_tolerance=0.0, seed=seed, **cfg),
        clock=VirtualClock(), r_bias_table=table)
    client = CapTestClient(SimLink(device))
    settings = Settings()
    settings.bias[key(adc, channel)] = r_bias
    n = settings.resolve_samples(adc, channel)
    burst = client.burst(adc, channel, n, 200, 200, 25)
    m = capfit.analyze_burst(burst, r_bias, settings.analysis())
    return m, device.true_values(adc, channel)


@pytest.mark.parametrize("r_bias", [249e3, 470e3, 1e6])
@pytest.mark.parametrize("c_total_pf", [60.0, 80.0, 100.0])
def test_realistic_noise_accuracy(r_bias, c_total_pf):
    c_d = 12.0
    m, truth = run_channel(r_bias, c_total_pf - c_d, c_d)
    assert m.ok, m.flags
    assert m.c_total == pytest.approx(truth["c_total"], rel=0.02)
    assert abs(m.ratio - truth["ratio"]) < 0.01
    assert capfit.LOW_AMPLITUDE not in m.flags
    assert capfit.PERIODIC_RESIDUAL not in m.flags


def test_charge_injection_pedestal_biases_ratio_not_tau():
    m, truth = run_channel(470e3, 70, vmid_pedestal_v=0.02)
    assert m.c_total == pytest.approx(truth["c_total"], rel=0.02)
    assert m.v_d == pytest.approx(1.23, abs=0.002)


def test_tag_mismatch_is_discarded_and_flagged():
    device = SimulatedDevice(SimConfig(seed=5), clock=VirtualClock())
    client = CapTestClient(SimLink(device))
    burst = client.burst(2, 0, 400, 200, 200, 25)
    words = burst.words.copy()
    words[100] = (7 << 12) | (words[100] & 0x0FFF)
    words[101] = (3 << 12) | 4000   # a wild value with a wrong tag must not affect the fit
    burst.words = words
    settings = Settings()
    m = capfit.analyze_burst(burst, settings.r_bias(2, 0), settings.analysis())
    assert capfit.TAG_MISMATCH in m.flags
    assert m.n_used == 400 - settings.discard_k - 2
    assert m.ok


def test_missing_samples_flag_incomplete():
    device = SimulatedDevice(SimConfig(seed=5), clock=VirtualClock())
    burst = CapTestClient(SimLink(device)).burst(2, 0, 400, 200, 200, 25)
    burst.words = burst.words[:120]
    burst.timestamps = burst.timestamps[:120]
    burst.flags |= protocol.FLAG_INCOMPLETE
    settings = Settings()
    m = capfit.analyze_burst(burst, 470e3, settings.analysis())
    assert capfit.INCOMPLETE in m.flags
    # 118 samples (~130 us) of a ~39 us tau still fit.
    assert m.tau == pytest.approx(470e3 * 83e-12, rel=0.05)

    burst.words = burst.words[:4]
    burst.timestamps = burst.timestamps[:4]
    m = capfit.analyze_burst(burst, 470e3, settings.analysis())
    assert capfit.FEW_SAMPLES in m.flags and not m.ok


def test_timestamp_wraparound():
    device = SimulatedDevice(SimConfig(seed=9), clock=VirtualClock())
    burst = CapTestClient(SimLink(device)).burst(1, 0, 500, 200, 200, 25)
    settings = Settings()
    reference = capfit.analyze_burst(burst, 1e6, settings.analysis())
    shifted = protocol.decode_burst_packet(protocol.encode_burst_packet(burst))
    shifted.timestamps = ((shifted.timestamps.astype(np.int64) + 0xFFFFFF00) & 0xFFFFFFFF).astype(np.uint32)
    again = capfit.analyze_burst(shifted, 1e6, settings.analysis())
    assert again.tau == pytest.approx(reference.tau, rel=1e-6)


def test_low_amplitude_flag():
    # Sensor nearly as charged as the node: tiny step.
    m, _ = run_channel(470e3, 70, v_s_start=1.24, leak_tau_s=1e9)
    assert capfit.LOW_AMPLITUDE in m.flags


def test_frame_periodic_pattern_flagged():
    m, _ = run_channel(470e3, 70, frame_pattern_lsb=3)
    assert capfit.PERIODIC_RESIDUAL in m.flags
    assert m.periodic_frames == pytest.approx(2.0, abs=0.05)


def test_vs0_suspect_when_too_soon():
    device = SimulatedDevice(SimConfig(seed=5), clock=VirtualClock())
    burst = CapTestClient(SimLink(device)).burst(2, 0, 300, 200, 200, 25)
    m = capfit.analyze_burst(burst, 470e3, Settings().analysis(), since_last_ms=20, discharge_ms=100)
    assert capfit.TOO_SOON in m.flags and capfit.VS0_SUSPECT in m.flags


def test_aggregate_mean_std_and_v0_inconsistency():
    ms = []
    for seed, v_start in ((1, 0.0), (2, 0.4), (3, 0.8)):
        m, _ = run_channel(470e3, 70, seed=seed, v_s_start=v_start, leak_tau_s=1e9)
        ms.append(m)
    agg = capfit.aggregate(ms)
    assert agg.count == 3
    assert capfit.V0_INCONSISTENT in agg.flags and capfit.VS0_SUSPECT in agg.flags
    assert agg.std["tau"] / agg.mean["tau"] < 0.03


def test_plateau_check():
    result = capfit.plateau_check([10, 30, 100, 300], [0.60, 0.40, 0.2005, 0.2000])
    assert result["plateau"] and result["sufficient_wait_ms"] == 100
    result = capfit.plateau_check([10, 30], [0.6, 0.3])
    assert not result["plateau"]


def test_recommended_samples_cap():
    assert capfit.recommended_samples(1e6, 100e-12, 1.1e-6) == 547
    assert capfit.recommended_samples(1e6, 1e-9, 1.1e-6) == 1024
    assert capfit.recommended_samples(1e3, 1e-12, 1.1e-6) == 32


def test_slow_structure_is_not_a_frame_pattern():
    """A ~10-frame ring or slow misfit (seen on the real board) is outside the frame-pattern band."""
    t, v = synthetic(n=400, noise=0.0004, seed=2)
    lsb = 2.5 / 4096
    v = v + 3 * lsb * np.sin(2 * np.pi * np.arange(400) / 10.4)
    r = (v - (1.25 - (1.25 - 0.2) * np.exp(-t / 40e-6))) / lsb
    assert capfit.periodic_residual(r) is None


def test_leakage_offset_is_reported_not_flagged():
    m, _ = run_channel(1e6, 70, vmid_pedestal_v=0.0)
    assert abs(m.leak_na) < 20 and capfit.VINF_OFFSET not in m.flags
