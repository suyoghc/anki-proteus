"""
SQLite-backed cache for pre-generated question variants.

Stores multiple variants per card, tracks which have been used,
and handles cleanup of stale entries.

Thread-safe via per-thread connections and WAL mode. Each calling thread
(main UI, prefetch workers) gets its own sqlite3.Connection lazily; SQLite's
own file-level locking serializes writes. An application lock is held only
around check-then-act sequences (count + insert, select + update-used) so
concurrent workers can't produce duplicate writes.

Schema versioning uses PRAGMA user_version to track migrations.
"""

import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

_SCHEMA_VERSION = 9


class VariantCache:
    """Cache for LLM-generated question variants."""

    def __init__(self, addon_dir: str, max_variants: int = 3):
        self._db_path = os.path.join(addon_dir, "variant_cache.db")
        self._max_variants = max_variants
        # Serializes check-then-act sequences only. Single-statement ops
        # rely on SQLite's own locking and do NOT take this lock.
        self._lock = threading.Lock()
        # Per-thread connections — each thread gets its own sqlite3.Connection
        # on first use (see _conn()). Replaces the prior single-connection +
        # check_same_thread=False pattern, which is officially undefined
        # behaviour even with an external lock.
        self._local = threading.local()
        self._migrate()

    def _conn(self) -> sqlite3.Connection:
        """Return this thread's sqlite3 connection, creating it on first use.

        Enables WAL so concurrent readers aren't blocked by writers, and sets
        a short busy_timeout so brief contention doesn't raise OperationalError.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self._db_path)
        # WAL is persistent per database — setting it on any connection is
        # idempotent. We also set it here so a brand-new DB gets WAL from the
        # very first connection.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=2000")
        self._local.conn = conn
        return conn

    def _migrate(self):
        conn = self._conn()
        with self._lock:
            version = conn.execute("PRAGMA user_version").fetchone()[0]

            if version < 1:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS variants (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        card_id INTEGER NOT NULL,
                        variant_text TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        used INTEGER DEFAULT 0,
                        rating INTEGER DEFAULT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_card_id
                    ON variants (card_id, used)
                """)

            if version < 2:
                # v1 DBs have variants but no rating column
                if version == 1:
                    try:
                        conn.execute(
                            "ALTER TABLE variants ADD COLUMN rating INTEGER DEFAULT NULL"
                        )
                    except sqlite3.OperationalError:
                        pass
                conn.execute("""
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
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_card_ideas_used
                    ON card_ideas (used)
                """)

            if version < 3:
                try:
                    conn.execute(
                        "ALTER TABLE card_ideas ADD COLUMN evaluation TEXT DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass

            if version < 4:
                try:
                    conn.execute(
                        "ALTER TABLE card_ideas ADD COLUMN edited_variant_text TEXT DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass

            if version < 5:
                try:
                    conn.execute(
                        "ALTER TABLE card_ideas ADD COLUMN decision_status TEXT DEFAULT 'pending'"
                    )
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute(
                        "ALTER TABLE card_ideas ADD COLUMN decision_reason TEXT DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass

            if version < 6:
                try:
                    conn.execute(
                        "ALTER TABLE card_ideas ADD COLUMN edited_answer_text TEXT DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass

            if version < 7:
                try:
                    conn.execute(
                        "ALTER TABLE variants ADD COLUMN expected_answer TEXT DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass

            if version < 8:
                try:
                    conn.execute(
                        "ALTER TABLE variants ADD COLUMN variant_style TEXT DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass

            if version < 9:
                try:
                    conn.execute(
                        "ALTER TABLE variants ADD COLUMN svg TEXT DEFAULT NULL"
                    )
                except sqlite3.OperationalError:
                    pass

            if version < _SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {int(_SCHEMA_VERSION)}")

            conn.commit()

    def get_variant(self, card_id: int) -> Optional[Tuple[int, str, str, str, str]]:
        """
        Get an unused variant for a card. Marks it as used.
        Returns (variant_id, text, expected_answer, variant_style, svg) or None.

        Locked: select + update-used is check-then-act and must be atomic.
        """
        conn = self._conn()
        with self._lock:
            cursor = conn.execute(
                """SELECT id, variant_text, expected_answer, variant_style, svg
                   FROM variants
                   WHERE card_id = ? AND used = 0
                   ORDER BY created_at ASC
                   LIMIT 1""",
                (card_id,),
            )
            row = cursor.fetchone()
            if row:
                variant_id, text, expected_answer, variant_style, svg = row
                conn.execute(
                    "UPDATE variants SET used = 1 WHERE id = ?",
                    (variant_id,),
                )
                conn.commit()
                return (variant_id, text, expected_answer or "",
                        variant_style or "", svg or "")
            return None

    def get_variant_by_id(self, variant_id: int) -> Optional[Tuple[int, str, Optional[int], str]]:
        """Look up a variant by row id. Returns (card_id, variant_text, rating, expected_answer) or None."""
        row = self._conn().execute(
            "SELECT card_id, variant_text, rating, expected_answer FROM variants WHERE id = ?",
            (variant_id,),
        ).fetchone()
        if row:
            return (row[0], row[1], row[2], row[3] or "")
        return None

    def record_feedback(self, variant_id: int, rating: int):
        """Store user feedback for a variant. rating: 1 (up) or -1 (down)."""
        conn = self._conn()
        conn.execute(
            "UPDATE variants SET rating = ? WHERE id = ?",
            (rating, variant_id),
        )
        conn.commit()

    def has_variant(self, card_id: int) -> bool:
        """Check if there are unused variants cached for a card."""
        cursor = self._conn().execute(
            "SELECT 1 FROM variants WHERE card_id = ? AND used = 0 LIMIT 1",
            (card_id,),
        )
        return cursor.fetchone() is not None

    def store_variant(self, card_id: int, variant_text: str,
                      expected_answer: str = "", variant_style: str = "",
                      svg: str = ""):
        """Store a generated variant, enforcing the max per card.

        Locked: count + insert is check-then-act. Without the lock, two
        concurrent prefetch workers could both observe count=max-1 and both
        insert, producing max+1 cached variants.
        """
        conn = self._conn()
        with self._lock:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM variants WHERE card_id = ? AND used = 0",
                (card_id,),
            )
            count = cursor.fetchone()[0]
            if count >= self._max_variants:
                return  # already have enough cached

            conn.execute(
                """INSERT INTO variants (card_id, variant_text, created_at, used,
                   expected_answer, variant_style, svg)
                   VALUES (?, ?, ?, 0, ?, ?, ?)""",
                (card_id, variant_text, time.time(),
                 expected_answer or None, variant_style or None,
                 svg or None),
            )
            conn.commit()

    def count_unused(self, card_id: int) -> int:
        """Count unused variants for a card."""
        cursor = self._conn().execute(
            "SELECT COUNT(*) FROM variants WHERE card_id = ? AND used = 0",
            (card_id,),
        )
        return cursor.fetchone()[0]

    def cleanup(self, max_age_days: int = 30):
        """Remove old used variants to keep the database small."""
        cutoff = time.time() - (max_age_days * 86400)
        conn = self._conn()
        conn.execute(
            "DELETE FROM variants WHERE used = 1 AND created_at < ?",
            (cutoff,),
        )
        conn.commit()

    def clear_card(self, card_id: int):
        """Remove all cached variants for a specific card."""
        conn = self._conn()
        conn.execute(
            "DELETE FROM variants WHERE card_id = ?",
            (card_id,),
        )
        conn.commit()

    def clear_all(self):
        """Remove all cached variants."""
        conn = self._conn()
        conn.execute("DELETE FROM variants")
        conn.commit()

    # ------------------------------------------------------------------
    # Card ideas
    # ------------------------------------------------------------------

    def save_idea(self, card_id, variant_text, original_question,
                  original_answer, rating=None, evaluation=None,
                  edited_variant_text=None, edited_answer_text=None):
        # type: (int, str, str, str, Optional[int], Optional[str], Optional[str], Optional[str]) -> int
        """Save a card idea. Returns the new row id."""
        conn = self._conn()
        cursor = conn.execute(
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
        conn.commit()
        return cursor.lastrowid

    def get_ideas(self, include_used=False):
        # type: (bool) -> List[Dict]
        """Return saved ideas as a list of dicts, newest first."""
        conn = self._conn()
        if include_used:
            rows = conn.execute(
                "SELECT id, card_id, variant_text, original_question, "
                "original_answer, rating, created_at, used, evaluation, "
                "edited_variant_text, edited_answer_text, "
                "decision_status, decision_reason "
                "FROM card_ideas ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
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
        conn = self._conn()
        conn.execute(
            "UPDATE card_ideas SET edited_variant_text = ? WHERE id = ?",
            (edited_variant_text, idea_id),
        )
        conn.commit()

    def update_idea_answer_edit(self, idea_id, edited_answer_text):
        # type: (int, str) -> None
        """Persist the latest human-edited answer wording for an idea."""
        conn = self._conn()
        conn.execute(
            "UPDATE card_ideas SET edited_answer_text = ? WHERE id = ?",
            (edited_answer_text, idea_id),
        )
        conn.commit()

    def set_idea_decision(self, idea_id, decision_status, decision_reason=None,
                          mark_used=False):
        # type: (int, str, Optional[str], bool) -> None
        """Store human triage label for an idea (accept/edit/reject)."""
        conn = self._conn()
        if mark_used:
            conn.execute(
                "UPDATE card_ideas "
                "SET decision_status = ?, decision_reason = ?, used = 1 "
                "WHERE id = ?",
                (decision_status, decision_reason, idea_id),
            )
        else:
            conn.execute(
                "UPDATE card_ideas "
                "SET decision_status = ?, decision_reason = ? "
                "WHERE id = ?",
                (decision_status, decision_reason, idea_id),
            )
        conn.commit()

    def mark_idea_used(self, idea_id):
        # type: (int) -> None
        """Mark an idea as used (dismissed or created)."""
        conn = self._conn()
        conn.execute(
            "UPDATE card_ideas SET used = 1 WHERE id = ?",
            (idea_id,),
        )
        conn.commit()

    def count_unseen_ideas(self):
        # type: () -> int
        """Count ideas that haven't been used yet."""
        cursor = self._conn().execute(
            "SELECT COUNT(*) FROM card_ideas WHERE used = 0"
        )
        return cursor.fetchone()[0]

    def delete_idea(self, idea_id):
        # type: (int) -> None
        """Permanently delete an idea."""
        conn = self._conn()
        conn.execute(
            "DELETE FROM card_ideas WHERE id = ?",
            (idea_id,),
        )
        conn.commit()

    def close(self):
        """Close the calling thread's connection.

        Worker-thread connections are not explicitly closed here — they are
        released when the worker thread exits (sqlite3.Connection.__del__
        handles cleanup). Closing them from the main thread would raise
        ProgrammingError since each connection is bound to its creating
        thread.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
