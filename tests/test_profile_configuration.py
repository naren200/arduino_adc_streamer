"""A changed identity, commands and status transcript need only JSON changes."""
import copy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from config.adc_configuration_service import ADCConfigurationService
from config.boards import BoardRegistry
from config.boards.sensors import default_sensor_layout
from config.boards import get_board_registry
from config.boards.validation import validate_profile
from serial_communication.adc_session import ADCSessionController


def rename_parameter(value, old, new):
    if isinstance(value, dict):
        return {key: new if key == 'parameter' and item == old else rename_parameter(item, old, new)
                for key, item in value.items()}
    if isinstance(value, list):
        return [rename_parameter(item, old, new) for item in value]
    return value


@pytest.fixture
def custom_board(tmp_path, request):
    root = tmp_path / 'boards'
    shutil.copytree('config/boards', root, ignore=shutil.ignore_patterns('__pycache__', '*.py'))
    profile = json.loads((root / 'profiles/testboard_adc124.json').read_text())
    profile.update(id='custom_routed', mcu_aliases=['CustomRoutedMCU'])
    mode = profile['modes']['PZT']
    params = mode['parameters']
    params['sensor_delay'] = params.pop('settle_time_us')
    params['sensor_delay'].update(default=9, maximum=60, command='dwell')
    mode['configuration'] = rename_parameter(mode['configuration'], 'settle_time_us', 'sensor_delay')
    mode['configuration']['acknowledgement'] = getattr(request, 'param', 'echo')
    params['reference'].update(default='external', choices=[dict(id='external', label='4.096 V', wire_value='ext', full_scale_volts=4.096)])
    params['spi_engine']['default'] = 'lpspi'
    params['spi_frame']['default'] = 'single'
    params['guard_cycles'] = dict(type='integer', default=17, minimum=0, maximum=50,
                                label='Guard cycles', section='acquisition', policy='editable', command='guard')
    for step in mode['configuration']['commands']:
        if step.get('command') == 'mode':
            step['command'] = 'streamkind'
        if step.get('command') == 'adcchannels':
            step['command'] = 'routes'
    status = mode['configuration']['status']
    status.update(command='inspect', start={'prefix': '# BEGIN CUSTOM'}, end={'prefix': '# DONE CUSTOM'})
    fields = status['fields']
    fields['board']['expected'] = {'literal': 'CustomRoutedMCU'}
    fields['dwell_reported'] = fields.pop('mux_settle_us')
    fields['guard_readback'] = dict(type='integer', target='parameters.guard_cycles', expected={'parameter': 'guard_cycles'})
    (root / 'profiles/custom_routed.json').write_text(json.dumps(profile), encoding='utf-8')
    index = json.loads((root / 'registry.json').read_text())
    index['profiles'].append('profiles/custom_routed.json')
    (root / 'registry.json').write_text(json.dumps(index), encoding='utf-8')
    context = BoardRegistry(root).context('CustomRoutedMCU')
    layout = default_sensor_layout(get_board_registry().resolve('TestBoard_ADC124'))
    layout.update(name='Custom layout', board_profile='custom_routed')
    parameters = context.mode.defaults()
    parameters.update(array_selection='2', vmid_sampling=True, settling_conversions=3,
                      vmid_settle_follow_sensor=False, vmid_settle_time_us=0)
    request = SimpleNamespace(current_mcu='CustomRoutedMCU', board_context=context, parameters=parameters,
                              sensor_configuration=layout, buffer_size=1, channels_to_send=[5, 13, 21, 29, 30],
                              testboard_adc_routes=[(2, ch) for ch in [5, 13, 21, 29, 30]], array_operation_mode='PZT')
    return profile, context, request


def transcript():
    return ['# irrelevant text', '# BEGIN CUSTOM', '# board=CustomRoutedMCU', '# protocol_version=3',
            '# array=2', '# scanorder=adc', '# spi_frame=single', '# spiengine=lpspi', '# spi_clock_hz=16000000',
            '# channelrepeat=3', '# dwell_reported=9', '# vmid_settle_us=0', '# vmid_settle_mode=explicit',
            '# vmid_between_groups=true', '# vmid_address=7', '# vref=ext', '# guard_readback=17',
            '# adcchannels=2:1:5,2:2:5,2:3:5,2:4:5,2:4:6',
            '# payload_routes=2:1:5,2:2:5,2:3:5,2:4:5,2:4:6', '# route_count=5', '# DONE CUSTOM']


