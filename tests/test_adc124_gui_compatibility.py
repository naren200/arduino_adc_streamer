"""Host compatibility with the current ADC124 protocol and physical wiring."""
import copy
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from config.boards import get_board_registry
from config.boards.sensors import default_sensor_layout
from config.boards.models import freeze, thaw, ModeProfile, Parameter
from config.array_acquisition import build_descriptor, descriptor_groups, validate_descriptor
from config.array_scan import format_array_routes
from config.sensor_config import normalize_combined_sensor_config
from serial_communication.adc_connection_state import ArduinoStatus
from serial_communication.testboard_status import apply_testboard_status_line


def context():
    return get_board_registry().context('TestBoard_ADC124')


def layout():
    return default_sensor_layout(context().profile)


def context_with_defaults(**defaults):
    ctx = context()
    definition = thaw(ctx.mode.definition)
    for key, value in defaults.items():
        definition['parameters'][key]['default'] = value
    parameters = freeze({k: Parameter(k, freeze(v)) for k, v in definition['parameters'].items()})
    return replace(ctx, mode=ModeProfile('PZT', freeze(definition), parameters))


@pytest.mark.parametrize('selection', ['1', '2', 'both'])
@pytest.mark.parametrize('order', ['adc', 'mux'])
@pytest.mark.parametrize('sensors', [['PZT7'], ['PZT6', 'PZT1'], ['PZT1', 'PZT3', 'PZT5', 'PZT6', 'PZT7']])
def test_exact_wire_order_and_sensor_identity(selection, order, sensors):
    desc = build_descriptor('TestBoard_ADC124', dict(array_selection=selection, scan_order=order,
                            selected_array_sensors=sensors), layout())
    manifest = json.loads(Path('Arduino_Sketches/TestBoard_ADC124/benchmarks/testboard_adc124_routes.json').read_text())
    all_routes = [tuple(map(int, r.split(':'))) for r in manifest['route_sets']['both_full']]
    expected_inputs = {'PZT1': [(1, i) for i in range(5)], 'PZT3': [(2, i) for i in range(5)],
                       'PZT5': [(3, i) for i in range(5)], 'PZT6': [(4, i) for i in range(5)],
                       'PZT7': [(1, 5), (2, 5), (3, 5), (4, 5), (4, 6)]}
    expected = [r for r in all_routes if (selection == 'both' or r[0] == int(selection))
                and any(r[1:] in expected_inputs[s] for s in sensors)]
    expected.sort(key=(lambda r: (r[0], r[2], r[1])) if order == 'adc' else (lambda r: (r[2], r[0], r[1])))
    assert format_array_routes(desc['ordered_routes'], layout()) == ','.join(':'.join(map(str, r)) for r in expected)
    assert desc['full_scale_volts'] == 3.3 and desc['adc_resolution_bits'] == 12
    assert len(desc['ordered_routes']) == len(sensors) * 5 * (2 if selection == 'both' else 1)
    for spec in desc['channel_specs']:
        index = int(spec['placement'].removeprefix('CH')) - 1
        assert expected[spec['sample_indices'][0]] == (spec['array_number'], *expected_inputs[spec['sensor_id']][index])
    assert len(descriptor_groups(desc)) == len(sensors) * (2 if selection == 'both' else 1)
    validate_descriptor(json.loads(json.dumps(desc)), len(expected))


def test_reserved_inputs_and_layout_encoding_are_validated():
    assert normalize_combined_sensor_config(layout()) is not None
    for channel in [6, 7, 14, 15, 22, 23, 31, 32]:
        bad = layout()
        bad['mux_mapping']['PZT7']['channels'][-1] = channel
        assert normalize_combined_sensor_config(bad) is None
    bad = layout()
    bad.pop('route_encoding')
    assert normalize_combined_sensor_config(bad) is None


