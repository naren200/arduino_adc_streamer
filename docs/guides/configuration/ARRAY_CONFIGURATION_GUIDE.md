# Array Sensor Configuration Guide

## Overview

The sensor library supports two configuration types:

- `channel_layout`: a single 5-channel package using the logical positions `T`, `R`, `C`, `L`, and `B`
- `array_layout`: a 3x3 grid of named sensors with per-sensor MUX and channel assignments

This guide covers the current array-layout path used by the GUI.

## Current Array Model

The current editor in the Sensor tab uses:

- a `3 x 3` array grid
- sensor IDs in canonical form such as `PZT1` or `PZR2`
- optional legacy input such as `PZT_1`, which is normalized when saved
- `1..5` logical channels per sensor
- MUX/ADC lane IDs `1..4` (`1..2` on the PCB1.x MG24 boards)
- physical channel indices `0..15`

## What Array Layouts Affect

An active array configuration is used to:

- resolve selected array sensors into physical acquisition channels
- build display labels for the time-series view
- define package grouping used by the heatmap and shear tabs
- persist the selected sensor library entry across app restarts

## Where Configurations Are Stored

- Bundled starter library: `sensors_library/sensor_configurations.json`
- User-edited library: `~/.adc_streamer/sensors/sensor_configurations.json`

The app loads the bundled library first when available, then overlays user edits from the local settings path.

## Creating Or Editing An Array Layout

1. Open the `Sensor` tab.
2. Create a new configuration or select an existing one.
3. Set the configuration type to `Array Layout`.
4. Fill the `3 x 3` array grid with sensor IDs such as `PZT1`, `PZR2`, or leave cells blank.
5. Add a MUX mapping for every sensor present in the grid.
6. Set `Channels per Sensor` to match the hardware layout.
7. Save the configuration.

## Example Layout

Example `3 x 3` grid:

```text
[PZT1] [PZT2] [PZT3]
[PZR4] [PZT5] [PZR6]
[   ]  [PZT7] [   ]
```

Example MUX mapping:

```text
PZT1 -> MUX 1, Channels 0,1,2,3,4
PZT2 -> MUX 1, Channels 5,6,7,8,9
PZT3 -> MUX 2, Channels 0,1,2,3,4
PZR4 -> MUX 2, Channels 5,6,7,8,9
```

## How Acquisition Mapping Works

During acquisition:

1. The selected array sensors are converted into the set of unique physical channels required by the active layout.
2. The MCU receives that physical channel list in acquisition order.
3. The GUI remaps the returned samples back into sensor-aware labels for display and processing.

This means the physical stream can contain shared or de-duplicated channels, while the GUI still renders the selected sensors using sensor-specific labels.

### TestBoard_7953: two ADS7953 arrays

The GUI recognizes `TestBoard_7953` and the historical `PCB_TestBoard_7953`
alias. Select the bundled **TestBoard_7953** sensor configuration. Both physical
arrays use the Array_PCB1.7 spatial grid and share the same selected PZT numbers.
The configuration records `array_count: 2` and physical ADC pairs:

```json
"arrays": {
  "1": {"adc_lanes": [1, 2]},
  "2": {"adc_lanes": [3, 4]}
}
```

The shared `mux_mapping` uses pair-local ADC positions 1/2. Sensor JSON owns
electrical mappings, spatial placement and polarity. Electrical editing is
profile-controlled and enabled for TestBoard. Validation checks capacity,
overlapping routes and reserved inputs, without a competing Python wiring map.
Old single-array configurations continue to load with `array_count: 1`.

- Array 1 uses ADC1/ADC2; array 2 uses ADC3/ADC4.
- PZT6 and PZT7 use the first ADC in each pair, inputs 0..4 and 5..9.
- PZT1, PZT3, and PZT5 use the second ADC, inputs 0..4, 5..9, and 10..14.
- Every group is ordered B, L, C, R, T. Input 15 is reserved for Vmid.

