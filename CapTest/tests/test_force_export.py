import copy
import json
import math

import pytest

from captest import capfit, force_export as fx, force_model, runner, sensor_groups
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.sim import SimConfig, SimLink, SimulatedDevice

PROFILE = "TestBoard_7953"


def make_session(**config):
    clock = VirtualClock()
    settings = Settings()
    device = SimulatedDevice(SimConfig(firmware_version="1.3.0", **config), clock=clock,
                             r_bias_table=settings.bias)
    session = runner.MeasurementSession(CapTestClient(SimLink(device)), settings, clock, simulated=True)
    return session, device


@pytest.fixture(scope="module")
def gm():
    return sensor_groups.resolve(PROFILE)


@pytest.fixture(scope="module")
def measured(gm):
    """Session with the five routes of A1_PZT1 (ADC2:0-4) and A1_PZT6 (ADC1:0-4) measured."""
    session, device = make_session()
    refs = gm.get("A1_PZT1").refs + gm.get("A1_PZT6").refs
    for ref in refs:
        session.measure_channel(ref, bursts=2)
    return session, device


def test_complete_package_record_has_every_required_field(gm, measured):
    session, device = measured
    group = gm.get("A1_PZT1")
    forces = fx.ForceInputs.new(gm, session.settings, board_instance="board-7", simulated=True)
    entry = forces.add_package(group, fx.results_for_group(session.results, group),
                               session.settings, gm, board_instance="board-7", simulated=True)
    assert entry["status"] == fx.COMPLETE and forces.ready_groups() == ["A1_PZT1"]
    r = entry["routes"][0]
    assert r["position"] == "B" and r["ref"] == [2, 0] and r["status"] == "usable"
    truth = device.true_values(2, 0)
    assert r["c_total_f"] == pytest.approx(truth["c_total"], rel=0.05)
    for name in ("tau_s", "v_d_v", "c0", "c1_per_v", "qv_v", "d_min_v", "d_max_v",
                 "r_bias_ohm", "r_bias_tolerance", "residual_rms_lsb"):
        assert r[name] is not None and math.isfinite(r[name]), name
    diag = r["diagnostics"]
    for name in ("c_share_vd_f", "c_share_1v_pf", "q_cycle_pc", "t_short_us", "t_long_us"):
        assert diag[name] is not None, name
    assert len(r["per_burst"]) == 2
    burst = r["per_burst"][0]
    assert burst["status"] == "used" and burst["raw_packet_id"] in forces.data["raw_packets"]
    assert burst["v_d_v"] is not None and burst["c0"] == pytest.approx(burst["c0"])
    # c0 is the stored share ratio; c1 and qv follow from the summary fields
    m = result_measurement(session, (2, 0))
    assert burst["c0"] == pytest.approx(m.c_share_ratio)
    assert burst["qv_v"] == pytest.approx(m.connect_info["q_cycle_pc"] * 1e-12 / m.c_total)
    assert burst["c1_per_v"] == pytest.approx(
        (m.connect_info["c_share_1v_pf"] * 1e-12 - m.c_share_ratio * m.c_total) / m.c_total)
    prov = forces.data["provenance"]
    assert prov["origin"] == "simulated" and prov["board_instance"] == "board-7"
    assert forces.data["force_inputs"]["coff_f"] is None
    assert forces.data["force_inputs"]["d33_eff_c_per_n"] == {}


def result_measurement(session, ref):
    return session.results[ref].measurements[0]


def test_summary_recovery_formulas_match_per_burst_values(measured):
    session, _ = measured
    m = result_measurement(session, (2, 1))
    model = force_model.RouteModel.from_summary(
        (2, 1), r_bias=m.r_bias, c_total=m.c_total, v_d=m.v_d, c_share_ratio=m.c_share_ratio,
        c_share_1v_pf=m.connect_info["c_share_1v_pf"], q_cycle_pc=m.connect_info["q_cycle_pc"])
    coef = fx._burst_coefficients(m)
    assert (model.c0, model.c1, model.qv) == pytest.approx((coef["c0"], coef["c1"], coef["qv_v"]))


