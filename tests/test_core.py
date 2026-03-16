"""Tests for Anki Proteus core logic (no Anki/Qt dependency)."""

import ast
import json
import os
import sqlite3
import sys
import threading
import time

import pytest

# ---------------------------------------------------------------------------
# Setup: make addon modules importable
# ---------------------------------------------------------------------------

ADDON_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir))

# generator.py and cache.py have no Anki deps — import directly
sys.path.insert(0, ADDON_DIR)
import generator  # noqa: E402
from cache import VariantCache  # noqa: E402

# __init__.py can't be imported (aqt dependency at top level).
# Extract the three pure-math functions via AST so tests always run
# against the real source, not a copy.
_init_source = open(os.path.join(ADDON_DIR, "__init__.py")).read()
_tree = ast.parse(_init_source)
_target_funcs = {"_estimate_cost", "_budget_pct", "_budget_bar_text", "_idea_has_feedback"}
_func_nodes = [
    n for n in ast.iter_child_nodes(_tree)
    if isinstance(n, ast.FunctionDef) and n.name in _target_funcs
]
_mod = ast.Module(body=_func_nodes, type_ignores=[])
ast.fix_missing_locations(_mod)
_ns = {}
exec(compile(_mod, "__init__.py", "exec"), _ns)
_estimate_cost = _ns["_estimate_cost"]
_budget_pct = _ns["_budget_pct"]
_budget_bar_text = _ns["_budget_bar_text"]
_idea_has_feedback = _ns["_idea_has_feedback"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_usage(tmp_path, monkeypatch):
    """Point the usage tracker at a temp directory and reset state."""
    monkeypatch.setattr(generator, "_USAGE_PATH", str(tmp_path / "usage.json"))
    monkeypatch.setattr(generator, "_usage_tracker", None)


# ===========================================================================
# 1-6  Usage tracker (generator.py)
# ===========================================================================

class TestUsageTracker:

    def test_fresh_start(self):
        """No usage.json exists -> get_usage() returns zeros."""
        usage = generator.get_usage()
        assert usage == {"input_tokens": 0, "output_tokens": 0, "api_calls": 0}

    def test_record_and_accumulate(self):
        """Call _record_usage(100, 50) twice -> totals are 200/100/2."""
        generator._record_usage(100, 50)
        generator._record_usage(100, 50)
        usage = generator.get_usage()
        assert usage["input_tokens"] == 200
        assert usage["output_tokens"] == 100
        assert usage["api_calls"] == 2

    def test_persistence_round_trip(self):
        """Record, clear in-memory tracker, reload from disk."""
        generator._record_usage(42, 17)
        # Force reload from disk on next access
        generator._usage_tracker = None
        usage = generator.get_usage()
        assert usage["input_tokens"] == 42
        assert usage["output_tokens"] == 17
        assert usage["api_calls"] == 1

    def test_reset(self):
        """Reset clears both in-memory and on-disk data."""
        generator._record_usage(500, 200)
        generator.reset_usage()
        usage = generator.get_usage()
        assert usage == {"input_tokens": 0, "output_tokens": 0, "api_calls": 0}
        # Verify on disk too
        with open(generator._USAGE_PATH) as f:
            disk = json.load(f)
        assert disk["input_tokens"] == 0

    def test_corrupt_file(self):
        """Corrupt usage.json -> _load_usage returns zeros gracefully."""
        with open(generator._USAGE_PATH, "w") as f:
            f.write("NOT VALID JSON {{{")
        usage = generator.get_usage()
        assert usage == {"input_tokens": 0, "output_tokens": 0, "api_calls": 0}

    def test_thread_safety(self):
        """10 threads each calling _record_usage(1, 1) -> totals exactly 10/10/10."""
        threads = [
            threading.Thread(target=generator._record_usage, args=(1, 1))
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        usage = generator.get_usage()
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 10
        assert usage["api_calls"] == 10


# ===========================================================================
# 7-8  Grading config path (generator.py)
# ===========================================================================

class TestVariantGeneration:

    def test_generate_variant_shortens_overlong_output(self, monkeypatch):
        """Overlong variants trigger one shorten pass."""
        calls = []

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            calls.append({"max_tokens": max_tokens, "system": system})
            if len(calls) == 1:
                return (
                    "What type of effects should be used for subject and item in a "
                    "hierarchical study design where you need to account for "
                    "correlations within groups and allow parameters to vary across "
                    "different levels of the data?"
                )
            return "Should subject and item be modeled as random effects in this study?"

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.generate_variant(
            question="Q",
            answer="A",
            config={"api_key": "k", "model": "m"},
        )

        assert len(calls) == 2
        assert calls[0]["max_tokens"] == 300
        assert calls[1]["max_tokens"] == 120
        assert out == "Should subject and item be modeled as random effects in this study?"
        assert len(out.split()) <= generator._MAX_VARIANT_WORDS

    def test_generate_variant_hard_caps_when_shorten_fails(self, monkeypatch):
        """If shorten pass fails, variant is hard-capped to limits."""
        calls = []

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            calls.append(max_tokens)
            if len(calls) == 1:
                return (
                    "In this hierarchical design with repeated observations from many "
                    "subjects and items across several contextual groupings, what type "
                    "of effects should be used for subject and item if we need to "
                    "capture within-group correlations while allowing parameters to vary?"
                )
            return None

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.generate_variant(
            question="Q",
            answer="A",
            config={"api_key": "k", "model": "m"},
        )

        assert calls == [300, 120]
        assert out is not None
        assert len(out.split()) <= generator._MAX_VARIANT_WORDS
        assert len(out) <= generator._MAX_VARIANT_CHARS
        assert out.endswith("?")


# ===========================================================================
# 7-8  Grading config path (generator.py)
# ===========================================================================

class TestGradingConfig:

    def test_grade_response_uses_fast_defaults(self, monkeypatch):
        """grade_response should call API with grading defaults."""
        captured = {}

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            captured["api_key"] = api_key
            captured["model"] = model
            captured["max_tokens"] = max_tokens
            captured["timeout_s"] = timeout_s
            return '{"correct":[],"incorrect":[],"missed":[],"overall":"ok","score":3}'

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m"},
        )

        assert captured["api_key"] == "k"
        assert captured["model"] == "m"
        assert captured["max_tokens"] == 280
        assert captured["timeout_s"] == 10
        assert out["score"] == 3

    def test_grade_response_honors_overrides(self, monkeypatch):
        """grading_model/max_tokens/timeout overrides should be passed through."""
        captured = {}

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            captured["model"] = model
            captured["max_tokens"] = max_tokens
            captured["timeout_s"] = timeout_s
            return '{"correct":[],"incorrect":[],"missed":[],"overall":"ok","score":4}'

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={
                "api_key": "k",
                "model": "slow-model",
                "grading_model": "fast-model",
                "grading_max_tokens": 120,
                "grading_timeout_s": 6,
            },
        )

        assert captured["model"] == "fast-model"
        assert captured["max_tokens"] == 120
        assert captured["timeout_s"] == 6
        assert out["score"] == 4

    def test_grade_response_falls_back_to_base_model(self, monkeypatch):
        """If grading override fails, grade_response retries once on base model."""
        calls = []

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            calls.append((model, max_tokens, timeout_s))
            if model == "bad-fast-model":
                return None
            return '{"correct":[],"incorrect":[],"missed":[],"overall":"ok","score":5}'

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={
                "api_key": "k",
                "model": "base-model",
                "grading_model": "bad-fast-model",
                "grading_max_tokens": 120,
                "grading_timeout_s": 10,
            },
        )

        assert calls[0][0] == "bad-fast-model"
        assert calls[1][0] == "base-model"
        assert calls[1][2] == 10  # fallback uses a slightly higher timeout floor
        assert out["score"] == 5

    def test_grade_response_misaligned_zeroes_correctness(self, monkeypatch):
        """Misaligned variants should not return correctness verdict fields."""

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            return json.dumps({
                "alignment": "misaligned",
                "alignment_note": "target drift",
                "learning_feedback": ["Good related intuition."],
                "correct": ["point A"],
                "incorrect": ["point B"],
                "missed": ["point C"],
                "overall": "Not directly comparable.",
                "score": 5,
            })

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m"},
        )

        assert out["alignment"] == "misaligned"
        assert out["score"] == 0
        assert out["correct"] == []
        assert out["incorrect"] == []
        assert out["missed"] == []
        assert out["learning_feedback"] == ["Good related intuition."]
        assert out["coverage_pct"] == 0
        assert out["question_gap_points"] == ["point C"]

    def test_grade_response_computes_coverage_pct(self, monkeypatch):
        """Coverage is derived from covered/missed canonical points when omitted."""

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            return json.dumps({
                "alignment": "aligned",
                "alignment_note": "",
                "canonical_points": ["A", "B", "C"],
                "covered_points": ["A", "B"],
                "missed_points": ["C"],
                "incorrect": [],
                "learning_feedback": [],
                "overall": "Solid response.",
            })

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m"},
        )

        assert out["coverage_pct"] == 67
        assert out["covered_points"] == ["A", "B"]
        assert out["missed_points"] == ["C"]
        assert out["correct"] == ["A", "B"]
        assert out["question_gap_points"] == ["C"]

    def test_grade_response_derives_missing_gap_points_from_canonical(self, monkeypatch):
        """If missed/question gaps are absent, derive them from canonical-covered diff."""

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            return json.dumps({
                "alignment": "partial",
                "canonical_points": ["A", "B", "C"],
                "covered_points": ["A"],
                "coverage_pct": 33,
                "overall": "Partial.",
            })

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m"},
        )

        assert out["coverage_pct"] == 33
        assert out["missed_points"] == ["B", "C"]
        assert out["question_gap_points"] == ["B", "C"]

    def test_grade_response_ignores_inconsistent_raw_coverage_when_points_exist(self, monkeypatch):
        """Coverage percent is computed from points when those points are available."""

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            return json.dumps({
                "alignment": "aligned",
                "canonical_points": ["A", "B", "C"],
                "covered_points": ["A", "B", "C"],
                "coverage_pct": 33,
                "overall": "Complete.",
            })

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m"},
        )

        assert out["coverage_pct"] == 100

    def test_grade_response_salvages_truncated_json(self, monkeypatch):
        """Truncated JSON should still return structured alignment/related feedback."""

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            return (
                '{"alignment":"partial","alignment_note":"target overlap but incomplete",'
                '"learning_feedback":["Good intuition about dependencies","Generalization caveat"'
            )

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m"},
        )

        assert out["alignment"] == "partial"
        assert out["alignment_note"] == "target overlap but incomplete"
        assert out["learning_feedback"] == [
            "Good intuition about dependencies",
            "Generalization caveat",
        ]
        assert not out["overall"].startswith("{")

    def test_grade_response_invalid_json_uses_clean_fallback(self, monkeypatch):
        """Totally invalid JSON should not leak raw payload into overall."""

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            return "<<<bad-output>>>"

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m"},
        )

        assert out["overall"] == "Evaluation unavailable."
        assert out["score"] == 0


