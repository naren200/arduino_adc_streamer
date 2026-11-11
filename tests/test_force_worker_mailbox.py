import threading
import time
import unittest

from data_processing.force_worker_mailbox import ForceWorkMailbox


class ForceWorkMailboxTests(unittest.TestCase):
    def setUp(self):
        self.mailbox = ForceWorkMailbox()

    def test_empty_mailbox_returns_none(self):
        self.assertIsNone(self.mailbox.take())

    def test_newer_batch_replaces_pending_and_returns_the_replaced_one(self):
        self.assertIsNone(self.mailbox.put_batch("first"))

        self.assertEqual(self.mailbox.put_batch("second"), "first")

        self.assertEqual(self.mailbox.take(), "second")
        self.assertIsNone(self.mailbox.take())

    def test_control_items_are_never_replaced_by_batches_and_come_first(self):
        self.mailbox.post_control("swap")
        self.mailbox.put_batch("batch")
        self.mailbox.post_control("reset")
        self.mailbox.put_batch("newer batch")

        self.assertEqual([self.mailbox.take() for _ in range(3)], ["swap", "reset", "newer batch"])

    def test_posting_control_discards_the_pending_batch(self):
        self.mailbox.put_batch("old layout")

        self.mailbox.post_control("swap")

        self.assertEqual(self.mailbox.take(), "swap")
        self.assertIsNone(self.mailbox.take())

    def test_drop_pending_batch_keeps_control_items(self):
        self.mailbox.post_control("swap")
        self.mailbox.put_batch("batch")

        self.mailbox.drop_pending_batch()

        self.assertEqual(self.mailbox.take(), "swap")
        self.assertIsNone(self.mailbox.take())

    def test_take_times_out_when_nothing_arrives(self):
        started = time.perf_counter()

        self.assertIsNone(self.mailbox.take(timeout=0.05))

        self.assertGreaterEqual(time.perf_counter() - started, 0.04)

    def test_take_wakes_when_another_thread_posts(self):
        producer = threading.Timer(0.05, self.mailbox.put_batch, args=("late batch",))
        producer.start()
        self.addCleanup(producer.join)

        self.assertEqual(self.mailbox.take(timeout=2.0), "late batch")


if __name__ == "__main__":
    unittest.main()
