import os
import unittest

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from data_processing.force_block_worker import ForceBlockBatch, ForceBlockWorker
from data_processing.pressure_force_display import PressureForceDisplayEngine
from data_processing.pressure_map_geometry import PressureMapGeometry


class CountingEngine(PressureForceDisplayEngine):
    def __init__(self):
        super().__init__(geometry=PressureMapGeometry())
        self.configure_layout({"PZT1": (0, 0)})
        self.reset_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1
        super().reset()


def make_batch(rows: int = 1) -> ForceBlockBatch:
    return ForceBlockBatch(
        sweeps=np.zeros((rows, 1), dtype=np.float64),
        timestamps=np.zeros(rows, dtype=np.float64),
        first_sweep_id=0,
        dt_s=0.001,
        voltage_scale=1.0,
        ghost_net_centered=False,
        repeat_slots=1,
        complete_packages={},
        baselines={},
        polarity_multiplier=1.0,
        mux_timing=None,
        jerk_shapes=None,
        dropped_sweeps_before=0,
        grid_positions={},
        channel_calibration={},
    )


class ForceBlockWorkerControlItemTests(unittest.TestCase):
    """The worker is never started: a busy worker is modelled by not draining until asserted."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.old_engine = CountingEngine()
        self.worker = ForceBlockWorker(self.old_engine)
        self.results = QSignalSpy(self.worker.result_ready)

    def test_engine_swap_survives_a_following_batch(self):
        new_engine = CountingEngine()

        self.worker.swap_engine(new_engine)
        self.worker.enqueue_batch(make_batch())
        self.worker.drain_synchronous()

        self.assertIs(self.worker._engine, new_engine)
        self.assertEqual(len(self.results), 1)

    def test_reset_survives_a_following_batch(self):
        self.worker.send_reset()
        self.worker.enqueue_batch(make_batch())
        self.worker.drain_synchronous()

        self.assertEqual(self.old_engine.reset_calls, 1)
        self.assertEqual(len(self.results), 1)

    def test_reset_does_not_discard_a_pending_engine_swap(self):
        new_engine = CountingEngine()

        self.worker.swap_engine(new_engine)
        self.worker.send_reset()
        self.worker.drain_synchronous()

        self.assertIs(self.worker._engine, new_engine)
        self.assertEqual(new_engine.reset_calls, 1)

    def test_newer_batch_replaces_pending_batch_and_reports_dropped_sweeps(self):
        self.worker.enqueue_batch(make_batch(rows=3))

        dropped = self.worker.enqueue_batch(make_batch(rows=2))
        self.worker.drain_synchronous()

        self.assertEqual(dropped, 3)
        self.assertEqual(len(self.results), 1)

    def test_engine_swap_discards_a_pending_batch_of_the_old_layout(self):
        new_engine = CountingEngine()

        self.worker.enqueue_batch(make_batch())
        self.worker.swap_engine(new_engine)
        self.worker.drain_synchronous()

        self.assertIs(self.worker._engine, new_engine)
        self.assertEqual(len(self.results), 0)


if __name__ == "__main__":
    unittest.main()