# ===========================================================================
# 9-14  Variant cache (cache.py)
# ===========================================================================

class TestVariantCache:

    @pytest.fixture()
    def cache(self, tmp_path):
        c = VariantCache(str(tmp_path), max_variants=3)
        yield c
        c.close()

    def test_store_and_retrieve(self, cache):
        """Store a variant, get_variant returns (id, text), marks it used."""
        cache.store_variant(1, "What is X?")
        result = cache.get_variant(1)
        assert result is not None
        vid, text = result
        assert text == "What is X?"
        assert isinstance(vid, int)
        assert not cache.has_variant(1)

    def test_has_variant(self, cache):
        """True when unused variant exists, False after retrieval."""
        assert not cache.has_variant(1)
        cache.store_variant(1, "Variant A")
        assert cache.has_variant(1)
        cache.get_variant(1)
        assert not cache.has_variant(1)

    def test_max_variants_enforced(self, cache):
        """Store max_variants+1 -> last one rejected, count stays at max."""
        for i in range(4):  # max is 3
            cache.store_variant(1, "Variant %d" % i)
        assert cache.count_unused(1) == 3

    def test_fifo_order(self, cache):
        """Store A then B -> retrieve returns A first."""
        cache.store_variant(1, "A")
        time.sleep(0.01)  # ensure different created_at
        cache.store_variant(1, "B")
        _, text_a = cache.get_variant(1)
        _, text_b = cache.get_variant(1)
        assert text_a == "A"
        assert text_b == "B"

    def test_clear_card(self, cache):
        """clear_card removes only that card's variants."""
        cache.store_variant(1, "V1")
        cache.store_variant(2, "V2")
        cache.clear_card(1)
        assert not cache.has_variant(1)
        assert cache.has_variant(2)

    def test_record_feedback(self, cache):
        """Store variant, retrieve it, record thumbs-up, verify in DB."""
        cache.store_variant(1, "Variant Q")
        vid, _ = cache.get_variant(1)
        cache.record_feedback(vid, 1)
        with cache._lock:
            row = cache._conn.execute(
                "SELECT rating FROM variants WHERE id = ?", (vid,)
            ).fetchone()
        assert row[0] == 1

    def test_feedback_ignored_when_not_given(self, cache):
        """Variant with no feedback keeps rating as NULL."""
        cache.store_variant(1, "Variant Q")
        vid, _ = cache.get_variant(1)
        with cache._lock:
            row = cache._conn.execute(
                "SELECT rating FROM variants WHERE id = ?", (vid,)
            ).fetchone()
        assert row[0] is None

    def test_cleanup(self, cache):
        """Old used variants removed, recent ones kept."""
        # Store and use a variant, then backdate it
        cache.store_variant(1, "Old")
        cache.get_variant(1)
        with cache._lock:
            cache._conn.execute(
                "UPDATE variants SET created_at = ? WHERE card_id = 1",
                (time.time() - 60 * 86400,),
            )
            cache._conn.commit()

        # Store and use a recent variant
        cache.store_variant(2, "Recent")
        cache.get_variant(2)

        cache.cleanup(max_age_days=30)

        with cache._lock:
            rows = cache._conn.execute("SELECT card_id FROM variants").fetchall()
            remaining = [r[0] for r in rows]
        assert 1 not in remaining
        assert 2 in remaining


