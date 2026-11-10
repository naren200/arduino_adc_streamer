"""Stall watchdog writes thread stacks when the beat stops, and stays quiet otherwise."""
import threading
import time

from data_processing.stall_watchdog import StallWatchdog, dump_all_threads


def test_dump_lists_every_thread_with_reason(tmp_path):
    release = threading.Event()
    blocked = threading.Thread(target=release.wait, name='BlockedWorker', daemon=True)
    blocked.start()
    path = dump_all_threads('unit test reason', tmp_path / 'dump.log')
    release.set()
    text = path.read_text()
    assert 'unit test reason' in text and 'BlockedWorker' in text


def test_watchdog_dumps_only_when_beat_stops(tmp_path):
    path = tmp_path / 'stalls.log'
    watchdog = StallWatchdog(threshold_s=0.1, min_interval_s=0.05, max_dumps=2, path=path)
    watchdog.start()
    deadline = time.perf_counter() + 0.4
    while time.perf_counter() < deadline:
        watchdog.beat()
        time.sleep(0.01)
    assert watchdog.dumps == 0 and not path.exists()
    time.sleep(0.6)
    watchdog.stop()
    watchdog.join(2)
    assert watchdog.dumps == 2
    messages = watchdog.pop_messages()
    assert len(messages) == 2 and 'GUI thread stalled' in messages[0]
    text = path.read_text()
    assert text.count('=== ') == 2 and f'({watchdog.ident})' not in text
    assert watchdog.pop_messages() == []