def test_partial_save_load_and_update_with_new_package(tmp_path, gm, measured):
    session, _ = measured
    forces = fx.ForceInputs.new(gm, session.settings, board_instance="b", simulated=True)
    g1, g6 = gm.get("A1_PZT1"), gm.get("A1_PZT6")
    forces.add_package(g1, fx.results_for_group(session.results, g1), session.settings, gm,
                       board_instance="b", simulated=True)
    path = forces.save(tmp_path / "force.json")
    cov = forces.coverage()
    assert cov["board"]["complete"] == 1 and cov["board"]["missing"] == 9 and not cov["ready"]
    assert fx.ForceInputs.new(gm, session.settings).status("A1_PZT1") == fx.MISSING
    assert "1 of 10 packages complete" in forces.summary_text()

    loaded = fx.ForceInputs.load(path, gm)
    assert loaded.status("A1_PZT1") == fx.COMPLETE and loaded.status("A1_PZT6") == fx.MISSING
    assert not list(tmp_path.glob("*.tmp"))              # atomic write leaves nothing behind
    loaded.add_package(g6, fx.results_for_group(session.results, g6), session.settings, gm,
                       board_instance="b", simulated=True)
    loaded.save(path)
    again = fx.ForceInputs.load(path, gm)
    assert sorted(again.ready_groups()) == ["A1_PZT1", "A1_PZT6"]
    assert again.coverage()["board"]["complete"] == 2
    assert [h["group"] for h in again.data["merge_history"]] == ["A1_PZT1", "A1_PZT6"]
    models = again.route_models("A1_PZT6")
    assert sorted(models) == [(1, i) for i in range(5)]
    assert all(m.c_total > 0 and m.signature == again.data["acquisition"]["signature"]
               for m in models.values())


def test_remeasuring_adds_a_superseding_revision_and_keeps_the_old_one(gm, measured):
    session, _ = measured
    g = gm.get("A1_PZT1")
    forces = fx.ForceInputs.new(gm, session.settings, simulated=True)
    for _ in range(2):
        forces.add_package(g, fx.results_for_group(session.results, g), session.settings, gm,
                           simulated=True)
    revs = forces.revisions("A1_PZT1")
    assert [r["revision"] for r in revs] == [1, 2]
    assert revs[0]["superseded"] is True and revs[1]["superseded"] is False
    assert forces.latest("A1_PZT1")["revision"] == 2
    assert forces.coverage()["packages"]["A1_PZT1"]["revision"] == 2


def test_failed_and_interrupted_packages_are_marked_not_ready(gm, measured):
    session, _ = measured
    g = gm.get("A1_PZT1")
    forces = fx.ForceInputs.new(gm, session.settings, simulated=True)
    results = dict(fx.results_for_group(session.results, g))
    # an interrupted pass: only three of five routes measured
    part = {ref: results[ref] for ref in list(results)[:3]}
    entry = forces.add_package(g, part, session.settings, gm, simulated=True)
    assert entry["status"] == fx.INCOMPLETE and "interrupted" in entry["note"]
    assert forces.ready_groups() == []
    with pytest.raises(fx.ForceExportError, match="not usable"):
        forces.route_models("A1_PZT1")
    # a completed pass with one failed channel
    bad = copy.deepcopy(results)
    victim = bad[(2, 2)]
    victim.failed = True
    victim.message = "no usable burst (FIT_FAILED)"
    entry = forces.add_package(g, bad, session.settings, gm, simulated=True)
    assert entry["status"] == fx.FAILED
    failed = [r for r in entry["routes"] if r["status"] == "failed"][0]
    assert failed["ref"] == [2, 2] and failed["c_total_f"] is None and failed["c0"] is None
    assert "FIT_FAILED" in failed["unavailable"]["c_total_f"]            # reason, never zero
    assert forces.status("A1_PZT1") == fx.FAILED and forces.ready_groups() == []
    cov = forces.coverage()["packages"]["A1_PZT1"]
    assert cov["usable_routes"] == 4 and cov["failed_routes"] == 1
    # the evidence of the other routes is still exported
    assert sum(1 for r in entry["routes"] if r["status"] == "usable") == 4


