"""Tests for Anki Proteus core logic (no Anki/Qt dependency)."""

import ast
import json
import os
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
        assert captured["max_tokens"] == 120
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
