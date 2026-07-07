# Analysis Tab Feature Specification

Owner: Host application GUI/data-processing stack  
Status: Implemented  
Date: 2026-07-07

## Purpose

The Analysis tab provides offline inspection of retained captures or app-exported CSV plus JSON metadata. It is read-only: loading, filtering, plotting, marker inspection, image export, and derived calculations never mutate live acquisition buffers, firmware protocol state, or runtime processing settings.

## Sources

- In-memory cache: copies the current circular capture buffer in oldest-to-newest order and uses the same display-channel labels shown in the Time Series graph when available.
- CSV plus JSON: loads files produced by the app's Save Data flow. Metadata is used for timing/configuration context, but older CSV files are tolerated when they contain usable named signal columns.

CSV compatibility rules:

- Force columns named `Force_X_N` / `Force_Z_N` are loaded as Newtons.
- Legacy raw force columns named `Force_X` / `Force_Z` are converted to Newtons using the configured load-cell constants.
- Legacy placeholder columns such as `Col1`, `Col3`, etc. are ignored when real signal labels are present.
- CSV/metadata signal-count mismatches are reported as warnings instead of blocking analysis when the CSV can still be interpreted.

Analysis is disabled while runtime acquisition is active and re-enabled after capture stops. CSV plus JSON analysis can be used before MCU connection because it does not depend on serial state.

## UI Behavior

- New visualization tab: Analysis, with nested Display and Settings sub-tabs.
- Source selector: In-memory cache or CSV plus JSON.
- File controls: browse/load CSV and metadata JSON.
- Axis selector: Time ms or Sample index.
- Zoom selector: X only, Y only, or X and Y.
- Channel checklist with All/None buttons.
- Optional processing/display toggles: Spectrum-compatible filter, shear, normal pressure, integration, calculated PZT force, and marker.
- PZT force settings: capacitance, leak resistance, d33, fallback noise threshold, quiet-window duration, noise multiplier, and a Calculate Vmid + Noise action.
- Analysis image export controls: select Raw signals, Integrated signals, Shear / Normal, and/or Force plots and save them as PNG images.
- Calculated-force traces use the same plot color as their source raw signal trace. For example, if `PZT3_L` is green in Raw signals, `Calculated Force - PZT3_L [N]` is also green in Force.
- Display controls, channel checklists, force-trace checklists, and image-export controls are width-stable and must remain visible inside the Analysis scroll area. Long calculated-force labels are displayed in a compact force-trace list instead of forcing horizontal overflow.
- Marker: mouse readout reports nearest displayed values for visible signal, integration, derived, and force traces. Marker readout text is elided to the visible status-label width and keeps the full text in a tooltip, so moving the marker cannot resize or horizontally shift the Analysis layout.

## Data Pipeline

1. Copy or load source arrays, timestamps, force traces, metadata, and labels into an `AnalysisSourceSnapshot`.
2. Normalize X-domain as sample index or milliseconds.
3. Optionally apply the current Spectrum filter settings to a copy of the source data.
4. Convert ADC signal counts to volts using the active configured Vref.
5. Build raw signal traces for visible channels.
6. Optionally build integrated traces independently for each visible voltage channel using Pressure-map integration and HPF settings.
7. Optionally build shear and normal-pressure traces from mapped R/L/C/T/B positional channels.
8. Build measured force traces in Newtons.
9. Optionally reconstruct calculated PZT force traces from visible voltage channels, using per-channel quiet-window Vmid/noise calibration when available.
10. Render synchronized plots with a shared X range:
    - Raw signals.
    - Integrated signals.
    - Shear / Normal derived traces.
    - Force traces, including measured load-cell force and calculated PZT force.

## PZT Force

Calculated PZT force is implemented outside the GUI in `data_processing/pzt_force_calculation.py`, with defaults in `constants/pzt_force.py`, so the calculation can be reused by future app sections.

The calculation:

- Centers voltage around a per-channel Vmid estimate, or a median fallback.
- Sets centered voltage below the per-channel noise threshold to zero before integration.
- Models leakage with `alpha = exp(-dt / (Rleak * Cpzt))`.
- Converts generated charge into force using `d33`.
- Resets accumulated force after complete opposite-polarity events return below the noise threshold to reduce drift.

Quiet-window calibration estimates Vmid from the first configured duration of data and uses a consistent percentile-deviation noise threshold for every channel. MAD and robust sigma remain diagnostic values.

## Persistence

The tab persists UI preferences under `~/.adc_streamer/analysis/last_used_analysis_settings.json`, including source mode, axis mode, zoom mode, overlay toggles, marker enabled state, channel visibility, image-export selections, PZT force settings, per-channel PZT baseline/noise calibration, and last file references. Raw capture arrays are not persisted.

## Acceptance Criteria

- The Analysis tab appears in the visualization tabs.
- In-memory loading shows the latest retained capture after Stop.
- CSV plus JSON loading works before MCU connection for valid app exports.
- Older CSV exports with named signal columns plus redundant `ColN` placeholders load without error; placeholder columns are hidden, and metadata-count mismatches are warnings.
- Invalid CSV/metadata files show actionable failures and preserve the prior valid state.
- Channel visibility changes redraw without reloading the source.
- Optional filtering uses the current Spectrum filter widget settings on a data copy.
- Shear, normal pressure, and integration overlays can be independently toggled.
- Integration is plotted separately from raw signals, and each integrated trace uses the same color as its source raw trace.
- Shear and normal pressure are plotted together on their own derived plot.
- Hiding a raw signal channel also hides that channel's integrated and calculated-force traces.
- Calculated PZT force traces use the same color as their corresponding raw signal traces.
- Measured load-cell force is displayed in Newtons.
- Calculated PZT force can use per-channel quiet-window Vmid/noise calibration, and below-threshold voltage does not contribute to force integration.
- Raw, integrated, shear/normal, and force plots share X range and support X-only, Y-only, and X+Y zoom modes.
- User can export selected Analysis plots to PNG images.
- Marker readout reports nearest values for visible traces without changing the page width or causing horizontal jiggle while the cursor moves.
- Analysis controls and dynamic channel/force-trace lists remain inside the visible scroll area when calculated PZT force is enabled.
- Capture start disables Analysis controls; capture finish re-enables them.

## Out Of Scope

- Real-time Analysis streaming.
- Firmware or serial protocol changes.
- New export formats.
- Persisting raw offline data blobs in settings.
