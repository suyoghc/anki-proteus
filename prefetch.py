"""
Background thread for pre-generating variants.

Uses QThread so it integrates with Anki's Qt event loop without
blocking the reviewer UI.
"""

from aqt.qt import QThread, pyqtSignal
from .generator import generate_variant


class PrefetchWorker(QThread):
    """
    Background worker that generates a variant for an upcoming card.

    Runs in a separate thread to avoid blocking the review UI.
    Stores the result directly in the cache when done.
    """

    finished = pyqtSignal(int, str)  # card_id, variant_text

    def __init__(
        self,
        card_id: int,
        question: str,
        answer: str,
        config: dict,
        cache,  # VariantCache instance
    ):
        super().__init__()
        self._card_id = card_id
        self._question = question
        self._answer = answer
        self._config = config
        self._cache = cache

    def run(self):
        """Generate variant in background thread."""
        try:
            variant = generate_variant(
                question=self._question,
                answer=self._answer,
                config=self._config,
            )
            if variant:
                self._cache.store_variant(self._card_id, variant)
                self.finished.emit(self._card_id, variant)
        except Exception as e:
            print(f"[Proteus] Prefetch failed: {e}")
