"""Decode key=value status using the active profile's declared field contract."""


def marker_matches(line, marker):
    return (('prefix' not in marker or line.startswith(marker['prefix']))
            and ('contains' not in marker or marker['contains'] in line)
            and ('exclude' not in marker or marker['exclude'] not in line))


def parse_routes(value, hardware):
    width = hardware.get('route_encoding', {}).get('inputs_per_mux')
    routes = []
    for token in value.split(','):
        parts = tuple(map(int, token.split(':')))
        if width:
            if (len(parts) != 3 or not 1 <= parts[1] <= hardware['inputs_per_adc'] // width
                    or not 0 <= parts[2] < width):
                raise ValueError('Invalid external MUX route')
            lane, channel = parts[0], (parts[1] - 1) * width + parts[2]
        else:
            if len(parts) != 2:
                raise ValueError('Invalid ADC route')
            lane, channel = parts
        if (not 1 <= lane <= hardware['adc_lane_count'] or not 0 <= channel < hardware['inputs_per_adc']
                or channel in hardware['reserved_inputs'].values() or channel in hardware.get('unavailable_inputs', ())):
            raise ValueError('Reserved or out-of-range route')
        routes.append((lane, channel))
    if not routes or len(set(routes)) != len(routes):
        raise ValueError('Empty or duplicate routes')
    return routes


def decode_parameter(value, parameter):
    if parameter.definition['type'] == 'boolean':
        if value not in ('true', 'false'):
            raise ValueError('Invalid boolean status value')
        value = value == 'true'
    elif parameter.choices:
        for choice in parameter.choices:
            wire = choice.get('wire_value', choice['id'])
            encoded = str(wire).lower() if isinstance(wire, bool) else str(wire)
            if value == encoded:
                value = choice['id']
                break
    return parameter.normalize(value)


def apply_status_line(status, line, context):
    if not line.startswith('#') or '=' not in line:
        return False
    key, value = (part.strip() for part in line[1:].split('=', 1))
    contract = context.mode.definition.get('configuration', {}).get('status', {})
    field = contract.get('fields', {}).get(key)
    if field is None:
        if not contract.get('retain_unknown', False):
            return False
        status.parameters[key] = value
        return True
    try:
        kind = field['type']
        if kind == 'integer':
            value = int(value)
        elif kind == 'boolean':
            if value not in ('true', 'false'):
                return False
            value = value == 'true'
        elif kind == 'parameter':
            value = decode_parameter(value, context.mode.parameters[field['parameter']])
        elif kind == 'routes':
            value = parse_routes(value, context.profile.hardware)
    except (TypeError, ValueError):
        return False
    for target in (field['target'], *field.get('also_targets', ())):
        if '.' in target:
            container, name = target.split('.', 1)
            getattr(status, container)[name] = value
        else:
            setattr(status, target, value)
    return True
