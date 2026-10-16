# Profile-driven device configuration

The shared configuration service executes three phases: send commands, collect
status, verify. It does not select behavior by a board identity or PCB name.
The board registry resolves identity to a JSON mode definition before these
phases begin.

## Definition and execution boundaries

- Profile JSON owns parameter defaults/bounds, wire tokens, command order, ACK
  policy, transcript markers, status decoders/destinations and expected values.
- Sensor JSON owns electrical routes and spatial labels.
- `config/profile_configuration.py` executes validated command plans and compares
  status with the same resolved settings used by the GUI.
- `serial_communication/profile_status.py` implements reusable integer, boolean,
  text, parameter and route decoders. It also matches transcript markers.
- `serial_communication/protocols/registry.py` registers acquisition/protocol
  algorithms and binds them to the active context. It has no per-board dispatch.

Both TestBoard profiles select `adapters.protocol: "profile_commands"`. The
previous board-specific configuration functions and ADC124 protocol module are
removed. Their firmware command sequences, ADC ranges and route order contracts
are preserved.

The existing `legacy_adc`, `legacy_555` and `array_dual` adapters retain shared
algorithms for channel blocks, resistance tuning, combined streams and buffer
capacity. These are protocol behaviors rather than PCB identities. They use the
same service lifecycle, with scalar extensions sent before verification. A new
board which requires a genuinely different acquisition algorithm still needs an
algorithm adapter; changing identity, scalar commands or routed status does not.

## JSON command plan

Each scalar parameter can declare a `command`. An ordered step can reference its
semantic ID; the executor obtains its value and wire encoding from that parameter.
For acquisition-derived or conditional arguments, a step declares a command and
a bounded value expression:

```json
{
  "acknowledgement": "bare",
  "commands": [
    {"parameter": "reference"},
    {"command": "adcchannels", "value": {"source": "configured_routes"}},
    {"parameter": "settle_time_us"},
    {
      "command": "vmidsettle",
      "value": {
        "when": {"parameter": "vmid_settle_follow_sensor", "equals": true},
        "then": {"literal": "default"},
        "else": {"parameter": "vmid_settle_time_us"}
      }
    }
  ]
}
```

This is a fragment; a complete mode also declares its status contract. Additional
non-reported scalar parameters with `command` are appended before status is read.
Add an explicit step when their position in the sequence matters. Duplicate
commands fail profile validation and are also rejected when building a plan.
`bare` ACKs accept success without an echoed argument; `echo` requests argument
verification through the existing ACK transport.

Values support a literal, a parameter (requested or effective, optionally encoded
with its wire token), an acquisition source, or a conditional choice. Available
sources are mode, configured routes, payload routes, route count and Vmid channel.
The evaluator supports no Python expressions, arbitrary attribute paths or imports.
Conditions have a bounded nesting depth.

## JSON status contract

`configuration.status` declares the query command, start/end markers and fields.
Each field declares its decoder and destination in `ArduinoStatus`, the
`parameters` dictionary or `stream_diagnostics`. For example:

```json
{
  "type": "integer",
  "target": "parameters.vmid_settle_us",
  "expected": {
    "when": {"parameter": "vmid_settle_follow_sensor", "equals": true},
    "then": {"parameter": "settle_time_us"},
    "else": {"parameter": "vmid_settle_time_us"}
  }
}
```

An expected value makes the field mandatory for successful verification. A field
without an expectation can be optional telemetry or require membership in a list
of allowed values. Route comparisons can preserve exact order or compare the
canonical scan order. A payload text field can compare against the encoded
`payload_routes` source. Settings, routes and transcript metadata are frozen into
the capture context; old captures are interpreted from their saved definitions.

The startup validator rejects unknown command/status fields, references to
missing parameters, unsupported decoders, invalid transcript markers, unsafe
destinations and duplicate commands. `config/boards/schema.json` documents the
same authoring structure. Increment `profile_version` after contract changes.

## Compatibility and validation

Historical Python import/field names remain compatibility facades. The optional
`registry.json.compatibility_profiles.routed_status` entry supplies a default
profile to old callers which omit an active context; normal acquisition passes
its identified context explicitly. Those facades hold no separate wiring or
electrical configuration.

`tests/test_profile_configuration.py` creates a new profile using only JSON:
different MCU identity, reference/wire token, parameter name/default/bounds,
command names, transcript markers/status keys and an additional scalar parameter.
It verifies every command runs once, new parameters precede the status query,
reported mismatches fail verification, and invalid definitions fail startup.
Existing board, capture, export and offline tests provide compatibility coverage.

Firmware is unchanged. Software tests do not establish analog settling or channel
identity on hardware. The ADC124 hardware validation steps remain in the
[array configuration guide](../../guides/configuration/ARRAY_CONFIGURATION_GUIDE.md#testboard_adc124-external-mux-arrays).