def test_non_alternate_bursts_are_not_usable_for_force(gm):
    session, _ = make_session()
    session.settings.method = "burst"
    g = gm.get("A1_PZT1")
    for ref in g.refs:
        session.measure_channel(ref, bursts=1)
    forces = fx.ForceInputs.new(gm, session.settings, simulated=True)
    entry = forces.add_package(g, fx.results_for_group(session.results, g), session.settings, gm,
                               simulated=True)
    assert entry["status"] == fx.FAILED
    assert all(r["status"] == "failed" for r in entry["routes"])
    assert "not the alternate" in json.dumps(entry["routes"][0]["per_burst"])


def test_merge_refused_on_mismatching_context(gm, measured):
    session, _ = measured
    g = gm.get("A1_PZT6")
    forces = fx.ForceInputs.new(gm, session.settings, board_instance="b", simulated=True)
    res = fx.results_for_group(session.results, g)

    def refused(match, **kw):
        args = dict(settings=session.settings, group_map=gm, board_instance="b", simulated=True)
        args.update(kw)
        with pytest.raises(fx.ForceExportError, match=match):
            forces.add_package(g, res, args.pop("settings"), args.pop("group_map"), **args)
        assert forces.status("A1_PZT6") == fx.MISSING             # nothing changed

    other = Settings.from_dict(json.loads(session.settings.to_json()))
    other.vmid_frames = 2
    refused("acquisition settings differ", settings=other)
    clock = Settings.from_dict(json.loads(session.settings.to_json()))
    clock.spi_clock_hz = 10_000_000
    refused("spi_clock_hz", settings=clock)
    refused("board instance", board_instance="other")
    refused("origin", simulated=False)
    changed = copy.deepcopy(json.loads(sensor_groups.default_library_path().read_text(encoding="utf-8")))
    next(c for c in changed["configurations"]
         if c.get("board_profile") == PROFILE)["reverse_polarity"] = True
    refused("layout changed", group_map=sensor_groups.resolve(PROFILE, library_data=changed))
    with pytest.raises(fx.ForceExportError, match="other routes"):
        forces.add_package(gm.get("A1_PZT1"), res, session.settings, gm, board_instance="b",
                           simulated=True)


def test_strict_json_hash_and_tamper_detection(tmp_path, gm, measured):
    session, _ = measured
    g = gm.get("A1_PZT1")
    forces = fx.ForceInputs.new(gm, session.settings, simulated=True)
    results = copy.deepcopy(fx.results_for_group(session.results, g))
    results[(2, 3)].measurements[0].c_total = float("nan")          # NaN must never be written
    forces.add_package(g, results, session.settings, gm, simulated=True)
    path = forces.save(tmp_path / "f.json")
    text = path.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    json.loads(text, parse_constant=lambda c: pytest.fail(c))
    # tamper: change one value
    data = json.loads(text)
    data["packages"]["A1_PZT1"][0]["routes"][0]["c_total_f"] *= 2
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(fx.ForceExportError, match="hash mismatch"):
        fx.ForceInputs.load(bad)
    # NaN literal rejected
    nan_file = tmp_path / "nan.json"
    nan_file.write_text(text.replace('"revision": 1', '"revision": NaN', 1), encoding="utf-8")
    with pytest.raises(fx.ForceExportError, match="strict JSON"):
        fx.ForceInputs.load(nan_file)
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"format": "other"}), encoding="utf-8")
    with pytest.raises(fx.ForceExportError, match="not a"):
        fx.ForceInputs.load(wrong)
    with pytest.raises(fx.ForceExportError):
        fx.ForceInputs.load(tmp_path / "missing.json")


