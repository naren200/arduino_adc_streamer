"""
Analysis Compute Worker
========================
Background worker that runs prepare_analysis_data() off the GUI thread so
switching to the Analysis tab, or changing its controls, never blocks the UI.
"""

from __future__ import annotations

from data_processing.analysis_workbench import prepare_analysis_data
from data_processing.gate_replay import compute_gate_replay
from data_processing.queued_background_worker import QueuedBackgroundWorker


class AnalysisComputeWorker(QueuedBackgroundWorker):
    """Runs prepare_analysis_data() (and the optional gate replay) in a dedicated thread.

    The queue is bounded to 1: a fresh submission evicts a still-pending
    one, so only the latest requested render is ever computed.
    """

    def _process(self, payload: dict) -> dict:
        prepared = prepare_analysis_data(
            payload["snapshot"],
            axis_mode=payload["axis_mode"],
            visible_labels=payload["visible_labels"],
            filter_enabled=payload["filter_enabled"],
            filter_settings=payload["filter_settings"],
            overlay_flags=payload["overlay_flags"],
            vref_voltage=payload["vref_voltage"],
            integration_window_samples=payload["integration_window_samples"],
            hpf_cutoff_hz=payload["hpf_cutoff_hz"],
            pzt_force_settings=payload["pzt_force_settings"],
        )
        replay_setup = payload.get("gate_replay_setup")
        if replay_setup is not None:
            prepared.gating = compute_gate_replay(payload["snapshot"], replay_setup)
        return {"prepared": prepared, "auto_range": payload.get("auto_range", False)}