Only PZT acquisition is available. Choose **Physical Arrays** (1, 2, or both)
and enter one PZT-number list, such as `6,1`. Both arrays receive that same list.
No channel-number or PZR selection is shown.

ADC Configuration offers **Voltage Reference** (2.5 V or 5 V input span) and
**SPI CLK** in MHz (0.1-30, default 20). Clocks above 20 MHz are experimental;
the recorded clock is requested Hz, not a measured SCLK. Gain and OSR are hidden.
**Repeat Count** is 1-3 (default 1) settling conversions; only the final value is
retained. **Use Vmid Sample** defaults off and controls an optional discarded
conversion on the firmware's fixed Vmid input. Mandatory firmware parking still
runs. Vmid sampling forces **Sequence** to `manual`. With Vmid off, `auto1` is
available and shows effective repeat 1, preserving the requested manual repeat.
Sweeps per block is fixed internally to 1 and hidden.

`modes.PZT.parameters.scan_order.default` in
`config/boards/profiles/testboard_7953.json` controls payload order (default `adc`).
Edit that JSON value to use `array` or `interleaved` and restart; there is no GUI
scan-order setting. Invalid values prevent configuration. Configure sends the
board-specific commands and requires a complete matching firmware status reply.

The shared **Display Array** selector defaults to 1 for both-array captures.
It remains enabled during acquisition and changes the Time Series, Spectrum,
Heatmap, Pressure Map and decay preview without restarting the scan. Single-array
captures lock it to the sampled array. Both arrays retain their data and separate
processing histories. Calibration rows retain the array selected when measurement
starts even if the live display switches. The decay tab offers array-qualified
live voltage previews; physical decay characterization is unavailable because
ADS7953 connection timing is not supplied by this firmware.

Archives and CSV metadata save the full acquisition mapping and reference span.
CSV always includes every sampled array, using labels such as `A1_PZT6_B` and
`A2_PZT6_B`. Stopped captures keep their saved mapping after disconnection or
library edits. Analysis has its own Display Array selector for the loaded source.
Files identified as historical TestBoard captures without route metadata cannot
be reliably associated with an array; loading reports this limitation.

Before hardware acceptance, check each array separately and both together, both
input spans, repeats 1-3, Vmid/manual and Vmid-off/auto1. Press the same PZT on each
array independently, switch views during a continuous scan, and verify an
archive larger than the RAM buffer exports/reloads both arrays correctly. Check
for missing frames or returned-channel errors. Measure SCLK when validating actual
clock frequency.

## TestBoard_ADC124 external MUX arrays

Connect firmware reporting `TestBoard_ADC124` (protocol 3). The GUI selects the
bundled ADC124 sensor layout and exposes **Physical Arrays**, **PZT Sensors**,
**SPI CLK [MHz]**, settling-conversion **Repeat Count**, **Use Vmid Sample**,
**Settle time [us]**, **VMID settle follows sensor**, and **VMID settle time [us]**.
Enable **Use Vmid Sample** to edit the independent VMID delay. Both delays default
to 5 us and accept 0..1000 us; zero retains the firmware switching guards.
**VMID settle follows sensor** is off by default; selecting it uses sensor
settling and disables the independent delay field. Vmid parking at startup/stop/failure is
mandatory even when optional Vmid sampling is off.

All parameter defaults, bounds, labels and policies live in
`config/boards/profiles/testboard_adc124.json`. Edit `scan_order.default` to
`adc` (array sequential) or `mux` (shared address settling), `spi_engine.default`
to `blocking`, `dma` or `lpspi`, and `spi_frame.default` to `burst` or `single`.
These three parameters are config-file-only, like the 7953 scan order. Restart
after editing JSON. Requested SPI clock is limited to 8–16 MHz; voltage scaling
is fixed at 3.3 V / 12 bits. Reconnect restores profile defaults.

