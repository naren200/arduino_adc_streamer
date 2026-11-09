"""A paused analysis consumer must not pause reception or hide transport faults."""
import queue
import threading

import numpy as np
import pytest

from captest import capture as cap, protocol
from captest.settings import all_channels


class StreamingClient:
    """Full-board frames with a tiny device/driver queue and independent producer."""

    burst_timeout = .1

    def __init__(self, count=1200, fault=None):
        self.config = cap.ScanConfig.for_routes(all_channels(), lag_us=500)
        self.count, self.fault = count, fault
        self.wire = queue.Queue(maxsize=8)
        self.produced = threading.Event()
        self.cancelled = threading.Event()
        self.producer_error = None
        self.readers = set()
        self.resync_readers = []

    def chunk(self, seq, final=False):
        n = 0 if final else 1
        refs = self.config.routes
        # 657 us measured revisit, with one 1.4 us connected interval per route.
        conn = np.array([[seq * 394200 + i * 1800 for i in range(len(refs))]], dtype=np.uint32)[:n]
        sensor = np.array([[(ch << 12) | 2048 for _, ch in refs]], dtype=np.uint16)[:n]
        return cap.encode_chunk(cap.Chunk(1, 2, seq, seq, len(refs),
            cap.FLAG_FINAL | cap.FLAG_STOPPED if final else 0, 0, 600_000_000,
            conn, conn + 840, sensor, np.full(sensor.shape, (15 << 12) | 2048, dtype=np.uint16)))

    def resync(self):
        self.resync_readers.append(len(self.readers))
        assert not self.readers, "cleanup must not compete with a live serial reader"
        self.cancelled.set()
        if hasattr(self, "producer"):
            self.producer.join(1)

    def capture_require(self):
        pass

    def capture_configure(self, config):
        assert config == self.config

    def capture_start(self, max_sweeps):
        self.cancelled.clear()
        self.producer = threading.Thread(target=self._produce, daemon=True)
        self.producer.start()

    def _produce(self):
        try:
            for seq in range(self.count):
                if self.cancelled.is_set():
                    return
                if self.fault == "gap" and seq == 10:
                    continue
                payload = self.chunk(seq)
                if self.fault == "crc" and seq == 10:
                    payload = payload[:-1] + bytes([payload[-1] ^ 1])
                self.wire.put(("chunk", payload), timeout=.5)
        except queue.Full:
            self.producer_error = "small driver queue overflowed because serial was not read"
        finally:
            self.produced.set()

    def capture_items(self, timeout):
        ident = threading.get_ident()
        self.readers.add(ident)
        try:
            while True:
                try:
                    item = self.wire.get(timeout=timeout)
                except queue.Empty:
                    return
                yield item
                if item[0] == "ack":
                    return
        finally:
            self.readers.remove(ident)

    def capture_stop(self):
        assert self.produced.wait(3)
        self.wire.put(("chunk", self.chunk(self.count, final=True)), timeout=1)
        self.wire.put(("ack", True), timeout=1)


def test_receiver_drains_full_board_while_analysis_is_paused_and_replays_exactly(tmp_path):
    client = StreamingClient()
    refs = [(2, i) for i in range(5, 10)]
    session = cap.CaptureSession(client, client.config, refs, background_read=True,
                                 record_path=tmp_path / "fast.cap", poll_timeout=.01)
    session.start()
    try:
        # No poll/decoding at all until the complete 1200-sweep burst has arrived.
        assert client.produced.wait(3)
        assert client.producer_error is None
        assert session.assembler.sweeps == 0
        result = session.stop()                  # drains pending data, final chunk and ack
    finally:
        if not session.closed:
            session.stop()
    assert result.valid, result.reasons()
    assert result.sweeps == client.count
    assert not session._receiver.thread.is_alive()
    assert client.resync_readers == [0, 0]
    assert result.revisit_us[refs[0]] == pytest.approx(657)
    replay = cap.replay_file(result.raw_file, keep_routes=refs)
    assert replay.valid and replay.raw_sha256 == result.raw_sha256
    for ref in refs:
        for key in result.samples[ref]:
            np.testing.assert_array_equal(result.samples[ref][key], replay.samples[ref][key])


@pytest.mark.parametrize("fault,kind", [("gap", cap.GAP), ("crc", cap.CRC)])
def test_background_reception_preserves_transport_rejections(fault, kind):
    client = StreamingClient(count=40, fault=fault)
    session = cap.CaptureSession(client, client.config, [(2, 5)], background_read=True, poll_timeout=.01)
    session.start()
    assert client.produced.wait(3)
    result = session.stop()
    assert not result.valid
    assert any(p.kind == kind for p in result.problems)
    assert not session._receiver.thread.is_alive()


def test_bounded_receive_queue_fails_explicitly_and_releases_serial_owner():
    client = StreamingClient(count=4)
    session = cap.CaptureSession(client, client.config, [(2, 5)], background_read=True,
                                 receive_queue_bytes=10, poll_timeout=.01)
    session.start()
    assert session._receiver.thread is not None
    session._receiver.thread.join(2)
    session.poll()
    result = session.stop()
    assert not result.valid
    assert any(p.kind == cap.LINK and "receive queue full" in p.detail for p in result.problems)
    assert client.resync_readers == [0, 0]
    assert not session._receiver.thread.is_alive()


def test_receiver_propagates_reader_exception_without_hanging_cleanup():
    client = StreamingClient(count=0)

    def broken(timeout):
        raise protocol.TruncatedPacketError("partial capture header")
        yield  # generator interface

    client.capture_items = broken
    session = cap.CaptureSession(client, client.config, [(2, 5)], background_read=True, poll_timeout=.01)
    session.start()
    session._receiver.thread.join(2)
    session.poll()
    result = session.stop()
    assert not result.valid
    assert any(p.kind == cap.LINK and "partial capture header" in p.detail for p in result.problems)
    assert not session._receiver.thread.is_alive()


def test_background_capture_uses_real_client_framing_and_can_restart(tmp_path):
    from captest.client import CapTestClient
    from captest.link import VirtualClock
    from captest.settings import Settings
    from captest.sim import SimConfig, SimLink, SimulatedDevice

    settings = Settings()
    device = SimulatedDevice(SimConfig(firmware_version="1.4.0"), clock=VirtualClock(),
                             r_bias_table=settings.bias)
    client = CapTestClient(SimLink(device))
    config = cap.ScanConfig.for_routes(all_channels(), lag_us=500)
    for run in range(2):
        session = cap.CaptureSession(client, config, [(2, 5)], background_read=True,
                                     record_path=tmp_path / ("run%d.cap" % run), poll_timeout=.01)
        session.start(max_sweeps=180)
        try:
            for _ in range(20):
                session.poll()
                if not session.running:
                    break
            result = session.stop()
        finally:
            if not session.closed:
                session.stop()
        assert result.valid, result.reasons()
        assert result.sweeps == 180
        assert not session._receiver.thread.is_alive()
        assert device.parked and not device.scan.streaming
        replay = cap.replay_file(result.raw_file, keep_routes=[(2, 5)])
        assert replay.valid and replay.sweeps == result.sweeps
