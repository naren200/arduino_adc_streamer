# file_operations (Legacy)

Archived file-export code for features removed from the active application.

## Files

### pzt_decay_exporter.py

`PztDecayExporter` writes PZT_Decay_Calc results: a samples CSV (`SAMPLE_COLUMNS`), a result JSON, and a summary CSV for one `PztDecayResult`.

- `PztDecayExporter.export(directory, result, samples, write_summary=True)` — writes the result files and returns their paths.
- `PztDecayExporter.select_samples_for_csv(samples)` — selects the pre-trend, fit, and post-fit context samples to persist.
- `PztDecayExporter.write_samples_csv(path, result, samples)` / `write_summary_csv(path, result, samples_path, result_path)` — CSV writers.

### __init__.py

Empty package marker file (no content).
