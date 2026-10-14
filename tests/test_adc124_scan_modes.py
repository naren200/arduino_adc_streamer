import pytest
from Arduino_Sketches.TestBoard_ADC124.benchmarks import testboard_adc124_benchmark as b
from Arduino_Sketches.TestBoard_ADC124.benchmarks.scan_modes import operation_counts, acquisition_phases


@pytest.mark.parametrize("mode,vmid,waits", [("adc", False, 14), ("adc", True, 28), ("mux", False, 7), ("mux", True, 14)])
def test_full_operation_counts_and_estimated_cs_savings(mode, vmid, waits):
    routes = b.load_manifest(b.DEFAULT_MANIFEST)["both_full"]
    c = operation_counts(routes, mode, vmid=vmid)
    assert c["sensor_batches"] == 14 and c["settling_waits"] == waits
    assert c["spi_words"] == (134 if vmid else 64)
    assert c["cs_frames"] == (28 if vmid else 14)
    assert c["removed_cs_allowance_us"] == pytest.approx(21.2 if vmid else 10)
    assert c["bank_switches"] == (28 if vmid else 14)
    assert operation_counts(routes, mode, vmid=vmid, spi_frame="single")["removed_cs_allowance_us"] == 0


@pytest.mark.parametrize("mode", ["adc", "mux"])
def test_partial_and_single_address_selections_skip_redundant_switches(mode):
    routes = tuple(b.Route.parse(s) for s in ("1:3:0", "1:1:0", "2:4:0"))
    c = operation_counts(routes, mode)
    assert c["spi_words"] == 5
    assert c["bank_switches"] == c["settling_waits"] == 0
    assert c["cs_frames"] == 2
    single = (b.Route(2, 4, 6),)
    assert operation_counts(single, mode)["settling_waits"] == 0
    assert len(acquisition_phases(single, mode)) == 1


def test_protocol_version_disambiguates_old_and_new_mux_payloads():
    routes = tuple(b.Route.parse(s) for s in ("1:1:1", "2:1:0", "1:1:0", "2:4:6"))
    assert b.payload_order(routes, "mux", 2) == b.payload_order(routes, "adc", 3)
    assert b.payload_order(routes, "mux", 3) != b.payload_order(routes, "mux", 2)
    assert b.payload_order(routes, "mux", 3)[1] == b.Route(2, 1, 0)


def test_legacy_configuration_requires_explicit_single_frame_adc_mode():
    routes = (b.Route(1, 1, 0),)
    config = b.BenchmarkConfig("1", "mux", "blocking", 16000000, 1, 5, False)
    with pytest.raises(b.BenchmarkError, match="Legacy firmware"):
        b.validate_status({"protocol_version": "2"}, config, routes)


@pytest.mark.parametrize("version", ["1", "2"])
def test_legacy_sequential_adc_request_preserves_old_mux_labels(version):
    from test_testboard_adc124_benchmark import status_for
    routes = (b.Route(1, 3, 0), b.Route(2, 4, 6), b.Route(1, 1, 0))
    config = b.BenchmarkConfig("both", "adc", "blocking", 16000000, 1, 5, False, "single")
    status = status_for(config, routes)
    status.update(protocol_version=version, scanorder="mux")
    b.validate_status(status, config, routes)


def test_old_mode_names_are_rejected_by_cli():
    for name in ("channel", "interleaved", "array"):
        with pytest.raises(SystemExit):
            b.parse_args(["--dry-run", "--scan-order", name])
