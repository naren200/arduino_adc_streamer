"""Serial receive buffering must tolerate host work and unsupported drivers."""
from types import SimpleNamespace

import pytest

from captest.link import SerialLink


@pytest.mark.parametrize("support", ["supported", "unsupported", "absent"])
def test_receive_buffer_request_is_best_effort_without_blocking_port_open(monkeypatch, support):
    serial = pytest.importorskip("serial")
    calls = []
    port = SimpleNamespace(close=lambda: None)
    if support != "absent":
        def resize(**kwargs):
            calls.append(kwargs)
            if support == "unsupported":
                raise OSError("driver does not support queue resizing")
        port.set_buffer_size = resize
    monkeypatch.setattr(serial, "Serial", lambda *a, **kw: port)
    link = SerialLink("test-port")
    assert link._serial is port
    assert link.receive_buffer_requested == (1024 * 1024 if support == "supported" else None)
    if support != "absent":
        assert calls == [{"rx_size": 1024 * 1024, "tx_size": 4096}]
    link.close()
