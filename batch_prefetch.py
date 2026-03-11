"""
Batch pre-generation of variants at review session start.

Spawns up to `max_concurrent` QThread workers that pull work items
from a shared queue, generate variants, and store them in the cache.
All collection access (card text extraction) happens on the main thread
before items are enqueued — workers only receive plain strings.
"""

import queue
import threading
from typing import List, Optional

from aqt.qt import QObject, QThread, pyqtSignal

from .generator import generate_variant
from .cache import VariantCache


class _BatchWorker(QThread):
    """Worker thread that pulls items from a shared queue and generates variants."""

    item_done = pyqtSignal(int)  # card_id

    def __init__(self, work_queue, cache, config, cancel_event, parent=None):
        # type: (queue.Queue, VariantCache, dict, threading.Event, Optional[QObject]) -> None
        super().__init__(parent)
        self._queue = work_queue
        self._cache = cache
        self._config = config
        self._cancel = cancel_event

    def run(self):
        while not self._cancel.is_set():
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break

            card_id, question, answer = item

            # Another worker (or single-card prefetch) may have filled this
            if self._cache.has_variant(card_id):
                self.item_done.emit(card_id)
                continue

            try:
                variant = generate_variant(
                    question=question,
                    answer=answer,
                    config=self._config,
                )
                if variant:
                    self._cache.store_variant(card_id, variant)
            except Exception as e:
                print(f"[Proteus] Batch worker error for card {card_id}: {e}")

            self.item_done.emit(card_id)


class BatchPrefetchManager(QObject):
    """
    Manages batch pre-generation of variants for upcoming review cards.

    Enqueue work items (card_id, question, answer) then call start().
    Workers run in parallel up to max_concurrent.
    """

    progress = pyqtSignal(int, int)  # (completed, total)
    all_done = pyqtSignal()

    def __init__(self, cache, config, max_concurrent=3, parent=None):
        # type: (VariantCache, dict, int, Optional[QObject]) -> None
        super().__init__(parent)
        self._cache = cache
        self._config = config
        self._max_concurrent = max_concurrent
        self._queue = queue.Queue()  # type: queue.Queue
        self._cancel_event = threading.Event()
        self._workers = []  # type: List[_BatchWorker]
        self._total = 0
        self._completed = 0
        self._lock = threading.Lock()

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
                self._cancel_event, parent=self,
            )
            worker.item_done.connect(self._on_item_done)
            worker.finished.connect(self._on_worker_finished)
            self._workers.append(worker)

        for w in self._workers:
            w.start()

    def cancel(self):
        """Stop workers after their current item finishes."""
        self._cancel_event.set()
        # Drain the queue so workers exit promptly
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _on_item_done(self, card_id):
        # type: (int) -> None
        with self._lock:
            self._completed += 1
            completed = self._completed
            total = self._total
        self.progress.emit(completed, total)

    def _on_worker_finished(self):
        """Check if all workers are done."""
        all_finished = all(w.isFinished() for w in self._workers)
        if all_finished:
            self.all_done.emit()
