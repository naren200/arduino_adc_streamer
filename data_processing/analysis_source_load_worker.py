"""
Analysis Source Load Worker
=============================
Background worker that produces Analysis source windows off the GUI thread.

A payload with ``window_source`` and ``view_start_s`` pages that already-open
``AnalysisWindowSource`` to a new window.  Otherwise the payload names a CSV and
its metadata JSON: the file is indexed once (``CsvWindowSource.open``) and its
first window is returned together with the opened source.  Toggling the Blip
Filter reopens the source so the filter setting is baked into every window.
The result always carries ``window_source`` so the panel can page it later.
"""

from __future__ import annotations

from data_processing.analysis_window_source import CsvWindowSource
from data_processing.queued_background_worker import QueuedBackgroundWorker

FIRST_VIEW_START_S = 0.0


class AnalysisSourceLoadWorker(QueuedBackgroundWorker):
    """Runs window loads in a dedicated thread.

    The queue is bounded to 1: a fresh submission evicts a still-pending
    one, so only the latest requested load is ever parsed.
    """

    def _process(self, payload: dict) -> dict:
        window_source = payload.get("window_source")
        if window_source is not None and "view_start_s" in payload:
            snapshot = window_source.snapshot_for(payload["view_start_s"])
        else:
            window_source = CsvWindowSource.open(
                payload["csv_path"],
                payload["metadata_path"],
                blip_filter_enabled=payload["blip_filter_enabled"],
                blip_filter_window_samples=payload["blip_filter_window_samples"],
            )
            snapshot = window_source.snapshot_for(FIRST_VIEW_START_S)
        return {
            "snapshot": snapshot,
            "window_source": window_source,
            "csv_path": payload.get("csv_path"),
            "metadata_path": payload.get("metadata_path"),
        }
