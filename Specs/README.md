# Specs

Planning and specification documents for upcoming and recently implemented features. Prompt-style implementation plans capture steps, relevant files, decisions, and verification notes; formal spec documents describe current intended behavior after implementation.

## Files

- `plan-forceCalibration.prompt.md` — implementation plan for a new Force Calibration tab: lets the user select a sensor family/number, capture peak force and sensor response during a measurement window, build a calibration table, and save/load calibration files per sensor family. Scopes the first slice to the tab UI and calibration-table persistence, deferring pressure-map/shear/heatmap legend integration to a future follow-up. Lists affected files, verification steps, decisions made, and open questions.
- `ANALYSIS_TAB_SPEC.md` — current Analysis tab specification: offline in-memory or CSV+JSON loading, legacy CSV compatibility, raw/integrated/shear-normal/force plots, load-cell Newton conversion, calculated PZT force, marker/zoom behavior, PNG image export, settings persistence, and acceptance criteria.
- `plan-analysisTabOfflineSignalWorkbench.prompt.md` — implementation plan for the offline Analysis workbench, including source adapters, read-only lifecycle, Spectrum filter reuse, integration/shear/normal overlays, synchronized plots, marker behavior, settings persistence, and current compatibility decisions.
- `plan-inAnalysisTab-Calculated_PZT_Force.md` — feature specification for calculated PZT force in Analysis: settings, reusable calculation ownership, quiet-window Vmid/noise estimation, noise thresholding, leakage model, automatic zeroing, plot behavior, and export expectations.
