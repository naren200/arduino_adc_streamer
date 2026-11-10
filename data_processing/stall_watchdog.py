"""Diagnostics for host stalls during capture.

The GUI thread calls ``StallWatchdog.beat`` from a short QTimer. When the beat
stops for longer than the threshold, the watchdog thread writes the stack of
every Python thread to a file, which shows what the GUI thread, decoder and
acquisition worker were blocked on. Dumps are rate limited and bounded.
"""
from datetime import datetime
from pathlib import Path
import sys
import threading
import time
import traceback

DUMP_DIRECTORY_NAME = 'stall_dumps'
_file_lock = threading.Lock()
_session_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')


def dump_path():
    return Path.home() / '.adc_streamer' / DUMP_DIRECTORY_NAME / f'stalls_{_session_stamp}.log'


def dump_all_threads(reason, path=None):
    """Append every thread's current stack to the dump file; return its path or None."""
    target = Path(path) if path is not None else dump_path()
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    lines = [f'=== {datetime.now().strftime("%H:%M:%S.%f")[:-3]} {reason}\n']
    skip = threading.get_ident() if threading.current_thread().name == 'StallWatchdog' else None
    for ident, frame in sys._current_frames().items():
        if ident == skip:
            continue
        lines.append(f'--- thread {names.get(ident, "?")} ({ident})\n')
        lines.extend(traceback.format_stack(frame))
    try:
        with _file_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, 'a', encoding='utf-8') as handle:
                handle.write(''.join(lines) + '\n')
    except OSError:
        return None
    return target


class StallWatchdog(threading.Thread):
    def __init__(self, threshold_s=0.3, min_interval_s=1.0, max_dumps=40, path=None):
        super().__init__(name='StallWatchdog', daemon=True)
        self.threshold_s = threshold_s
        self.min_interval_s = min_interval_s
        self.max_dumps = max_dumps
        self.path = path
        self.dumps = 0
        self._last_beat = time.perf_counter()
        self._last_dump = 0.0
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._messages = []

    def beat(self):
        self._last_beat = time.perf_counter()

    def stop(self):
        self._stop_event.set()

    def pop_messages(self):
        with self._lock:
            messages, self._messages = self._messages, []
        return messages

    def run(self):
        while not self._stop_event.wait(0.05) and self.dumps < self.max_dumps:
            now = time.perf_counter()
            age = now - self._last_beat
            if age < self.threshold_s or now - self._last_dump < self.min_interval_s:
                continue
            self._last_dump = now
            self.dumps += 1
            target = dump_all_threads(f'GUI thread unresponsive for {age * 1000:.0f} ms', self.path)
            with self._lock:
                self._messages.append(f'GUI thread stalled {age * 1000:.0f} ms; thread stacks written to {target}')
