"""
SQLite-backed cache for pre-generated question variants.

Stores multiple variants per card, tracks which have been used,
and handles cleanup of stale entries.

Thread-safe: uses check_same_thread=False and a lock so the
prefetch QThread can write without raising ProgrammingError.
"""

import os
import sqlite3
import threading
import time
from typing import Optional


class VariantCache:
    """Cache for LLM-generated question variants."""

    def __init__(self, addon_dir: str, max_variants: int = 3):
        self._db_path = os.path.join(addon_dir, "variant_cache.db")
        self._max_variants = max_variants
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._create_tables()

    def _create_tables(self):
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS variants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    card_id INTEGER NOT NULL,
                    variant_text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    used INTEGER DEFAULT 0
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_card_id
                ON variants (card_id, used)
            """)
            self._conn.commit()

    def get_variant(self, card_id: int) -> Optional[str]:
        """
        Get an unused variant for a card. Marks it as used.
        Returns None if no cached variants available.
        """
        with self._lock:
            cursor = self._conn.execute(
                """SELECT id, variant_text FROM variants
                   WHERE card_id = ? AND used = 0
                   ORDER BY created_at ASC
                   LIMIT 1""",
                (card_id,),
            )
            row = cursor.fetchone()
            if row:
                variant_id, text = row
                self._conn.execute(
                    "UPDATE variants SET used = 1 WHERE id = ?",
                    (variant_id,),
                )
                self._conn.commit()
                return text
            return None

    def has_variant(self, card_id: int) -> bool:
        """Check if there are unused variants cached for a card."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT 1 FROM variants WHERE card_id = ? AND used = 0 LIMIT 1",
                (card_id,),
            )
            return cursor.fetchone() is not None

    def store_variant(self, card_id: int, variant_text: str):
        """Store a generated variant, enforcing the max per card."""
        with self._lock:
            # Check if we're at the limit for unused variants
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM variants WHERE card_id = ? AND used = 0",
                (card_id,),
            )
            count = cursor.fetchone()[0]
            if count >= self._max_variants:
                return  # already have enough cached

            self._conn.execute(
                """INSERT INTO variants (card_id, variant_text, created_at, used)
                   VALUES (?, ?, ?, 0)""",
                (card_id, variant_text, time.time()),
            )
            self._conn.commit()

    def count_unused(self, card_id: int) -> int:
        """Count unused variants for a card."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM variants WHERE card_id = ? AND used = 0",
                (card_id,),
            )
            return cursor.fetchone()[0]

    def cleanup(self, max_age_days: int = 30):
        """Remove old used variants to keep the database small."""
        cutoff = time.time() - (max_age_days * 86400)
        with self._lock:
            self._conn.execute(
                "DELETE FROM variants WHERE used = 1 AND created_at < ?",
                (cutoff,),
            )
            self._conn.commit()

    def clear_card(self, card_id: int):
        """Remove all cached variants for a specific card."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM variants WHERE card_id = ?",
                (card_id,),
            )
            self._conn.commit()

    def clear_all(self):
        """Remove all cached variants."""
        with self._lock:
            self._conn.execute("DELETE FROM variants")
            self._conn.commit()

    def close(self):
        """Close the database connection."""
        with self._lock:
            self._conn.close()