def status_reader(context, events, lines):
    session = ADCSessionController(None, None, None)
    session.board_context = context
    session.send_command = lambda command: events.append(command)
    def wait(predicate, timeout, *, consume, send_action):
        send_action()
        return any(predicate(line) for line in lines)
    session.wait_for_line = wait
    return session.read_profile_status


@pytest.mark.parametrize('custom_board', ['bare', 'echo'], indirect=True)
def test_json_only_identity_commands_status_scaling_and_new_parameter(custom_board):
    _, context, request = custom_board
    events, expectations = [], []
    def send(command, expected):
        events.append(command)
        expectations.append(expected)
        return True, expected
    service = ADCConfigurationService(send, status_reader(context, events, transcript()))
    result = service.send_config_with_verification(request)
    assert result.success, result.messages
    assert 'streamkind PZT' in events and 'ref ext' in events and 'dwell 9' in events and 'guard 17' in events
    assert 'routes 2:1:5,2:2:5,2:3:5,2:4:5,2:4:6' in events
    assert events[-1] == 'inspect'
    assert len(events) == len(set(events))
    echo = context.mode.definition['configuration']['acknowledgement'] == 'echo'
    assert all((value is not None) == echo for value in expectations)
    assert result.arduino_status.parameters['guard_cycles'] == 17
    assert result.arduino_status.reference == 'external'
    assert context.mode.full_scale_volts() == 4.096


@pytest.mark.parametrize('bad_line', ['# guard_readback=18', '# guard_readback=bad'])
def test_new_parameter_is_verified_after_all_commands(custom_board, bad_line):
    _, context, request = custom_board
    events = []
    lines = [bad_line if line.startswith('# guard_readback=') else line for line in transcript()]
    service = ADCConfigurationService(lambda command, expected: (events.append(command) or True, expected),
                                      status_reader(context, events, lines))
    result = service.send_config_with_verification(request)
    assert not result.success
    assert any('guard_readback' in message for message in result.messages)
    assert events[-1] == 'inspect'


def test_legacy_request_fields_receive_json_scalar_extensions(custom_board):
    _, context, request = custom_board
    for key, value in request.parameters.items():
        setattr(request, context.mode.parameters[key].legacy_key, value)
    request.parameters = None
    events = []
    service = ADCConfigurationService(lambda command, expected: (events.append(command) or True, expected),
                                      status_reader(context, events, transcript()))
    result = service.send_config_with_verification(request)
    assert result.success, result.messages
    assert events.count('guard 17') == 1 and events[-1] == 'inspect'


@pytest.mark.parametrize('edit', [
    lambda d: d['commands'].append(copy.deepcopy(d['commands'][0])),
    lambda d: d['commands'].append({'parameter': 'missing'}),
    lambda d: d['commands'][0].update(value={'source': 'python_expression'}),
    lambda d: d['commands'][0].update(value={'literal': 'PZT', 'exec': 'bad'}),
    lambda d: d.update(acknowledgement='unknown'),
    lambda d: d['status']['start'].update(prefix=''),
    lambda d: d['status']['fields']['guard_readback'].update(target='__dict__'),
    lambda d: d['status']['fields']['guard_readback'].update(expected={'parameter': 'missing'}),
    lambda d: d['status']['fields']['guard_readback'].update(type='python'),
    lambda d: d['status']['fields']['guard_readback'].update(comparison='canonical_routes'),
    lambda d: d['commands'][-1]['value']['when'].update(equals='bad'),
])
def test_invalid_declarative_contracts_fail_startup(custom_board, edit):
    profile, _, _ = custom_board
    edit(profile['modes']['PZT']['configuration'])
    with pytest.raises(ValueError):
        validate_profile(profile)


def test_json_schema_matches_installed_command_contracts():
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads(Path('config/boards/schema.json').read_text())
    for path in Path('config/boards/profiles').glob('*.json'):
        jsonschema.validate(json.loads(path.read_text()), schema)


@pytest.mark.parametrize('value, expected', [('true', True), ('false', False)])
def test_boolean_parameter_status_is_typed(value, expected):
    from serial_communication.profile_status import decode_parameter
    parameter = get_board_registry().context('TestBoard_ADC124').mode.parameters['vmid_sampling']
    assert decode_parameter(value, parameter) is expected
    with pytest.raises(ValueError):
        decode_parameter('yes', parameter)


def test_numeric_enum_status_uses_declared_wire_tokens():
    from serial_communication.profile_status import decode_parameter
    parameter = get_board_registry().context('MG24').mode.parameters['osr']
    assert decode_parameter('4', parameter) == 4
    with pytest.raises(ValueError):
        decode_parameter('unknown', parameter)
