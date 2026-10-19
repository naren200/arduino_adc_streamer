"""Authoring checks for declarative command and status contracts."""
from dataclasses import fields
import re

from serial_communication.adc_connection_state import ArduinoStatus


VALUE_SOURCES = {'mode', 'configured_routes', 'payload_routes', 'route_count', 'vmid_channel'}
STATUS_TYPES = {'text', 'integer', 'boolean', 'parameter', 'routes'}
STATUS_TARGETS = {field.name for field in fields(ArduinoStatus)} - {'parameters', 'stream_diagnostics'}


def object_keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError('Invalid command/status contract fields')


def command_name(value):
    if not isinstance(value, str) or not re.fullmatch('[a-z][a-z0-9]*', value):
        raise ValueError('Invalid command name')


def status_target(target):
    if not isinstance(target, str):
        raise ValueError('Invalid status destination')
    if target in STATUS_TARGETS:
        return
    if re.fullmatch(r'(parameters|stream_diagnostics)\.[a-z][a-z0-9_]*', target):
        return
    raise ValueError(f'Unknown status destination: {target}')


def validate_expression(value, parameters, depth=0):
    if depth > 8 or not isinstance(value, dict):
        raise ValueError('Invalid configuration value expression')
    if 'literal' in value:
        object_keys(value, ['literal'], ['literal'])
        if type(value['literal']) not in (str, int, float, bool):
            raise ValueError('Invalid configuration literal')
    elif 'parameter' in value:
        object_keys(value, ['parameter', 'setting', 'wire'], ['parameter'])
        if value['parameter'] not in parameters or value.get('setting', 'requested') not in ('requested', 'effective'):
            raise ValueError('Unknown parameter/setting in configuration expression')
        if type(value.get('wire', False)) is not bool:
            raise ValueError('Invalid wire encoding flag')
    elif 'source' in value:
        object_keys(value, ['source'], ['source'])
        if value['source'] not in VALUE_SOURCES:
            raise ValueError('Unknown configuration value source')
    else:
        object_keys(value, ['when', 'then', 'else'], ['when', 'then', 'else'])
        predicate = value['when']
        object_keys(predicate, ['parameter', 'equals'], ['parameter', 'equals'])
        parameters[predicate['parameter']].normalize(predicate['equals'])
        validate_expression(value['then'], parameters, depth + 1)
        validate_expression(value['else'], parameters, depth + 1)


def validate_configuration(mode, parameters):
    config = mode.get('configuration')
    if config is None:
        if mode['adapters']['protocol'] == 'profile_commands':
            raise ValueError('Declarative protocol requires a configuration contract')
        return
    if mode['adapters']['protocol'] != 'profile_commands' or mode['adapters']['acquisition'] != 'multi_array':
        raise ValueError('Command plans require the routed multi-array acquisition contract')
    required = {'reference', 'array_selection', 'scan_order', 'samples_per_channel', 'sweeps_per_block'}
    if not required <= set(parameters):
        raise ValueError('Missing routed acquisition parameters')
    object_keys(config, ['acknowledgement', 'commands', 'status', 'capture_engine_field'], ['acknowledgement', 'commands', 'status'])
    if config['acknowledgement'] not in ('bare', 'echo'):
        raise ValueError('Invalid acknowledgement policy')
    if not isinstance(config['commands'], list) or not config['commands']:
        raise ValueError('Empty configuration command plan')
    covered, names = set(), set()
    for step in config['commands']:
        if 'parameter' in step:
            object_keys(step, ['parameter'], ['parameter'])
            key = step['parameter']
            parameter = parameters[key]
            if parameter.definition.get('policy') == 'reported':
                raise ValueError('Reported parameters cannot be configured')
            name = parameter.definition['command']
            covered.add(key)
        else:
            object_keys(step, ['command', 'value'], ['command', 'value'])
            name = step['command']
            validate_expression(step['value'], parameters)
        command_name(name)
        if name in names:
            raise ValueError(f'Duplicate configuration command: {name}')
        names.add(name)
    for key, parameter in parameters.items():
        name = parameter.definition.get('command')
        if key not in covered and name and parameter.definition.get('policy') != 'reported':
            if name in names:
                raise ValueError(f'Duplicate configuration command: {name}')
            names.add(name)
    status = config['status']
    object_keys(status, ['command', 'start', 'end', 'fields', 'retain_unknown'], ['command', 'start', 'end', 'fields'])
    command_name(status['command'])
    for marker in (status['start'], status['end']):
        object_keys(marker, ['prefix', 'contains', 'exclude'])
        if (not {'prefix', 'contains'} & set(marker)
                or any(not isinstance(v, str) or not v for v in marker.values())):
            raise ValueError('Invalid status transcript marker')
    if type(status.get('retain_unknown', False)) is not bool:
        raise ValueError('Invalid unknown-status policy')
    if not isinstance(status['fields'], dict) or not status['fields']:
        raise ValueError('Missing status field definitions')
    for key, field in status['fields'].items():
        if not re.fullmatch('[a-z][a-z0-9_]*', key):
            raise ValueError('Invalid status key')
        object_keys(field, ['type', 'target', 'also_targets', 'parameter', 'expected', 'comparison', 'required', 'choices'], ['type', 'target'])
        if field['type'] not in STATUS_TYPES:
            raise ValueError('Unknown status decoder')
        for target in [field['target'], *field.get('also_targets', [])]:
            status_target(target)
        if field['type'] == 'parameter' and field.get('parameter') not in parameters:
            raise ValueError('Unknown status parameter')
        if field.get('comparison', 'exact') not in ('exact', 'canonical_routes'):
            raise ValueError('Unknown status comparison')
        if field.get('comparison') == 'canonical_routes' and field['type'] != 'routes':
            raise ValueError('Route comparison requires a route decoder')
        if type(field.get('required', False)) is not bool:
            raise ValueError('Invalid required status flag')
        if 'choices' in field and (not isinstance(field['choices'], list) or not field['choices']):
            raise ValueError('Invalid status choices')
        if 'expected' in field:
            validate_expression(field['expected'], parameters)
    if 'capture_engine_field' in config:
        status_target(config['capture_engine_field'])
