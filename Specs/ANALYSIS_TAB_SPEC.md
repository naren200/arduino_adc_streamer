# Analysis Tab Feature Specification

Owner: Host application GUI/data-processing stack  
Status: Implemented vertical slice  
Date: 2026-06-26

## Purpose

The Analysis tab provides offline inspection of retained captures or app-exported CSV plus JSON metadata. It is read-only: loading, filtering, plotting, and marker inspection never mutate live acquisition buffers, firmware protocol state, or runtime processing settings.

## Sources

- In-memory cache: copies the current circular capture buffer in oldest-to-newest order.
- CSV plus JSON: loads files produced by the app's Save Data flow, validates the metadata configuration block, and rejects mismatched signal-column counts with a user-facing error.

Analysis is disabled while runtime acquisition is active and re-enabled after capture stops. CSV plus JSON analysis can be used before MCU connection because it does not depend on serial state.

## UI Behavior

- New visualization tab: Analysis.
- Source selector: In-memory cache or CSV plus JSON.
- File controls: browse/load CSV and metadata JSON.
- Axis selector: Time ms or Sample index.
- Zoom selector: X only, Y only, or X and Y.
- Channel checklist with All/None buttons.
- Optional overlays: Spectrum-compatible filter, shear, normal pressure, and integration.
- Marker: mouse readout reports nearest displayed values for visible signal, overlay, and force traces.

## Data Pipeline

1. Copy or load source arrays and timestamps into an `AnalysisSourceSnapshot`.
2. Normalize X-domain as sample index or milliseconds.
3. Optionally apply the current Spectrum filter settings to a copy of the source data.
4. Compute overlays from the display snapshot:
   - Convert counts to volts using the active configured Vref.
   - Integrate C/L/R/T/B channels with Pressure-map integration and HPF settings.
   - Run existing shear and normal-force calculators.
5. Render signal and overlay traces in the upper plot and force traces in the lower plot with a shared X range.

## Persistence

The tab persists UI preferences under `~/.adc_streamer/analysis/last_used_analysis_settings.json`, including source mode, axis mode, zoom mode, overlay toggles, marker enabled state, channel visibility, and last file references. Raw capture arrays are not persisted.

## Acceptance Criteria

- The Analysis tab appears in the visualization tabs.
- In-memory loading shows the latest retained capture after Stop.
- CSV plus JSON loading works before MCU connection for valid app exports.
- Invalid/mismatched CSV/metadata files show actionable failures and preserve the prior valid state.
- Channel visibility changes redraw without reloading the source.
- Optional filtering uses the current Spectrum filter widget settings on a data copy.
- Shear, normal pressure, and integration overlays can be independently toggled.
- Upper/lower plots share X range and support X-only, Y-only, and X+Y zoom modes.
- Marker readout reports nearest values for visible traces.
- Capture start disables Analysis controls; capture finish re-enables them.

## Out Of Scope

- Real-time Analysis streaming.
- Firmware or serial protocol changes.
- New export formats.
- Persisting raw offline data blobs in settings.
