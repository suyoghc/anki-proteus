"""
SQLite-backed cache for pre-generated question variants.

Stores multiple variants per card, tracks which have been used,
and handles cleanup of stale entries.

Thread-safe: uses check_same_thread=False and a lock so the
prefetch QThread can write without raising ProgrammingError.

Schema versioning uses PRAGMA user_version to track migrations.
"""

import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

_SCHEMA_VERSION = 6


class VariantCache:
    """Cache for LLM-generated question variants."""

    def __init__(self, addon_dir: str, max_variants: int = 3):
        self._db_path = os.path.join(addon_dir, "variant_cache.db")
        self._max_variants = max_variants
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._migrate()

    def _get_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _set_version(self, version: int):
        self._conn.execute(f"PRAGMA user_version = {int(version)}")

    def _migrate(self):
        with self._lock:
            version = self._get_version()

            if version < 1:
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS variants (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        card_id INTEGER NOT NULL,
                        variant_text TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        used INTEGER DEFAULT 0,
                        rating INTEGER DEFAULT NULL
                    )
                """)
                self._conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_card_id
                    ON variants (card_id, used)
                """)

            if version < 2:
                # v1 DBs have variants but no rating column
                if version == 1:
                    try:
                        self._conn.execute(
                            "ALTER TABLE variants ADD COLUMN rating INTEGER DEFAULT NULL"
                        )
                    except sqlite3.OperationalError:
                        pass
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS card_ideas (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        card_id INTEGER NOT NULL,
                        variant_text TEXT NOT NULL,
                        original_question TEXT NOT NULL,
                        original_answer TEXT NOT NULL,
                        rating INTEGER DEFAULT NULL,
                        created_at REAL NOT NULL,
                        used INTEGER DEFAULT 0
                    )
                """)
                self._conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_card_ideas_used
                    ON card_ideas (used)
                """)

            if version < 3:
                try:
                    self._conn.execute(
                        "ALTER TABLE card_ideas ADD COLUMN evaluation TEXT DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass

            if version < 4:
                try:
                    self._conn.execute(
                        "ALTER TABLE card_ideas ADD COLUMN edited_variant_text TEXT DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass

            if version < 5:
                try:
                    self._conn.execute(
                        "ALTER TABLE card_ideas ADD COLUMN decision_status TEXT DEFAULT 'pending'"
                    )
                except sqlite3.OperationalError:
                    pass
                try:
                    self._conn.execute(
                        "ALTER TABLE card_ideas ADD COLUMN decision_reason TEXT DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass

            if version < 6:
                try:
                    self._conn.execute(
                        "ALTER TABLE card_ideas ADD COLUMN edited_answer_text TEXT DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass

            if version < _SCHEMA_VERSION:
                self._set_version(_SCHEMA_VERSION)

            self._conn.commit()

    def get_variant(self, card_id: int) -> Optional[Tuple[int, str]]:
        """
        Get an unused variant for a card. Marks it as used.
        Returns (variant_id, text) or None if no cached variants available.
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
                return (variant_id, text)
            return None

    def record_feedback(self, variant_id: int, rating: int):
        """Store user feedback for a variant. rating: 1 (up) or -1 (down)."""
        with self._lock:
            self._conn.execute(
                "UPDATE variants SET rating = ? WHERE id = ?",
                (rating, variant_id),
            )
            self._conn.commit()

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

    # ------------------------------------------------------------------
    # Card ideas
    # ------------------------------------------------------------------

    def save_idea(self, card_id, variant_text, original_question,
                  original_answer, rating=None, evaluation=None,
                  edited_variant_text=None, edited_answer_text=None):
        # type: (int, str, str, str, Optional[int], Optional[str], Optional[str], Optional[str]) -> int
        """Save a card idea. Returns the new row id."""
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO card_ideas
                   (card_id, variant_text, original_question,
                    original_answer, rating, created_at, used, evaluation,
                    edited_variant_text, edited_answer_text,
                    decision_status, decision_reason)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'pending', NULL)""",
                (card_id, variant_text, original_question,
                 original_answer, rating, time.time(), evaluation,
                 edited_variant_text, edited_answer_text),
            )
            self._conn.commit()
            return cursor.lastrowid

    def get_ideas(self, include_used=False):
        # type: (bool) -> List[Dict]
        """Return saved ideas as a list of dicts, newest first."""
        with self._lock:
            if include_used:
                rows = self._conn.execute(
                    "SELECT id, card_id, variant_text, original_question, "
                    "original_answer, rating, created_at, used, evaluation, "
                    "edited_variant_text, edited_answer_text, "
                    "decision_status, decision_reason "
                    "FROM card_ideas ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, card_id, variant_text, original_question, "
                    "original_answer, rating, created_at, used, evaluation, "
                    "edited_variant_text, edited_answer_text, "
                    "decision_status, decision_reason "
                    "FROM card_ideas WHERE used = 0 ORDER BY created_at DESC"
                ).fetchall()
            cols = [
                "id", "card_id", "variant_text", "original_question",
                "original_answer", "rating", "created_at", "used",
                "evaluation", "edited_variant_text", "edited_answer_text",
                "decision_status",
                "decision_reason",
            ]
            return [dict(zip(cols, row)) for row in rows]

    def update_idea_edit(self, idea_id, edited_variant_text):
        # type: (int, str) -> None
        """Persist the latest human-edited wording for an idea."""
        with self._lock:
            self._conn.execute(
                "UPDATE card_ideas SET edited_variant_text = ? WHERE id = ?",
                (edited_variant_text, idea_id),
            )
            self._conn.commit()

    def update_idea_answer_edit(self, idea_id, edited_answer_text):
        # type: (int, str) -> None
        """Persist the latest human-edited answer wording for an idea."""
        with self._lock:
            self._conn.execute(
                "UPDATE card_ideas SET edited_answer_text = ? WHERE id = ?",
                (edited_answer_text, idea_id),
            )
            self._conn.commit()

    def set_idea_decision(self, idea_id, decision_status, decision_reason=None,
                          mark_used=False):
        # type: (int, str, Optional[str], bool) -> None
        """Store human triage label for an idea (accept/edit/reject)."""
        with self._lock:
            if mark_used:
                self._conn.execute(
                    "UPDATE card_ideas "
                    "SET decision_status = ?, decision_reason = ?, used = 1 "
                    "WHERE id = ?",
                    (decision_status, decision_reason, idea_id),
                )
            else:
                self._conn.execute(
                    "UPDATE card_ideas "
                    "SET decision_status = ?, decision_reason = ? "
                    "WHERE id = ?",
                    (decision_status, decision_reason, idea_id),
                )
            self._conn.commit()

    def mark_idea_used(self, idea_id):
        # type: (int) -> None
        """Mark an idea as used (dismissed or created)."""
        with self._lock:
            self._conn.execute(
                "UPDATE card_ideas SET used = 1 WHERE id = ?",
                (idea_id,),
            )
            self._conn.commit()

    def count_unseen_ideas(self):
        # type: () -> int
        """Count ideas that haven't been used yet."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM card_ideas WHERE used = 0"
            )
            return cursor.fetchone()[0]

    def delete_idea(self, idea_id):
        # type: (int) -> None
        """Permanently delete an idea."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM card_ideas WHERE id = ?",
                (idea_id,),
            )
            self._conn.commit()

    def close(self):
        """Close the database connection."""
        with self._lock:
            self._conn.close()