# ===========================================================================
# Schema versioning (cache.py)
# ===========================================================================

class TestSchemaMigration:

    def test_fresh_db_gets_latest_version(self, tmp_path):
        """A brand-new database should be at _SCHEMA_VERSION."""
        c = VariantCache(str(tmp_path), max_variants=3)
        version = c._conn.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        from cache import _SCHEMA_VERSION
        assert version == _SCHEMA_VERSION

    def test_v0_db_migrates_to_latest(self, tmp_path):
        """A v0 database (no tables) should be fully migrated."""
        db_path = str(tmp_path / "variant_cache.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()

        c = VariantCache(str(tmp_path), max_variants=3)
        version = c._conn.execute("PRAGMA user_version").fetchone()[0]

        # Verify all tables and columns exist
        c.store_variant(1, "Test variant")
        c.record_feedback(1, 1)  # rating column exists
        idea_id = c.save_idea(1, "V", "Q", "A", evaluation="e",
                              edited_variant_text="ev",
                              edited_answer_text="ea")
        c.set_idea_decision(idea_id, "accepted", "good")
        ideas = c.get_ideas(include_used=False)
        assert len(ideas) == 1
        assert ideas[0]["edited_answer_text"] == "ea"
        assert ideas[0]["decision_status"] == "accepted"

        from cache import _SCHEMA_VERSION
        assert version == _SCHEMA_VERSION
        c.close()

    def test_pre_versioned_db_upgrades(self, tmp_path):
        """A database with tables but user_version=0 (pre-versioning) migrates safely."""
        db_path = str(tmp_path / "variant_cache.db")
        conn = sqlite3.connect(db_path)
        # Create v0-style tables (original schema, no rating)
        conn.execute("""
            CREATE TABLE variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                variant_text TEXT NOT NULL,
                created_at REAL NOT NULL,
                used INTEGER DEFAULT 0
            )
        """)
        conn.execute("INSERT INTO variants (card_id, variant_text, created_at, used) VALUES (42, 'old', 1000, 0)")
        conn.commit()
        conn.close()

        c = VariantCache(str(tmp_path), max_variants=3)
        # Old data preserved
        result = c.get_variant(42)
        assert result is not None
        assert result[1] == "old"
        # New columns work
        c.store_variant(42, "new")
        idea_id = c.save_idea(42, "V", "Q", "A", edited_answer_text="ea")
        ideas = c.get_ideas()
        assert ideas[0]["edited_answer_text"] == "ea"
        c.close()

    def test_already_current_db_is_noop(self, tmp_path):
        """Opening an already-current database doesn't error."""
        c1 = VariantCache(str(tmp_path), max_variants=3)
        c1.store_variant(1, "V1")
        c1.close()

        c2 = VariantCache(str(tmp_path), max_variants=3)
        assert c2.has_variant(1)
        version = c2._conn.execute("PRAGMA user_version").fetchone()[0]
        from cache import _SCHEMA_VERSION
        assert version == _SCHEMA_VERSION
        c2.close()


# ===========================================================================
# 13-15  Cost / budget helpers (__init__.py)
# ===========================================================================

class TestCostBudget:

    def test_estimate_cost(self):
        """Known input/output -> expected USD (Sonnet: $3/M in, $15/M out)."""
        cost = _estimate_cost(1000, 500)
        expected = (1000 * 3.0 + 500 * 15.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_budget_pct(self):
        """0 budget -> 0%; normal case; capped at 100%."""
        usage = {"input_tokens": 1000, "output_tokens": 500}
        assert _budget_pct(usage, 0) == 0

        # cost = (100_000*3 + 10_000*15)/1M = 0.45 -> 0.45/5.00*100 = 9%
        usage = {"input_tokens": 100_000, "output_tokens": 10_000}
        assert _budget_pct(usage, 5.00) == 9

        # Way over budget -> capped at 100
        usage = {"input_tokens": 10_000_000, "output_tokens": 10_000_000}
        assert _budget_pct(usage, 0.01) == 100

    def test_budget_bar_text(self):
        """Green/orange/red thresholds and correct block characters."""
        green = _budget_bar_text(42)
        assert "#4caf50" in green
        assert "42%" in green
        assert "\u2588" in green  # filled block
        assert "\u2591" in green  # empty block

        orange = _budget_bar_text(80)
        assert "#ff9800" in orange
        assert "80%" in orange

        red = _budget_bar_text(100)
        assert "#f44336" in red
        assert "100%" in red


# ===========================================================================
# 16  Feedback gate helper (__init__.py)
# ===========================================================================

class TestIdeaFeedbackGate:

    def test_idea_has_feedback(self):
        """Create-card gate requires non-empty evaluation text."""
        assert not _idea_has_feedback({"evaluation": None})
        assert not _idea_has_feedback({"evaluation": ""})
        assert not _idea_has_feedback({"evaluation": "   "})
        assert _idea_has_feedback({"evaluation": '{"overall":"ok","score":3}'})
        assert _idea_has_feedback({"evaluation": "fallback text"})


# ===========================================================================
# 17-23  Card ideas (cache.py)
# ===========================================================================

class TestCardIdeas:

    @pytest.fixture()
    def cache(self, tmp_path):
        c = VariantCache(str(tmp_path), max_variants=3)
        yield c
        c.close()

    def test_save_and_retrieve_idea(self, cache):
        """Save an idea, retrieve it, verify all fields."""
        idea_id = cache.save_idea(
            card_id=42,
            variant_text="What is the capital of France?",
            original_question="Name the capital of France.",
            original_answer="Paris",
            rating=1,
        )
        assert isinstance(idea_id, int)
        ideas = cache.get_ideas()
        assert len(ideas) == 1
        idea = ideas[0]
        assert idea["id"] == idea_id
        assert idea["card_id"] == 42
        assert idea["variant_text"] == "What is the capital of France?"
        assert idea["original_question"] == "Name the capital of France."
        assert idea["original_answer"] == "Paris"
        assert idea["rating"] == 1
        assert idea["used"] == 0
        assert idea["edited_variant_text"] is None
        assert idea["decision_status"] == "pending"
        assert idea["decision_reason"] is None

    def test_mark_idea_used(self, cache):
        """Mark idea used -> excluded from default get_ideas, included with flag."""
        idea_id = cache.save_idea(1, "V", "Q", "A")
        cache.mark_idea_used(idea_id)
        assert cache.get_ideas(include_used=False) == []
        ideas = cache.get_ideas(include_used=True)
        assert len(ideas) == 1
        assert ideas[0]["used"] == 1

    def test_count_unseen_ideas(self, cache):
        """Save 2, mark 1 used -> count = 1."""
        id1 = cache.save_idea(1, "V1", "Q1", "A1")
        cache.save_idea(2, "V2", "Q2", "A2")
        assert cache.count_unseen_ideas() == 2
        cache.mark_idea_used(id1)
        assert cache.count_unseen_ideas() == 1

    def test_delete_idea(self, cache):
        """Save, delete, verify gone from all queries."""
        idea_id = cache.save_idea(1, "V", "Q", "A")
        cache.delete_idea(idea_id)
        assert cache.get_ideas(include_used=True) == []
        assert cache.count_unseen_ideas() == 0

    def test_save_idea_without_rating(self, cache):
        """Rating defaults to None when not provided."""
        idea_id = cache.save_idea(1, "V", "Q", "A")
        ideas = cache.get_ideas()
        assert ideas[0]["rating"] is None

    def test_update_idea_edit(self, cache):
        """Human-edited idea text is persisted and retrievable."""
        idea_id = cache.save_idea(1, "Original", "Q", "A")
        cache.update_idea_edit(idea_id, "Edited by human")
        idea = cache.get_ideas(include_used=True)[0]
        assert idea["edited_variant_text"] == "Edited by human"

    def test_set_idea_decision(self, cache):
        """Decision status/reason saved, optionally marking idea used."""
        idea_id = cache.save_idea(1, "V", "Q", "A")
        cache.set_idea_decision(idea_id, "edited_accepted", "awkward_wording")
        idea = cache.get_ideas(include_used=True)[0]
        assert idea["decision_status"] == "edited_accepted"
        assert idea["decision_reason"] == "awkward_wording"
        assert idea["used"] == 0

        cache.set_idea_decision(idea_id, "rejected", "too_easy", mark_used=True)
        idea = cache.get_ideas(include_used=True)[0]
        assert idea["decision_status"] == "rejected"
        assert idea["decision_reason"] == "too_easy"
        assert idea["used"] == 1

    def test_ideas_ordered_by_created_at_desc(self, cache):
        """Most recent idea appears first."""
        cache.save_idea(1, "First", "Q1", "A1")
        time.sleep(0.01)
        cache.save_idea(2, "Second", "Q2", "A2")
        ideas = cache.get_ideas()
        assert ideas[0]["variant_text"] == "Second"
        assert ideas[1]["variant_text"] == "First"

    def test_ideas_table_independent_of_variants(self, cache):
        """Saving ideas doesn't affect the variants table."""
        cache.store_variant(1, "Variant Q")
        cache.save_idea(1, "Idea V", "Q", "A")
        assert cache.count_unused(1) == 1  # variant still there
        ideas = cache.get_ideas()
        assert len(ideas) == 1  # idea also there


# ===========================================================================
# 24-30  Feedback mode: prompt selection (generator.py)
# ===========================================================================

class TestFeedbackModePromptSelection:

    def test_ai_mode_uses_ai_prompt_and_omits_canonical(self, monkeypatch):
        """AI mode should use the AI-specific prompt without canonical answer."""
        captured = {}

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            captured["system"] = system
            captured["user_message"] = user_message
            captured["max_tokens"] = max_tokens
            return json.dumps({
                "expected_answer": "The answer",
                "ai_covered_points": ["A"],
                "ai_missed_points": [],
                "ai_coverage_pct": 100,
                "overall": "Good.",
            })

        monkeypatch.setattr(generator, "_call_api", fake_call)

        generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m", "feedback_mode": "ai"},
        )

        assert "determine the ideal answer" in captured["system"]
        assert "Canonical answer" not in captured["user_message"]
        assert "Q" in captured["user_message"]
        assert "R" in captured["user_message"]

    def test_canonical_mode_uses_canonical_prompt(self, monkeypatch):
        """Canonical mode should use the standard grading prompt with canonical answer."""
        captured = {}

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            captured["system"] = system
            captured["user_message"] = user_message
            return json.dumps({
                "alignment": "aligned",
                "canonical_points": ["A"],
                "covered_points": ["A"],
                "coverage_pct": 100,
                "overall": "Good.",
            })

        monkeypatch.setattr(generator, "_call_api", fake_call)

        generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m", "feedback_mode": "canonical"},
        )

        assert "canonical answer" in captured["system"].lower()
        assert "Canonical answer:" in captured["user_message"]

    def test_both_mode_uses_both_prompt_and_bumps_tokens(self, monkeypatch):
        """Both mode should use the dual-perspective prompt and increase max_tokens."""
        captured = {}

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            captured["system"] = system
            captured["user_message"] = user_message
            captured["max_tokens"] = max_tokens
            return json.dumps({
                "alignment": "aligned",
                "expected_answer": "The answer",
                "canonical_points": ["A"],
                "covered_points": ["A"],
                "coverage_pct": 100,
                "ai_covered_points": ["A"],
                "ai_missed_points": [],
                "ai_coverage_pct": 100,
                "overall": "Good.",
            })

        monkeypatch.setattr(generator, "_call_api", fake_call)

        generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m", "feedback_mode": "both"},
        )

        assert "TWO perspectives" in captured["system"]
        assert "Canonical answer:" in captured["user_message"]
        assert captured["max_tokens"] == int(280 * 1.4)

    def test_default_feedback_mode_is_canonical(self, monkeypatch):
        """No feedback_mode in config should default to canonical prompt."""
        captured = {}

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            captured["system"] = system
            return json.dumps({"overall": "ok"})

        monkeypatch.setattr(generator, "_call_api", fake_call)

        generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m"},
        )

        assert "TWO perspectives" not in captured["system"]
        assert "determine the ideal answer" not in captured["system"]


