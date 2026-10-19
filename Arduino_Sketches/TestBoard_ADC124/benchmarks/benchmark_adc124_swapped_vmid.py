"""Repeat Tests 21-23 after exchanging the two attached sensor strips.

Reuse the original acquisition code and exact diagnostic image. Keep the old
dataset immutable; the original runner restores frozen production in finally.
"""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sys

import benchmark_adc124_long_vmid as original

RAW = original.d.ROOT / 'docs/testing/datasets/testboard-adc124/vmid_swapped_strips_2026-10-16'
OUT = original.d.ROOT / 'docs/testing/results/testboard-adc124/vmid_swapped_strips_2026-10-16'


def main():
    if len(sys.argv) != 1:
        raise SystemExit('This repeat uses the complete unchanged 63-capture plan; no arguments.')
    previous = original.RAW
    lifecycle = json.loads((previous / 'lifecycle.json').read_text())
    image = original.d.FW / '.pio/build/teensy41_pcbdiag/firmware.hex'
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    if digest != lifecycle['diagnostic_sha256']:
        raise RuntimeError('Diagnostic image differs from the original Tests 21-23')
    pairs, scans = original.plan()
    expected = json.loads((previous / 'plan.json').read_text())
    actual = dict(pairs=[dict(key=p['key'], before=asdict(p['before']), after=asdict(p['after'])) for p in pairs],
                  scans=[asdict(p) for p in scans], hold_us=20000000, repetitions=3, expected_captures=63)
    if json.loads(json.dumps(actual)) != expected:
        raise RuntimeError('Acquisition plan differs from the original Tests 21-23')
    RAW.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    setup = dict(recorded_utc=datetime.now(timezone.utc).isoformat(),
        experiment='Repeat Tests 21, 22 and 23 after exchanging sensor strips',
        board='TestBoard_ADC124', port='COM4', usb_serial='20330160',
        original_dataset=str(previous), original_results=str(original.OUT),
        strip_assignment={'A': 'strip formerly attached to Array B', 'B': 'strip formerly attached to Array A'},
        strip_assignment_basis='User reports exchanging the sensor strips and reconnecting USB; contact mapping not independently verified',
        array_A_mux='TMUX1108', array_B_mux='TMUX1308ABQBR (TMUX1308A)',
        bias_ohms={'A': [470000,1000000,470000,1000000], 'B': [1000000,470000,1000000,470000]},
        sensors='PZT strips attached; no intentional stimulus requested',
        diagnostic_hex_sha256=digest, acquisition_plan_matches_original=True,
        limitation='Separate-day sequential comparison; temperature, vibration and sensor charge state not independently instrumented')
    setup_file = RAW / 'setup.json'
    if setup_file.exists():
        saved = json.loads(setup_file.read_text())
        assert saved['diagnostic_hex_sha256'] == digest and saved['strip_assignment'] == setup['strip_assignment']
    else:
        setup_file.write_text(json.dumps(setup, indent=2))
    original.RAW, original.OUT = RAW, OUT
    original.main()


if __name__ == '__main__':
    main()