@pytest.fixture
def gui(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    app = QApplication.instance() or QApplication([])
    from adc_gui import ADCStreamerGUI
    monkeypatch.setattr(ADCStreamerGUI, '_pzt_decay_qsettings',
                        lambda self: QSettings(str(tmp_path / 'decay.ini'), QSettings.Format.IniFormat))
    monkeypatch.setattr(ADCStreamerGUI, '_pressure_map_workspace_qsettings',
                        lambda self: QSettings(str(tmp_path / 'pressure.ini'), QSettings.Format.IniFormat))
    with patch('adc_gui.load_device_config', return_value={'auto_connect': False}):
        window = ADCStreamerGUI()
    window.current_mcu = 'TestBoard_ADC124'
    window.update_gui_for_mcu()
    window.pzt_sequence_input.setText('1,3,5,6,7')
    yield window
    window.is_capturing = False
    window.close()
    app.processEvents()


def status_for(request):
    from config.profile_configuration import request_settings
    ctx, resolved = request_settings(request)
    values = resolved.requested
    status = ArduinoStatus()
    delay = values['settle_time_us'] if values['vmid_settle_follow_sensor'] else values['vmid_settle_time_us']
    lines = dict(board='TestBoard_ADC124', protocol_version=3, array=values['array_selection'],
                 scanorder=values['scan_order'], spi_frame=values['spi_frame'], spiengine=values['spi_engine'],
                 spi_clock_hz=values['spi_clock_hz'], channelrepeat=values['settling_conversions'],
                 mux_settle_us=values['settle_time_us'], vmid_settle_us=delay,
                 vmid_settle_mode='follow_sensor' if values['vmid_settle_follow_sensor'] else 'explicit',
                 vmid_between_groups=str(values['vmid_sampling']).lower(), vmid_address=7, vref='3.3',
                 adcchannels=format_array_routes(request.testboard_adc_routes, layout()),
                 payload_routes=format_array_routes(gui_order(request), layout()), route_count=len(request.testboard_adc_routes))
    for key, value in lines.items():
        assert apply_testboard_status_line(status, f'# {key}={value}', ctx)
    return status


def gui_order(request):
    from config.array_scan import order_array_routes
    return order_array_routes(request.testboard_adc_routes, request.testboard_scan_order, layout())


def test_gui_controls_rules_and_board_transition(gui):
    assert gui.active_sensor_config_name == 'TestBoard_ADC124'
    assert gui.vref_combo.isHidden() and gui.board_sequence_combo.isHidden()
    assert gui.buffer_spin.isHidden() and gui.ground_pin_spin.isHidden()
    assert gui.board_spi_clock_spin.minimum() == 8 and gui.board_spi_clock_spin.maximum() == 16
    assert gui.repeat_spin.maximum() == 3
    settle = gui._board_extra_controls['settle_time_us'][0]
    follow = gui._board_extra_controls['vmid_settle_follow_sensor'][0]
    vmid = gui._board_extra_controls['vmid_settle_time_us'][0]
    assert settle.value() == 5 and not follow.isChecked() and not vmid.isEnabled()
    gui.use_ground_check.setChecked(True)
    assert vmid.isEnabled()
    vmid.setValue(0)
    settle.setValue(20)
    request = gui._build_adc_configuration_request()
    assert request.parameters['vmid_settle_time_us'] == 0 and request.parameters['settle_time_us'] == 20
    assert len(request.testboard_adc_routes) == 50
    gui.set_controls_enabled(False)
    assert not settle.isEnabled() and not vmid.isEnabled()
    gui.set_controls_enabled(True)
    gui.current_mcu = 'TestBoard_7953'
    gui.update_gui_for_mcu()
    assert settle.isHidden() and vmid.isHidden()
    assert not gui.board_sequence_combo.isHidden() and gui.board_spi_clock_spin.value() == 20


@pytest.mark.parametrize('vmid_delay', [None, 0, 1, 1000])
def test_configure_bare_acks_and_status_verification(gui, vmid_delay):
    from config.adc_configuration_service import ADCConfigurationService
    gui._board_extra_controls['settle_time_us'][0].setValue(10)
    gui.use_ground_check.setChecked(True)
    gui._board_extra_controls['vmid_settle_follow_sensor'][0].setChecked(vmid_delay is None)
    if vmid_delay is not None:
        gui._board_extra_controls['vmid_settle_time_us'][0].setValue(vmid_delay)
    request = gui._build_adc_configuration_request()
    commands = []
    service = ADCConfigurationService(lambda cmd, expected: (commands.append(cmd) or True, None), lambda: status_for(request))
    result = service.send_config_with_verification(request)
    assert result.success, result.messages
    assert f'vmidsettle {"default" if vmid_delay is None else vmid_delay}' in commands
    assert 'muxsettle 10' in commands and 'ref 3.3' in commands
    assert 'spiframe burst' in commands and 'spiengine blocking' in commands and 'vmid true' in commands
    assert not any(cmd.split()[0] in ('adcseq', 'repeat', 'buffer', 'osr', 'gain') for cmd in commands)
    assert len(commands) == len(set(commands))
    bad = status_for(request)
    bad.parameters['payload_routes'] = ','.join(reversed(bad.parameters['payload_routes'].split(',')))
    service._read_testboard_status = lambda: bad
    assert not service.send_config_with_verification(request).success
    bad = status_for(request)
    bad.parameters['vmid_settle_us'] += 1
    service._read_testboard_status = lambda: bad
    assert not service.send_config_with_verification(request).success


def test_vmid_checkbox_enables_delay_and_preserves_explicit_value(gui):
    from config.adc_configuration_service import ADCConfigurationService
    delay = gui._board_extra_controls['vmid_settle_time_us'][0]
    follow = gui._board_extra_controls['vmid_settle_follow_sensor'][0]
    assert not gui.use_ground_check.isChecked() and not delay.isEnabled()
    gui.use_ground_check.setChecked(True)
    assert delay.isEnabled() and not follow.isChecked()
    delay.setValue(1)
    gui._board_extra_controls['settle_time_us'][0].setValue(20)
    request = gui._build_adc_configuration_request()
    commands = []
    service = ADCConfigurationService(lambda cmd, _: (commands.append(cmd) or True, None), lambda: status_for(request))
    result = service.send_config_with_verification(request)
    assert result.success, result.messages
    assert 'vmid true' in commands and 'vmidsettle 1' in commands and 'muxsettle 20' in commands
    assert request.parameters['vmid_settle_time_us'] == 1
    gui.use_ground_check.setChecked(False)
    assert not delay.isEnabled()
    gui.use_ground_check.setChecked(True)
    assert delay.isEnabled() and delay.value() == 1
    follow.setChecked(True)
    assert not delay.isEnabled()
    follow.setChecked(False)
    assert delay.isEnabled() and delay.value() == 1


def test_capture_export_and_offline_reload_keep_channel_identity(gui, tmp_path):
    from data_processing.analysis_workbench import load_exported_csv_snapshot, prepare_analysis_data
    gui._build_adc_configuration_request()
    gui.freeze_array_capture_descriptor()
    gui.raw_data_buffer = np.tile(np.arange(50, dtype=np.float32), (2, 1))
    gui.sweep_timestamps_buffer = np.array([0, .01])
    gui.samples_per_sweep = 50
    gui.sweep_count = gui.buffer_write_index = 2
    descriptor = copy.deepcopy(gui.array_capture_descriptor)
    gui.display_array_combo.setCurrentText('2')
    assert len(gui.get_display_channel_specs()) == 25
    assert len(gui.get_acquisition_channel_specs()) == 50
    gui.dir_input.setText(str(tmp_path))
    gui.filename_input.setText('adc124')
    with patch('file_operations.data_exporter.QMessageBox.information'), patch('file_operations.data_exporter.QMessageBox.warning') as warning:
        gui.save_data()
        warning.assert_not_called()
    snapshot = load_exported_csv_snapshot(next(tmp_path.glob('adc124_*.csv')), next(tmp_path.glob('adc124_*_metadata.json')))
    assert snapshot.data.shape == (2, 50)
    validate_descriptor(json.loads(json.dumps(descriptor)), 50)
    assert len(prepare_analysis_data(snapshot).traces) == 50


def test_session_reads_adc124_status_until_final_ack():
    from serial_communication.adc_session import ADCSessionController
    session = ADCSessionController(None, None, None)
    session.board_context = context()
    def wait(predicate, timeout, **kwargs):
        for line in ['# board=TestBoard_ADC124', '# protocol_version=3', '# mux_settle_us=5', '#OK']:
            done = predicate(line)
        return done
    session.wait_for_line = wait
    status = session.read_testboard_status()
    assert status.parameters['protocol_version'] == 3 and status.parameters['mux_settle_us'] == 5


def test_binary_capture_pipeline_archives_both_arrays(gui, tmp_path):
    from scripts.benchmark_testboard_gui import ReplayPort
    from serial_communication.serial_threads import SerialReaderThread
    from data_processing.archive_writer import ArchiveWriterThread
    from config.boards.capture import freeze_owner_context
    gui._build_adc_configuration_request()
    gui.freeze_array_capture_descriptor()
    freeze_owner_context(gui)
    descriptor = copy.deepcopy(gui.array_capture_descriptor)
    port = ReplayPort(50, 1000, .25)
    reader = SerialReaderThread(port)
    path = tmp_path / 'adc124.jsonl'
    writer = ArchiveWriterThread(str(path), {'metadata': {'testboard_acquisition': descriptor}})
    gui.serial_thread = reader
    gui._archive_writer = writer
    gui._block_timing_file = None
    gui._capture_generation = 1
    writer.start()
    try:
        gui._start_acquisition_pipeline()
        reader.set_capturing(True, 50)
        reader.binary_buffer.extend(port.data)
        reader.process_binary_data(reader.binary_buffer)
        reader.set_capturing(False)
        worker = gui._acquisition_worker
        worker.stop_nowait()
        worker.join(5)
        gui._acquisition_render_timer.stop()
        assert worker.error is None and worker.count == 250
        np.testing.assert_allclose(worker.times[:250], np.arange(250) / 1000)
        assert reader._rejected_packets_total == 0
    finally:
        if gui._acquisition_worker.is_alive():
            gui._acquisition_worker.stop_nowait()
            gui._acquisition_worker.join(5)
        writer.stop()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 251
    assert len(records[1]['samples']) == 50
    validate_descriptor(descriptor, 50)


def test_configure_rejection_and_missing_status_prevent_start(gui):
    from config.adc_configuration_service import ADCConfigurationService
    request = gui._build_adc_configuration_request()
    calls = []
    def send(command, _):
        calls.append(command)
        return command != 'muxsettle 5', None
    service = ADCConfigurationService(send, lambda: pytest.fail('Do not read status after a rejected command'))
    assert not service.send_config_with_verification(request).success
    assert calls[-1] == 'muxsettle 5'
    service = ADCConfigurationService(lambda *_: (True, None), lambda: None)
    assert not service.send_config_with_verification(request).success


def test_sensor_save_keeps_numbered_channels_and_electrical_routes(gui):
    assert not gui.sensor_editor_tabs.isTabVisible(0)
    assert gui.sensor_editor_tabs.currentIndex() == 1
    before = gui.get_active_sensor_configuration()
    normalized = gui._build_normalized_sensor_config_from_editor()
    for key in ('channel_sensor_map', 'mux_mapping', 'route_encoding', 'arrays'):
        assert normalized[key] == before[key]


@pytest.mark.parametrize('order', ['adc', 'mux'])
@pytest.mark.parametrize('engine', ['blocking', 'dma', 'lpspi'])
@pytest.mark.parametrize('frame', ['burst', 'single'])
def test_gui_scan_order_and_profile_engine_framing_configure(gui, order, engine, frame):
    from config.adc_configuration_service import ADCConfigurationService
    gui.array_scan_order_combo.setCurrentIndex(gui.array_scan_order_combo.findData(order))
    request = gui._build_adc_configuration_request()
    request.board_context = context_with_defaults(spi_engine=engine, spi_frame=frame)
    commands = []
    service = ADCConfigurationService(lambda cmd, _: (commands.append(cmd) or True, None), lambda: status_for(request))
    result = service.send_config_with_verification(request)
    assert result.success, result.messages
    assert f'scanorder {order}' in commands and f'spiengine {engine}' in commands and f'spiframe {frame}' in commands


@pytest.mark.parametrize('order', ['mux', 'adc'])
def test_scan_order_selector_invalidates_configuration_and_freezes_selected_order(gui, order):
    combo = gui.array_scan_order_combo
    assert not combo.isHidden() and combo.isEnabled()
    assert [combo.itemData(i) for i in range(combo.count())] == ['adc', 'mux']
    combo.setCurrentIndex(combo.findData('mux' if order == 'adc' else 'adc'))
    gui.config_is_valid = True
    combo.setCurrentIndex(combo.findData(order))
    assert not gui.config_is_valid
    request = gui._build_adc_configuration_request()
    assert request.parameters['scan_order'] == request.testboard_scan_order == order
    gui.freeze_array_capture_descriptor()
    captured = copy.deepcopy(gui.array_capture_descriptor)
    assert captured['scan_order'] == order
    assert captured['board_context']['requested']['scan_order'] == order
    assert captured['ordered_routes'] == [list(route) for route in gui_order(request)]
    gui.is_capturing = True
    gui.set_controls_enabled(False)
    assert not combo.isEnabled()
    gui.is_capturing = False
    gui.set_controls_enabled(True)
    assert combo.isEnabled()
    combo.setCurrentIndex(combo.findData('mux' if order == 'adc' else 'adc'))
    assert gui.array_capture_descriptor == captured


@pytest.mark.parametrize('wire_routes', ['', '1:1:7', '1:3:6', '1:5:0', '3:1:0', '1:1:0,1:1:0', '1:0', '1:1:-1'])
def test_invalid_reported_routes_are_rejected(wire_routes):
    status = ArduinoStatus()
    assert not apply_testboard_status_line(status, f'# adcchannels={wire_routes}', context())
    assert status.testboard_routes is None