# ===========================================================================
# 31-35  Feedback mode: normalization of ai_* fields (generator.py)
# ===========================================================================

class TestAIFieldNormalization:

    def test_both_mode_returns_ai_fields(self, monkeypatch):
        """Both-mode grading should include ai_covered/missed/coverage in output."""

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            return json.dumps({
                "alignment": "aligned",
                "expected_answer": "Use random effects",
                "canonical_points": ["A", "B"],
                "covered_points": ["A"],
                "missed_points": ["B"],
                "coverage_pct": 50,
                "ai_covered_points": ["X", "Y"],
                "ai_missed_points": ["Z"],
                "ai_coverage_pct": 67,
                "overall": "Partial.",
            })

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m", "feedback_mode": "both"},
        )

        # Canonical fields
        assert out["covered_points"] == ["A"]
        assert out["missed_points"] == ["B"]
        assert out["coverage_pct"] == 50

        # AI fields
        assert out["ai_covered_points"] == ["X", "Y"]
        assert out["ai_missed_points"] == ["Z"]
        assert out["ai_coverage_pct"] == 67

    def test_ai_coverage_derived_from_points(self, monkeypatch):
        """AI coverage_pct should be recomputed from point counts."""

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            return json.dumps({
                "expected_answer": "Answer",
                "ai_covered_points": ["A"],
                "ai_missed_points": ["B", "C"],
                "ai_coverage_pct": 99,  # LLM says 99 but points say 1/3
                "overall": "Partial.",
            })

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m", "feedback_mode": "ai"},
        )

        assert out["ai_coverage_pct"] == 33  # recomputed from 1/3

    def test_canonical_mode_omits_ai_fields(self, monkeypatch):
        """Canonical-only grading should not include ai_* keys."""

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            return json.dumps({
                "alignment": "aligned",
                "canonical_points": ["A"],
                "covered_points": ["A"],
                "coverage_pct": 100,
                "overall": "Good.",
            })

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m", "feedback_mode": "canonical"},
        )

        assert "ai_covered_points" not in out
        assert "ai_missed_points" not in out
        assert "ai_coverage_pct" not in out

    def test_misaligned_still_returns_ai_fields_in_both_mode(self, monkeypatch):
        """Misaligned canonical should zero canonical but preserve AI fields."""

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            return json.dumps({
                "alignment": "misaligned",
                "alignment_note": "drift",
                "expected_answer": "Different target",
                "canonical_points": ["A"],
                "covered_points": ["A"],
                "ai_covered_points": ["X"],
                "ai_missed_points": ["Y"],
                "ai_coverage_pct": 50,
                "overall": "Drifted.",
            })

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m", "feedback_mode": "both"},
        )

        # Canonical zeroed due to misalignment
        assert out["coverage_pct"] == 0
        assert out["covered_points"] == []

        # AI fields preserved
        assert out["ai_covered_points"] == ["X"]
        assert out["ai_missed_points"] == ["Y"]
        assert out["ai_coverage_pct"] == 50

    def test_partial_json_extracts_ai_fields(self, monkeypatch):
        """Truncated JSON with ai_* fields should be salvaged."""

        def fake_call(api_key, model, system, user_message, max_tokens=300, timeout_s=15):
            return (
                '{"alignment":"aligned","expected_answer":"The answer",'
                '"ai_covered_points":["point A","point B"],'
                '"ai_missed_points":["point C"],'
                '"overall":"Partial'
            )

        monkeypatch.setattr(generator, "_call_api", fake_call)

        out = generator.grade_response(
            variant_question="Q",
            user_response="R",
            canonical_answer="A",
            config={"api_key": "k", "model": "m", "feedback_mode": "both"},
        )

        assert out["expected_answer"] == "The answer"
        assert out["ai_covered_points"] == ["point A", "point B"]
        assert out["ai_missed_points"] == ["point C"]


# ===========================================================================
# 36-38  Variant generation constraints (generator.py)
# ===========================================================================

class TestVariantGenerationConstraints:

    def test_variant_prompt_includes_minimalistic_constraint(self):
        """System prompt should mention minimalistic communication."""
        assert "minimalistically" in generator.VARIANT_SYSTEM_PROMPT

    def test_variant_prompt_never_longer_constraint(self):
        """System prompt should say never longer than original."""
        assert "never longer than the original question" in generator.VARIANT_SYSTEM_PROMPT

    def test_hard_limit_adds_question_mark(self):
        """Hard-limited variants should end with a question mark."""
        long = "word " * 30
        result = generator._hard_limit_variant(long)
        assert result.endswith("?")
        assert len(result.split()) <= generator._MAX_VARIANT_WORDS
