"""
Analysis Source Load Worker
=============================
Background worker that runs load_exported_csv_snapshot() off the GUI thread,
so browsing to a large CSV/JSON source -- or toggling the Blip Filter, which
reloads the current source to re-bake the filter into the snapshot -- never
blocks the UI.
"""

from __future__ import annotations

from data_processing.analysis_workbench import load_exported_csv_snapshot
from data_processing.queued_background_worker import QueuedBackgroundWorker


class AnalysisSourceLoadWorker(QueuedBackgroundWorker):
    """Runs load_exported_csv_snapshot() in a dedicated thread.

    The queue is bounded to 1: a fresh submission evicts a still-pending
    one, so only the latest requested load is ever parsed.
    """

    def _process(self, payload: dict) -> dict:
        snapshot = load_exported_csv_snapshot(
            payload["csv_path"],
            payload["metadata_path"],
            blip_filter_enabled=payload["blip_filter_enabled"],
            blip_filter_window_samples=payload["blip_filter_window_samples"],
        )
        return {
            "snapshot": snapshot,
            "csv_path": payload["csv_path"],
            "metadata_path": payload["metadata_path"],
        }