Each array contains PZT1, PZT3, PZT5, PZT6 and PZT7 (25 channels per array).
PZT1/3/5/6 use inputs 0..4 on MUX1/2/3/4 respectively. PZT7 uses input 5 on all
four MUXes followed by MUX4 input 6. The shared runtime represents each endpoint
as `(ADC, packed input)`, where `packed input = (MUX - 1) * 8 + external input`.
The bundled sensor JSON declares this encoding; the protocol adapter sends
native triples. MUX1..3 input 6 and every input 7 are excluded from sensor routes.
The Sensor tab displays this fixed wiring; JSON remains its electrical owner.

The firmware supplies numbered channels without spatial orientation. The layout
therefore labels them CH1..CH5; a directional B/L/C/R/T assignment must be
established before interpreting spatial pressure/force results. The spatial
channel editor is hidden for the numbered layout. Time Series, Spectrum, capture,
array switching and export retain every selected channel with labels such as
`A2_PZT7_CH5`. Both arrays share one SPI bus and are converted sequentially.

Configure requires successful ACKs plus matching reported reference, route list,
exact payload order, scan settings, and effective sensor/VMID delays. Repeat is
1..3 conversions with only the last retained; each frame is one complete sweep.
Captures include frozen settings and wiring for export and offline reload.

Hardware validation for this GUI integration remains pending:

1. Connect, Configure each array and both arrays, and compare status for sensor
   delay 5 us with VMID following, then explicit VMID delays 0/1/5 us. Change
   sensor delay and confirm follow mode tracks it while explicit mode stays fixed.
2. Restart with each JSON scan order and engine. Test full and sparse PZT7 routes
   and repeats 1/3. Apply distinct known voltages to verify channel identity and
   fixed 3.3 V scaling; measure actual SCLK when checking frequency.
3. Capture while switching Display Array. Stop, disconnect, export and reload;
   verify both arrays and their channel labels. Check firmware transfer/data/USB
   errors, complete frames and sampled = sent + discarded counters.
4. Check MUX parking at address 7 on stop/failure, then run a long capture and
   export a recording larger than RAM. Compare sensor noise/carryover separately
   when changing settling settings; software replay does not establish analog timing.

## Example JSON Shape

```json
{
  "name": "Array_V2",
  "type": "array_layout",
  "channel_sensor_map": ["T", "R", "C", "B", "L"],
  "array_layout": {
    "cells": [
      [null, "PZT7", null],
      ["PZT1", "PZR6", "PZT5"],
      ["PZR2", "PZT3", "PZR4"]
    ]
  },
  "mux_mapping": {
    "PZT1": {
      "mux": 1,
      "channels": [0, 1, 2, 3, 4]
    },
    "PZR2": {
      "mux": 2,
      "channels": [0, 1, 2, 3, 4]
    }
  },
  "channel_layout": {
    "channels_per_sensor": 5
  }
}
```

## Validation Rules

- Every populated cell must be a valid `PZTn` or `PZRn` sensor ID.
- Every sensor in the grid must have a MUX mapping.
- MUX/ADC lane values must stay within `1..4`. Use only `1..2` for PCB1.x MG24
  firmware. Declared profiles provide board capacity and reserved-resource
  constraints. TestBoard mappings can be edited consistently in sensor JSON.
- Physical channels must stay within `0..15`.
- `channels_per_sensor` must stay within `1..5`.

## Troubleshooting

| Issue | Likely cause | What to check |
| --- | --- | --- |
| Array config will not save | Invalid sensor ID or incomplete MUX mapping | Check every populated grid cell has a matching mapping row |
| Sensors display the wrong channels | MUX/channel assignments do not match wiring | Compare the saved mapping with the hardware wiring |
| Heatmap or shear view is empty | Selected sensors do not form valid grouped input for the active mode | Verify sensor selection, mode, and channel count |

## Related Files

- `config/sensor_config.py`: normalization, validation, and persistence helpers
- `gui/sensor_panel.py`: Sensor tab editor and save/load behavior
- `config/config_handlers.py`: mapping from selected sensors to acquisition channels
- `docs/guides/heatmap/HEATMAP_README.md`: how array layouts feed the heatmap path
