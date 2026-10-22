"""Execute validated profile command plans and verify their reported values.

The expression vocabulary is deliberately small: literals, semantic parameters,
acquisition-derived sources and conditional selection. It never evaluates code.
"""
from config.array_scan import format_array_routes, order_array_routes, selected_arrays
from config.boards import get_board_registry
from config.boards.sensors import default_sensor_layout


def request_context(request):
    return request.board_context or get_board_registry().context(request.current_mcu, request.array_operation_mode)


def request_settings(request):
    context = request_context(request)
    if request.parameters is not None:
        return context, context.mode.resolve_settings(request.parameters)
    # Compatibility for callers which still construct the legacy request fields.
    values = {}
    for key, parameter in context.mode.parameters.items():
        attr = 'buffer_size' if key == 'sweeps_per_block' else parameter.legacy_key
        if hasattr(request, attr):
            values[key] = getattr(request, attr)
    return context, context.mode.resolve_settings(values, configuration_defaults=False)


def request_layout(request, context):
    return request.sensor_configuration or default_sensor_layout(context.profile)


def evaluate(expression, request, context, settings, layout):
    if 'literal' in expression:
        return expression['literal']
    if 'when' in expression:
        condition = expression['when']
        branch = 'then' if settings.effective[condition['parameter']] == condition['equals'] else 'else'
        return evaluate(expression[branch], request, context, settings, layout)
    if 'parameter' in expression:
        parameter = expression['parameter']
        values = settings.effective if expression.get('setting') == 'effective' else settings.requested
        value = values[parameter]
        if expression.get('wire'):
            value = context.mode.parameters[parameter].wire_value(value)
        return value
    source = expression['source']
    if source == 'mode':
        return context.mode.id
    if source == 'configured_routes':
        return list(request.testboard_adc_routes)
    if source == 'payload_routes':
        return order_array_routes(request.testboard_adc_routes, settings.requested['scan_order'], layout)
    if source == 'route_count':
        return len(request.testboard_adc_routes)
    if source == 'vmid_channel':
        return context.profile.hardware['reserved_inputs']['vmid']
    raise ValueError(f'Unknown configuration value source: {source}')


def encode(value, layout=None):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (list, tuple)):
            return format_array_routes(value, layout)
        return ','.join(str(v) for v in value)
    return str(value)


def command_plan(request):
    context, settings = request_settings(request)
    layout = request_layout(request, context)
    definition = context.mode.definition['configuration']
    commands = []
    for step in definition['commands']:
        if 'parameter' in step:
            key = step['parameter']
            parameter = context.mode.parameters[key]
            value = parameter.wire_value(settings.requested[key])
            name = parameter.definition['command']
        else:
            name = step['command']
            value = evaluate(step['value'], request, context, settings, layout)
        commands.append((name, encode(value, layout)))
    if len({name for name, _ in commands}) != len(commands):
        raise ValueError('Configuration would send a command more than once')
    return commands


def configure(service, request):
    from config.adc_configuration_service import ADCConfigurationResult
    from serial_communication.adc_connection_state import build_default_arduino_status
    from config.array_acquisition import validate_board_layout
    context, settings = request_settings(request)
    layout = request_layout(request, context)
    validate_board_layout(layout, context)
    hardware = context.profile.hardware
    allowed = {lane for array in selected_arrays(settings.requested['array_selection'], layout)
               for lane in layout['arrays'][str(array)]['adc_lanes']}
    routes = request.testboard_adc_routes
    unavailable = set(hardware['reserved_inputs'].values()) | set(hardware.get('unavailable_inputs', ()))
    if (not routes or len(set(routes)) != len(routes)
            or any(lane not in allowed or not 0 <= ch < hardware['inputs_per_adc'] or ch in unavailable
                   for lane, ch in routes)):
        raise ValueError('Invalid or empty acquisition routes')
    result = ADCConfigurationResult(True, context.mode.definition['device_mode'], build_default_arduino_status(),
                                    settings.requested['sweeps_per_block'])
    acknowledgement = context.mode.definition['configuration']['acknowledgement']
    for name, value in command_plan(request):
        success, _ = service._send_command_and_wait_ack(f'{name} {value}', value if acknowledgement == 'echo' else None)
        if not success:
            result.success = False
            result.messages.append(f'Configuration command failed: {name} {value}')
            break
        result.messages.append(f'Set {name}: {value}')
    return result


def complete(service, request, result):
    status = service._read_testboard_status() if service._read_testboard_status else None
    if status is None:
        result.success = False
        result.messages.append('No status data received yet')
        return
    _, settings = request_settings(request)
    status.repeat = settings.requested['samples_per_channel']
    status.buffer = settings.requested['sweeps_per_block']
    status.channels = list(dict.fromkeys(request.channels_to_send))
    status.use_ground = settings.requested.get('vmid_sampling', False)
    result.arduino_status = status


def status_value(status, target):
    if '.' in target:
        container, key = target.split('.', 1)
        return getattr(status, container).get(key)
    return getattr(status, target)


def verify(request, status):
    context, settings = request_settings(request)
    layout = request_layout(request, context)
    messages = []
    for key, field in context.mode.definition['configuration']['status']['fields'].items():
        actual = status_value(status, field['target'])
        expected = field.get('expected')
        if expected is None:
            if ((field.get('required') and actual is None)
                    or ('choices' in field and actual not in field['choices'])):
                messages.append(f'MISMATCH: Missing or invalid {key}: {actual}')
            continue
        value = evaluate(expected, request, context, settings, layout)
        if field.get('comparison') == 'canonical_routes':
            actual = None if actual is None else order_array_routes(actual, settings.requested['scan_order'], layout)
            value = order_array_routes(value, settings.requested['scan_order'], layout)
        elif field.get('type') == 'text' and not isinstance(value, str):
            value = encode(value, layout)
        if actual != value:
            messages.append(f'MISMATCH: Expected {key}={value}, got {actual}')
    return messages or ['Configuration matches reported status']
