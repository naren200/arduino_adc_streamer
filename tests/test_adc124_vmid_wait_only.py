"""Wait-only VMID removes ADC traffic while preserving switching and timing."""
import pytest
from Arduino_Sketches.TestBoard_ADC124.benchmarks import testboard_adc124_benchmark as b
from Arduino_Sketches.TestBoard_ADC124.benchmarks.scan_modes import operation_counts


@pytest.mark.parametrize('mode', ['adc', 'mux'])
@pytest.mark.parametrize('repeat', [1, 2, 3])
@pytest.mark.parametrize('frame', ['burst', 'single'])
@pytest.mark.parametrize('selection', ['full', 'single', 'sparse'])
def test_wait_only_preserves_mux_visits_and_removes_all_vmid_adc_words(mode, repeat, frame, selection):
    if selection == 'full':
        routes = b.load_manifest(b.DEFAULT_MANIFEST)['both_full']
    elif selection == 'single':
        routes = (b.Route(2, 4, 6),)
    else:
        routes = tuple(b.Route.parse(s) for s in ('1:3:0', '1:1:0', '2:4:6', '2:2:5'))
    options = dict(mode=mode, repeat=repeat, spi_frame=frame, coordinated=True, lookahead=True)
    old = operation_counts(routes, vmid=True, **options)
    new = operation_counts(routes, vmid=True, vmid_wait_only=True, **options)
    off = operation_counts(routes, vmid=False, vmid_wait_only=True, **options)
    assert new['spi_words'] == off['spi_words'] == len(routes) * repeat
    assert new['cs_frames'] == off['cs_frames']
    assert new['vmid_adc_words'] == new['vmid_adc_batches'] == 0
    assert old['spi_words'] - new['spi_words'] == 4 * new['sensor_batches']
    for key in ('bank_switches', 'settling_waits', 'enable_writes', 'sensor_settling_windows',
                'vmid_settling_windows', 'mux_switch_guard_us', 'grouped_address_register_writes'):
        assert new[key] == old[key]


def test_gui_retains_wait_only_policy_without_changing_vmid_configuration():
    from config.boards import get_board_registry
    from serial_communication.adc_connection_state import ArduinoStatus
    from serial_communication.testboard_status import apply_testboard_status_line
    status = ArduinoStatus()
    ctx = get_board_registry().context('TestBoard_ADC124')
    for line in ('# firmware_revision=adc124-vmid-wait-only-v1', '# vmid_scan_policy=wait_only',
                 '# vmid_between_groups=true', '# vmid_settle_us=0', '# vmid_settle_mode=explicit'):
        assert apply_testboard_status_line(status, line, ctx)
    assert status.parameters['vmid_scan_policy'] == 'wait_only'
    assert status.parameters['vmid_settle_us'] == 0
    assert status.testboard_vmid is True
