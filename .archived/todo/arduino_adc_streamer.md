# arduino_adc_streamer — Handoff TODO

## Project snapshot
PyQt6 desktop app streaming ADC data from Arduino/Teensy over serial. Dual-device: ADC MCU + optional Force MCU. Mixin-heavy architecture — `adc_gui.py` inherits ~15 mixins.

**Entry:** `adc_gui.py` — `ADCStreamerGUI` class  
**Run:** `python adc_gui.py`  
**Tests:** `pytest tests/`

---

## Architecture: key files

| Layer | Files |
|---|---|
| GUI mixins | `gui/display_panels.py`, `gui/control_panels.py`, `gui/heatmap_panel.py`, `gui/sensor_panel.py`, `gui/signal_integration_panel.py`, `gui/spectrum_panel.py`, `gui/analysis_panel.py`, `gui/force_calibration_panel.py` |
| Config | `config/config_handlers.py` (largest file ~1400L), `config/adc_config_state.py` |
| Data processing | `data_processing/binary_processor.py`, `adc_plotting.py`, `force_overlay.py` |
| Serial | `serial_communication/adc_serial.py`, `force_serial.py` |
| Constants | `constants/ui.py`, `plotting.py`, `force.py`, `heatmap.py`, `sensor_config.py` |
| Connection states | `serial_communication/adc_connection_state.py`, `force_connection_state.py` |

---

## Active TODO: Enum & class refactor

### Why
Bare string comparisons scattered across 15+ files. No typo safety, no autocomplete.

### Plan: create `constants/enums.py`

```python
from enum import Enum

class DeviceMode(str, Enum):
    ADC = "adc"
    ANALYZER_555 = "555"

class ArrayOperationMode(str, Enum):
    PZT = "PZT"
    PZR = "PZR"       # drives device_mode = '555'
    PZT_RS = "PZT_RS" # split PZT + Rosette tabs

class ChannelSelectionSource(str, Enum):
    MANUAL = "manual"
    ARRAY = "array"
    NONE = "none"

class SpecKeyType(str, Enum):
    ADC = "adc"        # key = ('adc', channel_int)
    SENSOR = "sensor"  # key = ('sensor', sensor_id, placement, channel, [mux])
    MUX = "mux"        # key = ('mux', mux_num, channel)
    RS = "rs"          # key = ('rs', ...)
```

Use `str, Enum` so existing `== 'adc'` comparisons keep working during migration.

### Where each string is used

**`DeviceMode`** — 6 occurrences in `data_processing/adc_plotting.py` (`getattr(self, 'device_mode', 'adc') == '555'`), set at `config/config_handlers.py:877` (`self.device_mode = '555' if mode == 'PZR' else 'adc'`), read in `filter_processor.py`, `data_exporter.py`, `mcu_profile.py`, `heatmap_panel.py`, `binary_processor.py`

**`ArrayOperationMode`** — set/compared in `config/config_handlers.py:124,161,241`, `config/adc_config_state.py:13` (default `"PZT"`), `config/config_snapshot.py`, `config/mcu_profile.py`

**`ChannelSelectionSource`** — `config/config_handlers.py:387,563,566,680,682,819,845,847,852,854`, `config/adc_config_state.py:12` (default `"none"`)

**`SpecKeyType`** — `data_processing/heatmap_piezo_processor.py:31,52,69,71,73` (`key[0] == 'sensor'` etc), `data_processing/adc_plotting.py:40-48` (`_extract_channel_from_spec_key`), `gui/signal_integration_panel.py:619` (`== 'rs'`), `config/config_handlers.py:621,659,713,737` (dict literal construction `'key': ('adc', channel)`)

### Also do: `DisplayChannelSpec` dataclass

Spec dicts built in `config/config_handlers.py:600-740`, consumed by plotting/heatmap. Stable shape:

```python
@dataclass(frozen=True)
class DisplayChannelSpec:
    key: tuple
    label: str
    sample_indices: list[int]
    color_slot: int
    stream: str | None = None  # 'rs' or None
```

Callers access via `.key`, `.label`, `.sample_indices`, `.color_slot`, `.stream` — already use dict `.get()` so migration is straightforward.

### Skip (not worth it)
- `VoltageReference`, `ConversionSpeed`, `SamplingSpeed`, `CapacitanceUnit` — localized to 1–2 spots in `config_handlers.py`, firmware protocol strings
- `FilterType`, `DCRemovalMode` — 1–2 files only
- Tab name constants → enum: already fine as string constants in `constants/ui.py`

---

## Architecture gotchas

### Mixin render loop
`binary_processor.py` calls `update_plot()` then `update_force_plot()` sequentially inside a rate-limited block guarded by `should_update_live_timeseries_display()`. Force plot also has its own `force_plot_timer`. Do NOT call `update_force_plot` inside `update_plot` — they're intentionally separate.

### Dual ViewBox (force overlay)
`data_processing/force_overlay.py::_get_force_plot_target()` returns different viewbox/curve/checkbox refs depending on active tab:
- Time Series tab → `force_viewbox`, `_force_x_curve`, `_force_z_curve`, `force_x/z_checkbox`
- Rosette tab → `rosette_force_viewbox`, `_rosette_force_x/z_curve`, `rosette_force_x/z_checkbox`

Rosette force viewbox uses **one-directional X-sync** (`_sync_rosette_force_x_range`) — not `setXLink` — to avoid autorange feedback loop killing main plot visibility.

### PZT_RS mode splits tabs
`update_pzt_rs_timeseries_tabs_visibility()` in `gui/display_panels.py` shows/hides `rosette_tab_index=1`. When PZT_RS active: tab 0 relabels to "PZT", tab 1 "Rosette (RS)" becomes visible.

### Sweep buffer is circular
`MAX_SWEEPS_BUFFER` rows, `buffer_write_index % MAX_SWEEPS_BUFFER`. Snapshot helpers in `adc_plotting.py` handle the wrap. Do not read buffer naively.

### `config` is `ADCConfigurationState` dataclass
Supports `config['key']` and `config.key` — see `config/adc_config_state.py`. Not a plain dict. Default mode `array_operation_mode="PZT"`, `channel_selection_source="none"`.

### Connection state enums already exist
`serial_communication/adc_connection_state.py::ADCConnectionState` and `force_connection_state.py::ForceConnectionState` — both use `auto()`. Do not duplicate.

---

## Migration order for enum refactor

1. Create `constants/enums.py` with four enums above
2. `config/adc_config_state.py` — change field types from `str` to enum, update `build_default_adc_config_state()`
3. `config/config_handlers.py` — replace literals at lines noted above
4. `data_processing/adc_plotting.py` — replace `getattr(self, 'device_mode', 'adc')` guards
5. `data_processing/heatmap_piezo_processor.py` — replace `key[0] == 'sensor'` etc
6. Remaining files — `filter_processor.py`, `data_exporter.py`, `mcu_profile.py`, `binary_processor.py`
7. Add `DisplayChannelSpec` dataclass, replace spec dict construction in `config_handlers.py:600-740`
