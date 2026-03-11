"""
Batch pre-generation of variants at review session start.

Spawns up to `max_concurrent` QThread workers that pull work items
from a shared queue, generate variants, and store them in the cache.
All collection access (card text extraction) happens on the main thread
before items are enqueued — workers only receive plain strings.

Progress is tracked via a main-thread QTimer polling a thread-safe
counter — no cross-thread Qt signals, which avoids reentrant event
processing crashes.
"""

import os
import queue
import threading
import time as _time
from typing import List, Optional

from aqt.qt import QObject, QThread, QTimer, pyqtSignal

from .generator import generate_variant
from .cache import VariantCache

_LOG_PATH = os.path.join(os.path.dirname(__file__), "proteus_diag.log")

def _log(msg, debug=True):
    if not debug:
        return
    ts = _time.strftime("%H:%M:%S")
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


class _BatchWorker(QThread):
    """Worker thread that pulls items from a shared queue and generates variants."""

    def __init__(self, work_queue, cache, config, cancel_event, completed_counter, counter_lock, debug=False, parent=None):
        # type: (queue.Queue, VariantCache, dict, threading.Event, list, threading.Lock, bool, Optional[QObject]) -> None
        super().__init__(parent)
        self._queue = work_queue
        self._cache = cache
        self._config = config
        self._cancel = cancel_event
        self._completed = completed_counter  # [count] — shared mutable list
        self._counter_lock = counter_lock
        self._debug = debug

    def run(self):
        while not self._cancel.is_set():
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break

            card_id, question, answer = item

            # Another worker (or single-card prefetch) may have filled this
            if self._cache.has_variant(card_id):
                _log(f"worker: card {card_id} already cached, skipping", self._debug)
                with self._counter_lock:
                    self._completed[0] += 1
                continue

            try:
                variant = generate_variant(
                    question=question,
                    answer=answer,
                    config=self._config,
                )
                if variant:
                    self._cache.store_variant(card_id, variant)
                    _log(f"worker: card {card_id} done ({len(variant)} chars)", self._debug)
                else:
                    _log(f"worker: card {card_id} generation returned None", self._debug)
            except Exception as e:
                _log(f"worker error for card {card_id}: {e}", self._debug)

            with self._counter_lock:
                self._completed[0] += 1


class BatchPrefetchManager(QObject):
    """
    Manages batch pre-generation of variants for upcoming review cards.

    Enqueue work items (card_id, question, answer) then call start().
    Workers run in parallel up to max_concurrent.

    Progress is polled from the main thread via QTimer — no cross-thread
    signal emission, which avoids Qt reentrant event loop crashes.
    """

    progress = pyqtSignal(int, int)  # (completed, total)
    all_done = pyqtSignal()

    def __init__(self, cache, config, max_concurrent=3, debug=False, parent=None):
        # type: (VariantCache, dict, int, bool, Optional[QObject]) -> None
        super().__init__(parent)
        self._cache = cache
        self._config = config
        self._max_concurrent = max_concurrent
        self._debug = debug
        self._queue = queue.Queue()  # type: queue.Queue
        self._cancel_event = threading.Event()
        self._workers = []  # type: List[_BatchWorker]
        self._total = 0
        self._completed = [0]  # mutable counter shared with workers
        self._counter_lock = threading.Lock()
        self._last_reported = 0
        self._poll_timer = None  # type: Optional[QTimer]

    def enqueue(self, card_id, question, answer):
        # type: (int, str, str) -> None
        """Add a work item. Call before start()."""
        self._queue.put((card_id, question, answer))
        self._total += 1

    def start(self):
        """Spawn workers and begin processing the queue."""
        if self._total == 0:
            self.all_done.emit()
            return

        num_workers = min(self._max_concurrent, self._total)
        for _ in range(num_workers):
            worker = _BatchWorker(
                self._queue, self._cache, self._config,
                self._cancel_event, self._completed, self._counter_lock,
                debug=self._debug, parent=self,
            )
            self._workers.append(worker)

        for w in self._workers:
            w.start()

        # Poll progress from main thread every 500ms
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_progress)
        self._poll_timer.start(500)

    def cancel(self):
        """Stop workers after their current item finishes."""
        self._cancel_event.set()
        if self._poll_timer:
            self._poll_timer.stop()
            self._poll_timer = None
        # Drain the queue so workers exit promptly
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        # Wait for workers to finish so they aren't destroyed while running.
        # API calls can block up to 15s, so allow 20s per worker.
        for w in self._workers:
            w.wait(20000)

    def _poll_progress(self):
        """Called on main thread by QTimer. Check worker progress."""
        with self._counter_lock:
            completed = self._completed[0]

        if completed != self._last_reported:
            self._last_reported = completed
            self.progress.emit(completed, self._total)

        if completed >= self._total or all(w.isFinished() for w in self._workers):
            self._poll_timer.stop()
            self._poll_timer = None
            self.all_done.emit()