def test_load_checks_mapping_and_packets(tmp_path, gm, measured):
    session, _ = measured
    g = gm.get("A1_PZT1")
    forces = fx.ForceInputs.new(gm, session.settings, simulated=True)
    forces.add_package(g, fx.results_for_group(session.results, g), session.settings, gm, simulated=True)
    path = forces.save(tmp_path / "f.json")
    changed = json.loads(sensor_groups.default_library_path().read_text(encoding="utf-8"))
    next(c for c in changed["configurations"]
         if c.get("board_profile") == PROFILE)["reverse_polarity"] = True
    with pytest.raises(fx.ForceExportError, match="layout differs"):
        fx.ForceInputs.load(path, sensor_groups.resolve(PROFILE, library_data=changed))
    data = json.loads(path.read_text(encoding="utf-8"))
    pid = next(iter(data["raw_packets"]))
    data["raw_packets"][pid] = data["raw_packets"][pid][:-8] + "AAAAAAAA"
    data.pop("content_sha256")
    broken = fx.ForceInputs(data)
    data["content_sha256"] = broken.content_hash()
    (tmp_path / "p.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(fx.ForceExportError, match="does not match its hash"):
        fx.ForceInputs.load(tmp_path / "p.json")


def test_raw_evidence_is_replayable(gm, measured):
    session, _ = measured
    g = gm.get("A1_PZT1")
    forces = fx.ForceInputs.new(gm, session.settings, simulated=True)
    entry = forces.add_package(g, fx.results_for_group(session.results, g), session.settings, gm,
                               simulated=True)
    route = entry["routes"][0]
    burst = route["per_burst"][0]
    packet = forces.raw_packet(burst["raw_packet_id"])
    from captest.boards import load_driver
    driver = load_driver(PROFILE)
    m = driver.analyze(driver.decode(packet), [(2, 0)], session.settings, {(2, 0): None})[(2, 0)]
    assert m.c_total == pytest.approx(burst["c_total_f"], rel=1e-9)
    assert m.c_share_ratio == pytest.approx(burst["c0"], rel=1e-9)


def test_legacy_raw_file_is_reanalysed_into_a_manifest(tmp_path, gm, measured):
    session, _ = measured
    raw_path = tmp_path / "legacy_raw.json"
    session.save_raw(raw_path)
    g = gm.get("A1_PZT6")
    forces = fx.from_raw_file(raw_path, gm, g)
    assert forces.status("A1_PZT6") == fx.COMPLETE
    assert forces.data["provenance"]["source_hashes"]
    again = fx.from_raw_file(raw_path, gm, gm.get("A1_PZT1"), forces)
    assert sorted(again.ready_groups()) == ["A1_PZT1", "A1_PZT6"]
    # a result-only file is not usable: it has no packets
    res_path = tmp_path / "results.json"
    session.save_results(res_path)
    with pytest.raises(Exception):
        fx.from_raw_file(res_path, gm, g)


def test_d33_eff_embedding_and_csv(tmp_path, gm, measured):
    session, _ = measured
    g = gm.get("A1_PZT1")
    forces = fx.ForceInputs.new(gm, session.settings, simulated=True)
    forces.add_package(g, fx.results_for_group(session.results, g), session.settings, gm, simulated=True)
    forces.set_d33_eff("A1_PZT1", 7.5e-9, {"fit": "test"})
    entry = forces.data["force_inputs"]["d33_eff_c_per_n"]["A1_PZT1"]
    assert entry["charge_conversion_model"] == force_model.CHARGE_MODEL
    with pytest.raises(fx.ForceExportError):
        forces.set_d33_eff("A1_PZT1", 0.0, {})
    with pytest.raises(fx.ForceExportError):
        forces.set_d33_eff("PZT1", 1e-9, {})
    csv_path = forces.write_csv(tmp_path / "f.csv")
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6 and lines[0].startswith("group,revision")
    path = forces.save(tmp_path / "f.json")
    assert fx.ForceInputs.load(path).data["force_inputs"]["d33_eff_c_per_n"]["A1_PZT1"]["value"] == 7.5e-9


def test_acquisition_signature_tracks_timing_settings():
    base = Settings()
    sig = fx.acquisition_signature(base)
    other = Settings.from_dict(json.loads(base.to_json()))
    other.gap_ns = 300
    assert fx.acquisition_signature(other) != sig
    assert "gap_ns=200" in sig and "vmid_frames=1" in sig


def test_electrical_hash_ignores_sensitivity_and_bookkeeping_but_not_remeasurement(gm, measured):
    session, _ = measured
    g = gm.get("A1_PZT1")
    forces = fx.ForceInputs.new(gm, session.settings, simulated=True)
    forces.add_package(g, fx.results_for_group(session.results, g), session.settings, gm, simulated=True)
    before = forces.electrical_hash()
    forces.set_d33_eff("A1_PZT1", 5e-9, {"record": "x"})
    assert forces.electrical_hash() == before and forces.content_hash() != ""
    forces.add_package(g, fx.results_for_group(session.results, g), session.settings, gm, simulated=True)
    assert forces.electrical_hash() != before
