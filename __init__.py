"""
Anki Proteus
Generates LLM-powered question variants at review time to test
understanding rather than pattern recognition.

Two modes:
  - flip:     See variant question → flip → see original answer → grade
  - freeform: See variant question → speak/type response → flip →
              see LLM evaluation + original answer → grade
"""

import html
import json
import os
import random
import re
import time as _time
from aqt import mw, gui_hooks
from aqt.utils import showInfo, tooltip
from aqt.qt import QAction, QThread, pyqtSignal, QObject
from aqt.webview import AnkiWebView

from .generator import (
    generate_variant, grade_response, get_usage, reset_usage,
    diag_log, DEFAULT_MODEL,
)
from .cache import VariantCache
from .prefetch import PrefetchWorker
from .batch_prefetch import BatchPrefetchManager

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ADDON_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(ADDON_DIR, "config.json")


def _log(msg: str):
    """Append a timestamped line to the diag log (only when debug_logging is on)."""
    diag_log(msg, debug=CONFIG.get("debug_logging", False))

def load_config():
    """Load config, merging user overrides with defaults."""
    defaults = {
        "enabled": True,                  # master on/off toggle for Proteus variants
        "api_key": "",
        "model": DEFAULT_MODEL,
        "response_mode": "flip",          # "flip" or "freeform"
        "active_decks": [],                # empty = all decks
        "transform_percent": 80,           # % of eligible cards to transform
        "min_interval_days": 0,            # only transform cards above this interval
        "max_cached_variants": 3,          # variants to pre-generate per card
        "system_prompt": "",               # optional domain context for generation
        "exclude_note_types": ["Image Occlusion"],  # note types to skip
        "batch_prefetch_count": 15,        # cards to pre-generate on session start (0 = off)
        "batch_prefetch_concurrency": 3,   # max simultaneous API calls
        "show_prefetch_progress": True,    # show tooltip progress during batch prefetch
        "debug_logging": False,            # write to proteus_diag.log
        "usage_budget": 5.00,              # monthly budget in USD (for progress bar)
        "submit_delay_ms": 750,            # ms delay after Enter before flipping to answer
        "grading_model": "",               # optional override for grading model
        "grading_max_tokens": 280,         # room for full grading schema
        "grading_timeout_s": 10,           # fail fast if grading is slow
        "feedback_mode": "both",           # "ai", "canonical", or "both"
    }
    conf = mw.addonManager.getConfig(__name__)
    if conf:
        defaults.update(conf)
    return defaults

CONFIG = {}

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_cache: VariantCache = None
_prefetch_worker: PrefetchWorker = None
_batch_manager: BatchPrefetchManager = None
_current_variant: str = None          # variant being shown right now
_current_variant_id: int = None       # DB row id for feedback
_current_card_id: int = None
_user_response: str = ""              # captured from freeform text input
_evaluation_text: str = None          # LLM grading result
_grading_worker = None                # background grading QThread
_grading_watchdog_seq: int = 0        # invalidate prior grading watchdog timers
_ideas_saved_this_session: int = 0
_idea_orphan_workers = []             # workers kept alive after ideas dialog closes

# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def init_addon():
    global CONFIG, _cache
    CONFIG = load_config()
    _cache = VariantCache(ADDON_DIR, max_variants=CONFIG.get("max_cached_variants", 3))

    if not CONFIG.get("api_key"):
        showInfo("Proteus: No API key configured.\n\n"
                 "Set it in: Tools → Add-ons → select Proteus → Config")

    # Register hooks
    gui_hooks.card_will_show.append(on_card_will_show)
    gui_hooks.reviewer_did_show_question.append(on_question_shown)
    gui_hooks.reviewer_did_show_answer.append(on_answer_shown)
    gui_hooks.webview_did_receive_js_message.append(on_js_message)
    gui_hooks.state_did_change.append(on_state_did_change)

    # Add config menu item
    action = QAction("Proteus Settings", mw)
    action.triggered.connect(show_config_dialog)
    mw.form.menuTools.addAction(action)

    # Add toggle shortcut (Ctrl+Shift+V to toggle response mode)
    toggle_action = QAction("Toggle Variant Response Mode", mw)
    toggle_action.setShortcut("Ctrl+Shift+V")
    toggle_action.triggered.connect(toggle_response_mode)
    mw.form.menuTools.addAction(toggle_action)

    # Add master on/off toggle shortcut (Cmd/Ctrl+Shift+P)
    enabled_action = QAction("Toggle Proteus On/Off", mw)
    enabled_action.setShortcut("Ctrl+Shift+P")
    enabled_action.triggered.connect(toggle_proteus_enabled)
    mw.form.menuTools.addAction(enabled_action)

    # Add answer-side variant peek shortcut (Cmd/Ctrl+Shift+B)
    peek_action = QAction("Toggle Variant Peek (After Answer)", mw)
    peek_action.setShortcut("Ctrl+Shift+B")
    peek_action.triggered.connect(toggle_variant_peek)
    mw.form.menuTools.addAction(peek_action)

    # Add back-to-question shortcut (Cmd/Ctrl+Shift+Left)
    back_action = QAction("Back to Question", mw)
    back_action.setShortcut("Ctrl+Shift+Left")
    back_action.triggered.connect(go_back_to_question)
    mw.form.menuTools.addAction(back_action)

    # Add usage stats menu item
    usage_action = QAction("Proteus Usage Stats", mw)
    usage_action.triggered.connect(show_usage_dialog)
    mw.form.menuTools.addAction(usage_action)

    # Add card ideas menu item
    ideas_action = QAction("Proteus: Card Ideas", mw)
    ideas_action.triggered.connect(show_card_ideas_dialog)
    mw.form.menuTools.addAction(ideas_action)


def toggle_response_mode():
    """Toggle between flip and freeform mode mid-session."""
    global CONFIG
    if CONFIG["response_mode"] == "flip":
        CONFIG["response_mode"] = "freeform"
        tooltip("Proteus: freeform mode (speak/type responses)")
        # Inject freeform input via JS if a variant is currently shown
        if _current_variant and mw.reviewer and mw.reviewer.web:
            escaped_html = _freeform_input_html().replace("\\", "\\\\").replace("`", "\\`")
            mw.reviewer.web.eval(f"""
            (function() {{
                var q = document.getElementById('variant-question');
                if (q && !document.getElementById('variant-response-area')) {{
                    q.insertAdjacentHTML('afterend', `{escaped_html}`);
                    var ta = document.getElementById('variant-response-input');
                    if (ta) setTimeout(function() {{ ta.focus(); }}, 100);
                }}
            }})();
            """)
    else:
        CONFIG["response_mode"] = "flip"
        tooltip("Proteus: flip mode (standard review)")
        # Remove freeform input via JS
        if mw.reviewer and mw.reviewer.web:
            mw.reviewer.web.eval("""
            (function() {
                var el = document.getElementById('variant-response-area');
                if (el) el.remove();
            })();
            """)


def toggle_proteus_enabled():
    # type: () -> None
    """Master toggle for enabling/disabling Proteus variants."""
    global CONFIG
    now_enabled = not bool(CONFIG.get("enabled", True))
    CONFIG["enabled"] = now_enabled
    if now_enabled:
        tooltip("Proteus: enabled")
        if mw.state == "review":
            _start_batch_prefetch()
    else:
        tooltip("Proteus: disabled")
        _cancel_batch_prefetch()


def toggle_variant_peek():
    # type: () -> None
    """Toggle the inline variant-question peek panel on the answer side."""
    if not mw.reviewer or not mw.reviewer.web:
        return
    if not _current_variant:
        tooltip("Proteus: no active variant on this card")
        return

    reviewer_state = str(getattr(mw.reviewer, "state", "") or "")
    if reviewer_state != "answer":
        tooltip("Proteus: variant peek is available after Show Answer")
        return

    mw.reviewer.web.eval(
        """
        (function() {
            var panel = document.getElementById('proteus-variant-peek');
            if (!panel) { return; }
            var visible = panel.style.display !== 'none';
            panel.style.display = visible ? 'none' : 'block';
            if (!visible && panel.scrollIntoView) {
                panel.scrollIntoView({behavior: 'smooth', block: 'start'});
            }
        })();
        """
    )


_returning_to_question = False  # flag to preserve state on re-show


def go_back_to_question():
    # type: () -> None
    """Navigate from answer side back to question side, preserving freeform state."""
    global _returning_to_question
    if not mw.reviewer:
        return
    reviewer_state = str(getattr(mw.reviewer, "state", "") or "")
    if reviewer_state != "answer":
        return
    _returning_to_question = True
    mw.reviewer._showQuestion()


# ---------------------------------------------------------------------------
# Card eligibility
# ---------------------------------------------------------------------------

_MIN_TEXT_LENGTH = 10  # cards with less extractable text are skipped


def _is_excluded_note_type(card) -> bool:
    """Check if a card's note type is in the exclusion list."""
    try:
        note_name = card.note_type()["name"].lower()
        excluded = CONFIG.get("exclude_note_types", [])
        return any(e.lower() in note_name for e in excluded)
    except Exception:
        return False


def _has_enough_text(card) -> bool:
    """Return False for image-only / media-only cards with no real text."""
    try:
        q = _extract_text(card, "question")
        return len(q) >= _MIN_TEXT_LENGTH
    except Exception:
        return False


def _is_eligible(card) -> bool:
    """Check all non-random eligibility criteria for variant generation."""
    if not CONFIG.get("enabled", True):
        return False
    if not CONFIG.get("api_key"):
        return False
    if _is_excluded_note_type(card):
        return False
    if not _has_enough_text(card):
        return False
    active = CONFIG.get("active_decks", [])
    if active:
        deck_name = mw.col.decks.name(card.did)
        if not any(a.lower() in deck_name.lower() for a in active):
            return False
    min_ivl = CONFIG.get("min_interval_days", 0)
    if card.ivl < min_ivl:
        return False
    return True


def should_transform(card) -> bool:
    """Decide whether this card should get a variant (includes random roll)."""
    if not _is_eligible(card):
        return False
    pct = CONFIG.get("transform_percent", 80)
    return random.randint(1, 100) <= pct


def should_prefetch(card) -> bool:
    """Check eligibility for pre-generation (no random roll)."""
    return _is_eligible(card)


# ---------------------------------------------------------------------------
# Core hook: replace question HTML
# ---------------------------------------------------------------------------

def on_card_will_show(text: str, card, kind: str) -> str:
    """Intercept card display. Replace question with variant if eligible."""
    global _current_variant, _current_variant_id, _current_card_id
    global _evaluation_text, _user_response

    try:
        if kind.endswith("Question"):
            global _returning_to_question

            # Re-showing the same card (back from answer) — preserve state
            if _returning_to_question and card.id == _current_card_id and _current_variant:
                _returning_to_question = False
                styled_variant = _wrap_variant_html(_current_variant)
                styled_variant += _feedback_buttons_html(card.id, _current_variant_id)
                if CONFIG.get("response_mode") == "freeform":
                    styled_variant += _freeform_input_html()
                return styled_variant

            _returning_to_question = False
            _evaluation_text = None
            _current_variant = None
            _current_variant_id = None
            _user_response = ""
            _current_card_id = card.id

            if not should_transform(card):
                return text

            # Use cached variant only — never block the UI with a sync API call
            result = _cache.get_variant(card.id)

            if result:
                _current_variant_id, _current_variant = result
                styled_variant = _wrap_variant_html(_current_variant)
                styled_variant += _feedback_buttons_html(card.id, _current_variant_id)
                if CONFIG.get("response_mode") == "freeform":
                    styled_variant += _freeform_input_html()
                return styled_variant

            return text

        elif kind.endswith("Answer"):
            if _current_variant and _current_variant_id is not None:
                peek_panel = _variant_peek_html()
                eval_html = ""
                if CONFIG.get("response_mode") == "freeform" and _user_response.strip():
                    eval_rendered = _render_saved_evaluation()
                    if eval_rendered:
                        eval_html = (
                            '<div style="margin-top: 12px; margin-bottom: 4px;'
                            ' font-size: 0.84em; color: #666;">'
                            '<b>Feedback w.r.t. canonical content</b></div>'
                            '<div style="margin-bottom: 8px;'
                            ' padding: 12px 14px; background: #f8f9fa;'
                            ' border: 1px solid #e0e0e0; border-radius: 6px;'
                            ' font-size: 0.9em; line-height: 1.5;">'
                            + eval_rendered + '</div>'
                        )
                return (
                    eval_html
                    + text
                    + _feedback_buttons_html(card.id, _current_variant_id)
                    + peek_panel
                )

            return text
    except Exception as e:
        _log(f"on_card_will_show error: {e}")

    return text


# ---------------------------------------------------------------------------
# Post-show hooks: pre-fetch next card & handle grading
# ---------------------------------------------------------------------------

def on_question_shown(card):
    """After showing question, pre-fetch next card and restore freeform state if returning."""
    _prefetch_next_card()
    _restore_freeform_state()


def _restore_freeform_state():
    """Re-inject saved response and evaluation into the question page after re-show."""
    if not _user_response.strip() or not _current_variant:
        return
    if not (mw.reviewer and mw.reviewer.web):
        return

    parts = []

    # Restore textarea value and disable it
    response_js = json.dumps(_user_response)
    parts.append(f"""
        var ta = document.getElementById('variant-response-input');
        if (ta) {{
            ta.value = {response_js};
            ta.disabled = true;
            ta.style.opacity = '0.5';
        }}
    """)

    # Restore evaluation if available
    if _evaluation_text:
        try:
            data = json.loads(_evaluation_text)
            rendered = _render_evaluation_html(data, mode="question")
            expected_rendered = None
            if isinstance(data, dict):
                expected_rendered = _render_expected_answer_content(data)
        except (json.JSONDecodeError, ValueError, TypeError):
            rendered = html.escape(str(_evaluation_text)).replace("\n", "<br>")
            expected_rendered = None

        if not expected_rendered:
            expected_rendered = (
                "<span style='color: #666; font-style: italic;'>"
                "Answer target unavailable.</span>"
            )

        eval_js = json.dumps(rendered)
        expected_js = json.dumps(expected_rendered)
        parts.append(f"""
            var el = document.getElementById('variant-evaluation');
            if (el) {{ el.innerHTML = {eval_js}; el.style.display = 'block'; }}
            var hdr = document.getElementById('variant-evaluation-header');
            if (hdr) {{ hdr.style.display = 'block'; }}
            var exp = document.getElementById('variant-expected-answer');
            if (exp) {{ exp.innerHTML = {expected_js}; exp.style.display = 'block'; }}
        """)

    if parts:
        js = "(function() {" + "".join(parts) + "})();"
        mw.reviewer.web.eval(js)


class _GradingWorker(QThread):
    """Background worker for LLM grading so the UI doesn't freeze."""
    done = pyqtSignal(object, str)    # (card_id, evaluation_json)
    failed = pyqtSignal(object, str)  # (card_id, error_message)

    def __init__(self, card_id, variant_question, user_response, canonical_answer, config):
        super().__init__()
        self._card_id = card_id
        self._variant_question = variant_question
        self._user_response = user_response
        self._canonical_answer = canonical_answer
        self._config = config

    def run(self):
        try:
            result = grade_response(
                variant_question=self._variant_question,
                user_response=self._user_response,
                canonical_answer=self._canonical_answer,
                config=self._config,
            )
            if result:
                self.done.emit(self._card_id, json.dumps(result))
            else:
                self.failed.emit(self._card_id, "LLM grading timed out or failed")
        except Exception as e:
            self.failed.emit(self._card_id, str(e))


def _cleanup_grading_worker():
    """Disconnect and schedule cleanup of any prior grading worker."""
    global _grading_worker
    if _grading_worker is not None:
        try:
            _grading_worker.done.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            _grading_worker.failed.disconnect()
        except (TypeError, RuntimeError):
            pass
        _grading_worker.deleteLater()
        _grading_worker = None


def _set_evaluation_message(message):
    # type: (str) -> None
    """Render a plain message in the evaluation box."""
    if not (mw.reviewer and mw.reviewer.web):
        return
    rendered = (
        "<span style='color: #666; font-style: italic;'>"
        + html.escape(message)
        + "</span>"
    )
    js_str = json.dumps(rendered)
    js = f"""
    (function() {{
        var el = document.getElementById('variant-evaluation');
        if (el) {{
            el.innerHTML = {js_str};
            el.style.display = 'block';
        }}
        var hdr = document.getElementById('variant-evaluation-header');
        if (hdr) {{ hdr.style.display = 'block'; }}
    }})();
    """
    mw.reviewer.web.eval(js)


def _set_expected_answer_message(message):
    # type: (str) -> None
    """Render a plain message in the expected-answer box."""
    if not (mw.reviewer and mw.reviewer.web):
        return
    rendered = (
        "<span style='color: #666; font-style: italic;'>"
        + html.escape(message)
        + "</span>"
    )
    js_str = json.dumps(rendered)
    js = f"""
    (function() {{
        var el = document.getElementById('variant-expected-answer');
        if (el) {{
            el.innerHTML = {js_str};
            el.style.display = 'block';
        }}
    }})();
    """
    mw.reviewer.web.eval(js)


def _card_ids_match(card_id):
    # type: (object) -> bool
    """Robustly compare signal card id with current card id."""
    try:
        return int(card_id) == int(_current_card_id)
    except Exception:
        return str(card_id) == str(_current_card_id)


def _render_saved_evaluation(mode="answer"):
    # type: (str) -> Optional[str]
    """Return rendered HTML for any saved evaluation text, or None."""
    if not _evaluation_text:
        return None
    try:
        data = json.loads(_evaluation_text)
        return _render_evaluation_html(data, mode=mode)
    except (json.JSONDecodeError, ValueError, TypeError):
        return html.escape(str(_evaluation_text)).replace("\n", "<br>")


def _render_expected_answer_content(data):
    # type: (dict) -> Optional[str]
    """Render answer-target text from grading payload."""
    expected_answer = str(data.get("expected_answer", "")).strip()

    if not expected_answer:
        canonical_points = data.get("canonical_points")
        if isinstance(canonical_points, list):
            points = []
            for item in canonical_points:
                text = str(item).strip()
                if text:
                    points.append(text)
                if len(points) >= 3:
                    break
            if points:
                expected_answer = "; ".join(points)

    if not expected_answer:
        return None

    return (
        "<div style='margin-bottom: 4px; color: #666; font-size: 0.84em;'>"
        "<b>AI answer target</b></div>"
        "<div>"
        + html.escape(expected_answer).replace("\n", "<br>")
        + "</div>"
    )


def _render_saved_expected_answer():
    # type: () -> Optional[str]
    """Return rendered expected-answer HTML when available."""
    if not _evaluation_text:
        return None
    try:
        data = json.loads(_evaluation_text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return _render_expected_answer_content(data)


def _cancel_grading_watchdog():
    # type: () -> None
    """Invalidate any pending grading watchdog callbacks."""
    global _grading_watchdog_seq
    _grading_watchdog_seq += 1


def _start_grading_watchdog(card_id):
    # type: (int) -> None
    """Show fallback text if grading gets stuck beyond timeout budget."""
    global _grading_watchdog_seq
    _grading_watchdog_seq += 1
    seq = _grading_watchdog_seq
    timeout_s = float(CONFIG.get("grading_timeout_s", 10))
    delay_ms = int((timeout_s + 2.0) * 1000)

    def _watchdog_fire():
        # type: () -> None
        if seq != _grading_watchdog_seq:
            return
        if card_id != _current_card_id:
            return
        if _grading_worker and _grading_worker.isRunning():
            _log(f"grading watchdog fired for card {card_id}")
            _set_evaluation_message(
                "Evaluation is taking longer than expected. "
                "You can grade manually."
            )

    QTimer.singleShot(delay_ms, _watchdog_fire)


def _safe_str_list(data, key):
    # type: (dict, str) -> list
    """Read a list-of-strings field from an already-normalized grading payload."""
    items = data.get(key)
    if not isinstance(items, list):
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def _coverage_cell_html(coverage_pct):
    # type: (Optional[int]) -> str
    """Build compact target-coverage content for a table cell."""
    if coverage_pct is None:
        return '<span style="color: #666; font-size: 0.9em;">n/a</span>'

    dark_gray = "#5f6368"
    light_gray = "#d9dce1"

    return (
        '<div style="display: flex; justify-content: center;">'
        '<div style="width: 52px; height: 52px; border-radius: 50%; '
        f'background: conic-gradient({dark_gray} {coverage_pct}%, '
        f'{light_gray} {coverage_pct}%); '
        'position: relative;">'
        '<div style="position: absolute; inset: 8px; border-radius: 50%; background: #fff; '
        'display: flex; align-items: center; justify-content: center; '
        'font-size: 0.74em; color: #444; font-weight: 600;">'
        f'{coverage_pct}%'
        "</div></div></div>"
    )


def _render_evaluation_html(data, mode="answer"):
    # type: (dict, str) -> str
    """Build color-coded HTML from structured grading data.

    mode="question": feedback vs AI answer — addressed, incorrect, related.
    mode="answer":   feedback vs canonical — addressed, remaining, incorrect, coverage donut.

    Expects data already normalized by generator._normalize_grading_payload.
    """
    incorrect = _safe_str_list(data, "incorrect")
    overall = str(data.get("overall", ""))
    alignment_note = str(data.get("alignment_note", ""))
    alignment = str(data.get("alignment", "aligned")).strip().lower()
    if alignment not in ("aligned", "partial", "misaligned"):
        alignment = "aligned"
    learning_feedback = _safe_str_list(data, "learning_feedback")
    covered_points = _safe_str_list(data, "covered_points")
    missed_points = _safe_str_list(data, "missed_points")
    coverage_pct = data.get("coverage_pct")  # already computed by normalizer

    if alignment == "misaligned":
        parts = [
            '<div style="margin-bottom: 8px; font-style: italic; color: #555;">'
            'Question drifted from canonical target.'
            '</div>'
        ]

        related = []
        seen = set()
        for item in (learning_feedback + covered_points + incorrect + missed_points):
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            related.append(text)

        detail = overall.strip() or alignment_note.strip()
        if detail and detail.lower() != "question drifted from canonical target.":
            parts.append(
                '<div style="margin-bottom: 8px; color: #4d4d4d;">'
                + html.escape(detail)
                + "</div>"
            )

        if related:
            bullets = "".join(
                f'<li style="margin-bottom: 4px;">{html.escape(str(item))}</li>'
                for item in related
            )
            parts.append(
                '<table style="width: 100%; border-collapse: collapse;'
                ' margin-bottom: 8px; table-layout: fixed; border: 1px solid #ddd;">'
                '<tr>'
                '<td style="padding: 6px 8px; font-weight: bold; color: #1e88e5;'
                ' background: #e3f2fd; border: 1px solid #ddd;'
                ' vertical-align: top;">Related</td>'
                '</tr>'
                '<tr>'
                '<td style="padding: 6px 8px; vertical-align: top; border: 1px solid #ddd;">'
                '<ul style="margin: 0; padding: 0 0 0 14px; font-size: 0.9em;">'
                + bullets +
                '</ul></td>'
                '</tr>'
                '</table>'
            )
        return "".join(parts)

    feedback_mode = str(CONFIG.get("feedback_mode", "both")).strip().lower()

    if mode == "question":
        # Question side: use ai_* fields when available (both/ai modes)
        if feedback_mode in ("ai", "both"):
            ai_covered = _safe_str_list(data, "ai_covered_points") or covered_points
            ai_missed = _safe_str_list(data, "ai_missed_points")
            ai_pct = data.get("ai_coverage_pct")
            columns = [
                {"items": ai_covered, "label": "Addressed",
                 "color": "#66bb6a", "bg": "#e8f5e9"},
                {"items": ai_missed, "label": "Missed",
                 "color": "#ffa726", "bg": "#fff8e1"},
                {"items": incorrect, "label": "Incorrect",
                 "color": "#ef5350", "bg": "#fce4ec"},
            ]
            if learning_feedback:
                columns.append({"items": learning_feedback, "label": "Related",
                                "color": "#1e88e5", "bg": "#e3f2fd"})
            if ai_pct is not None:
                columns.append({"items": None, "label": "AI coverage",
                                "color": "#5f6368", "bg": "#f2f3f5",
                                "coverage_pct": ai_pct})
        else:
            # canonical-only mode: minimal question side
            columns = [
                {"items": covered_points, "label": "Addressed",
                 "color": "#66bb6a", "bg": "#e8f5e9"},
                {"items": incorrect, "label": "Incorrect",
                 "color": "#ef5350", "bg": "#fce4ec"},
            ]
            if learning_feedback:
                columns.append({"items": learning_feedback, "label": "Related",
                                "color": "#1e88e5", "bg": "#e3f2fd"})
    else:
        if feedback_mode == "ai":
            # ai-only mode: no answer-side feedback
            return ""
        # Answer side: canonical coverage
        columns = [
            {"items": covered_points, "label": "Addressed",
             "color": "#66bb6a", "bg": "#e8f5e9"},
            {"items": missed_points, "label": "Remaining target points",
             "color": "#ffa726", "bg": "#fff8e1"},
            {"items": incorrect, "label": "Incorrect",
             "color": "#ef5350", "bg": "#fce4ec"},
        ]
        if learning_feedback:
            columns.append({"items": learning_feedback, "label": "Related",
                            "color": "#1e88e5", "bg": "#e3f2fd"})
        if coverage_pct is not None:
            columns.append({"items": None, "label": "Target coverage",
                            "color": "#5f6368", "bg": "#f2f3f5",
                            "coverage_pct": coverage_pct})

    # Only include populated columns (plus coverage meter column)
    active = []
    for col in columns:
        items = col.get("items")
        if items:
            active.append(col)
            continue
        if col.get("coverage_pct") is not None:
            active.append(col)

    if not active and not overall:
        return html.escape(str(data))

    parts = []

    header_lines = []
    if alignment == "partial":
        msg = "Partial alignment."
        if alignment_note:
            msg += " " + alignment_note
        header_lines.append(msg)
    if overall:
        header_lines.append(overall)

    if header_lines:
        parts.extend(
            '<div style="margin-bottom: 4px; font-style: italic; color: #555;">'
            + html.escape(line)
            + '</div>'
            for line in header_lines
        )

    # Separator + columnar table
    if active:
        n = len(active)
        pct = int(100 / n)
        cells_header = ""
        cells_body = ""
        for col in active:
            items = col.get("items")
            label = str(col.get("label", ""))
            color = str(col.get("color", "#666"))
            bg = str(col.get("bg", "#fafafa"))
            cells_header += (
                f'<td style="width:{pct}%; padding: 6px 8px; font-weight: bold;'
                f' color: {color}; background: {bg}; border: 1px solid #ddd;'
                f' vertical-align: top;">{label}</td>'
            )
            coverage_value = col.get("coverage_pct")
            if coverage_value is not None:
                body_html = _coverage_cell_html(coverage_value)
                cells_body += (
                    f'<td style="width:{pct}%; padding: 6px 8px; vertical-align: top;'
                    f' border: 1px solid #ddd; text-align: center;">'
                    f'{body_html}</td>'
                )
            else:
                bullets = "".join(
                    f'<li style="margin-bottom: 4px;">{html.escape(str(item))}</li>'
                    for item in items
                )
                cells_body += (
                    f'<td style="width:{pct}%; padding: 6px 8px; vertical-align: top;'
                    f' border: 1px solid #ddd;">'
                    f'<ul style="margin: 0; padding: 0 0 0 14px; font-size: 0.9em;">'
                    f'{bullets}</ul></td>'
                )
        parts.append(
            f'<table style="width: 100%; border-collapse: collapse;'
            f' margin-bottom: 8px; table-layout: fixed; border: 1px solid #ddd;">'
            f'<tr>{cells_header}</tr>'
            f'<tr>{cells_body}</tr>'
            f'</table>'
        )

    return "".join(parts)


def _on_grading_done(card_id, evaluation_json):
    """Callback on main thread when grading finishes."""
    global _evaluation_text

    still_on_card = _card_ids_match(card_id)

    if not still_on_card:
        _log(f"grading: discarding stale evaluation for card {card_id} "
             f"(current is {_current_card_id})")
        return

    _cancel_grading_watchdog()

    # Only persist evaluation if it belongs to the current card
    _evaluation_text = evaluation_json

    expected_rendered = None
    try:
        data = json.loads(evaluation_json)
        rendered = _render_evaluation_html(data, mode="question")
        if isinstance(data, dict):
            expected_rendered = _render_expected_answer_content(data)
    except (json.JSONDecodeError, ValueError, TypeError):
        rendered = html.escape(evaluation_json).replace("\n", "<br>")

    if not expected_rendered:
        expected_rendered = (
            "<span style='color: #666; font-style: italic;'>"
            "Answer target unavailable."
            "</span>"
        )

    if mw.reviewer and mw.reviewer.web:
        # json.dumps produces a valid JS string literal (with quotes)
        js_str = json.dumps(rendered)
        expected_js_str = json.dumps(expected_rendered)
        js = f"""
        (function() {{
            var el = document.getElementById('variant-evaluation');
            if (el) {{
                el.innerHTML = {js_str};
                el.style.display = 'block';
            }}
            var hdr = document.getElementById('variant-evaluation-header');
            if (hdr) {{ hdr.style.display = 'block'; }}
            var expectedEl = document.getElementById('variant-expected-answer');
            if (expectedEl) {{
                expectedEl.innerHTML = {expected_js_str};
                expectedEl.style.display = 'block';
            }}
        }})();
        """
        _log(f"grading: injecting evaluation ({len(rendered)} chars)")
        mw.reviewer.web.eval(js)


def _on_grading_failed(card_id, error_message):
    # type: (object, str) -> None
    """Show fallback text when grading fails/times out."""
    global _evaluation_text
    still_on_card = _card_ids_match(card_id)
    if not still_on_card:
        return
    _cancel_grading_watchdog()
    _log(f"grading failed for card {card_id}: {error_message}")
    _evaluation_text = "Evaluation unavailable (timeout). You can still grade manually."
    _set_evaluation_message(_evaluation_text)
    _set_expected_answer_message("Answer target unavailable.")


def _start_early_grading():
    """Start grading before answer flip so results arrive sooner."""
    global _grading_worker

    if (not _current_variant or _current_card_id is None
            or not _user_response.strip() or not _cache):
        return

    # Don't restart if already grading this card
    if _grading_worker and _grading_worker.isRunning():
        return

    try:
        card = mw.col.get_card(_current_card_id)
        answer_text = _extract_text(card, "answer")
        _cleanup_grading_worker()
        _log(f"grading: early start for card {_current_card_id}")
        _grading_worker = _GradingWorker(
            card_id=_current_card_id,
            variant_question=_current_variant,
            user_response=_user_response,
            canonical_answer=answer_text,
            config=CONFIG,
        )
        _grading_worker.done.connect(_on_grading_done)
        _grading_worker.failed.connect(_on_grading_failed)
        _grading_worker.start()
        _start_grading_watchdog(_current_card_id)
    except Exception as e:
        _log(f"early grading failed: {e}")


def on_answer_shown(card):
    """After answer shown in freeform mode, trigger async grading if not already started."""
    global _grading_worker

    if CONFIG.get("response_mode") != "freeform" or not _current_variant:
        return

    _log(f"on_answer_shown freeform: card={card.id}, response={len(_user_response)} chars")

    if not _user_response.strip():
        global _evaluation_text
        _evaluation_text = "No response captured. Type or dictate in the box, then press Enter."
        _cancel_grading_watchdog()
        _set_evaluation_message(_evaluation_text)
        return

    # Skip if early grading already started for this card
    if _grading_worker and _grading_worker.isRunning():
        _log("grading: already running (early start)")
        return

    _cleanup_grading_worker()

    answer_text = _extract_text(card, "answer")
    _log(f"grading: starting worker for card {card.id}")
    _grading_worker = _GradingWorker(
        card_id=card.id,
        variant_question=_current_variant,
        user_response=_user_response,
        canonical_answer=answer_text,
        config=CONFIG,
    )
    _grading_worker.done.connect(_on_grading_done)
    _grading_worker.failed.connect(_on_grading_failed)
    _grading_worker.start()
    _start_grading_watchdog(card.id)


# ---------------------------------------------------------------------------
# JS bridge: capture freeform text input
# ---------------------------------------------------------------------------

def on_js_message(handled: tuple, message: str, context):
    """Receive messages from injected JavaScript."""
    global _user_response

    if message.startswith("variantResponse:"):
        _user_response = message[len("variantResponse:"):]
        return (True, None)

    if message.startswith("variantFeedback:"):
        try:
            payload = message[len("variantFeedback:"):]
            parts = payload.split(":")
            if len(parts) >= 2:
                variant_id = int(parts[0])
                rating = int(parts[1])
            else:
                # Backward compatibility with older in-flight HTML.
                variant_id = _current_variant_id
                rating = int(payload)
            if variant_id and _cache:
                _cache.record_feedback(variant_id, rating)
        except Exception:
            pass
        return (True, None)

    if message.startswith("saveCardIdea"):
        card_id = None
        variant_id = None
        if ":" in message:
            try:
                _, card_s, variant_s = message.split(":", 2)
                card_id = int(card_s)
                variant_id = int(variant_s)
            except Exception:
                card_id = None
                variant_id = None
        _save_current_idea(card_id=card_id, variant_id=variant_id)
        return (True, None)

    if message == "startGrading":
        _start_early_grading()
        return (True, None)

    return handled


# ---------------------------------------------------------------------------
# Pre-fetching
# ---------------------------------------------------------------------------

def _prefetch_next_card():
    """Pre-generate variant for the next card in the review queue."""
    global _prefetch_worker

    if not CONFIG.get("api_key"):
        return

    if not mw.reviewer:
        return

    try:
        queued = mw.col.sched.get_queued_cards(fetch_limit=2)
        if not queued or not queued.cards:
            return

        # First card is the current one; take the second if available
        entries = list(queued.cards)
        if len(entries) < 2:
            return

        next_card = mw.col.get_card(entries[1].card.id)

        # Skip if a previous prefetch is still running
        if _prefetch_worker and _prefetch_worker.isRunning():
            return

        if should_prefetch(next_card) and not _cache.has_variant(next_card.id):
            question = _extract_text(next_card, "question")
            answer = _extract_text(next_card, "answer")

            # Clean up old worker before creating new one
            if _prefetch_worker is not None:
                _prefetch_worker.deleteLater()

            _prefetch_worker = PrefetchWorker(
                card_id=next_card.id,
                question=question,
                answer=answer,
                config=CONFIG,
                cache=_cache,
            )
            _prefetch_worker.start()
    except Exception as e:
        # Pre-fetching is best-effort; never break the review flow
        _log(f"prefetch error: {e}")


# ---------------------------------------------------------------------------
# Batch pre-fetch on session start
# ---------------------------------------------------------------------------

def on_state_did_change(new_state: str, old_state: str):
    """Start batch prefetch when entering review, cancel when leaving."""
    global _ideas_saved_this_session
    _log(f"state_did_change: {old_state} -> {new_state}")
    if new_state == "review":
        _ideas_saved_this_session = 0
        _start_batch_prefetch()
    elif old_state == "review":
        _cancel_batch_prefetch()
        if _ideas_saved_this_session > 0:
            n = _ideas_saved_this_session
            s = "s" if n != 1 else ""
            tooltip(
                f"{n} card idea{s} saved \u2014 review via "
                f"Tools \u2192 Proteus: Card Ideas",
                period=5000,
            )


def _start_batch_prefetch():
    """Pre-generate variants for the first N due cards."""
    global _batch_manager

    count = CONFIG.get("batch_prefetch_count", 15)
    if count <= 0 or not CONFIG.get("api_key"):
        return

    try:
        queued = mw.col.sched.get_queued_cards(fetch_limit=count)
    except Exception as e:
        _log(f"batch: could not get queued cards: {e}")
        return

    if not queued or not queued.cards:
        _log("batch: no cards in queue")
        return

    _log(f"batch: {len(queued.cards)} cards from scheduler")
    concurrency = CONFIG.get("batch_prefetch_concurrency", 3)
    debug = CONFIG.get("debug_logging", False)
    _batch_manager = BatchPrefetchManager(
        cache=_cache,
        config=CONFIG,
        max_concurrent=concurrency,
        debug=debug,
    )

    enqueued = 0
    for entry in queued.cards:
        card = mw.col.get_card(entry.card.id)
        if not should_prefetch(card):
            _log(f"  skip {card.id} (not eligible)")
            continue
        if _cache.has_variant(card.id):
            _log(f"  skip {card.id} (already cached)")
            continue

        question = _extract_text(card, "question")
        answer = _extract_text(card, "answer")
        _batch_manager.enqueue(card.id, question, answer)
        enqueued += 1

    if enqueued == 0:
        _log("batch: all cards already cached or ineligible")
        _batch_manager = None
        return

    _log(f"batch: enqueued {enqueued} cards, concurrency={concurrency}")

    if CONFIG.get("show_prefetch_progress", True):
        _batch_manager.progress.connect(_on_batch_progress)
        _batch_manager.all_done.connect(_on_batch_done)

    _batch_manager.start()

    if CONFIG.get("show_prefetch_progress", True):
        tooltip(f"Proteus: pre-generating variants (0/{enqueued})")


def _on_batch_progress(completed, total):
    # type: (int, int) -> None
    if CONFIG.get("show_prefetch_progress", True):
        tooltip(f"Proteus: pre-generating variants ({completed}/{total})")


def _on_batch_done():
    _log("batch: all done")
    if CONFIG.get("show_prefetch_progress", True):
        tooltip("Proteus: variants ready")


def _cancel_batch_prefetch():
    """Cancel any in-progress batch prefetch."""
    global _batch_manager
    if _batch_manager:
        _log("batch: cancelled (left reviewer)")
        _batch_manager.cancel()  # waits for workers to finish
        _batch_manager.deleteLater()
        _batch_manager = None


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _wrap_variant_html(variant: str) -> str:
    """Wrap variant question in styled HTML."""
    safe_variant = html.escape(variant)
    return f"""
    <div id="variant-question" style="
        position: relative;
        padding: 12px;
    ">
        <div style="
            font-size: 0.75em;
            color: #888;
            margin-bottom: 8px;
            font-style: italic;
        ">&#128256; variant question</div>
        <div>{safe_variant}</div>
    </div>
    """


def _feedback_buttons_html(card_id: int, variant_id: int) -> str:
    """Thumbs up/down buttons for rating variant quality, plus bookmark."""
    return """
    <div class="variant-feedback" style="
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 12px;
        font-size: 0.8em;
        color: #888;
    ">
        <span id="vf-label">Good variant?</span>
        <button id="vf-up" onclick="
            try {
                pycmd('variantFeedback:%d:1');
                document.getElementById('vf-up').style.opacity='1';
                document.getElementById('vf-down').style.opacity='0.3';
                document.getElementById('vf-label').textContent='Saved';
            } catch(e) {}
        " style="
            background: none; border: none; cursor: pointer;
            font-size: 1.3em; opacity: 0.5; padding: 2px 6px;
        " title="Good variant">&#128077;</button>
        <button id="vf-down" onclick="
            try {
                pycmd('variantFeedback:%d:-1');
                document.getElementById('vf-down').style.opacity='1';
                document.getElementById('vf-up').style.opacity='0.3';
                document.getElementById('vf-label').textContent='Saved';
            } catch(e) {}
        " style="
            background: none; border: none; cursor: pointer;
            font-size: 1.3em; opacity: 0.5; padding: 2px 6px;
        " title="Bad variant">&#128078;</button>
        <span style="border-left: 1px solid #ccc; height: 1.2em; margin: 0 4px;"></span>
        <button id="vf-save" onclick="
            try {
                pycmd('saveCardIdea:%d:%d');
                var btn = document.getElementById('vf-save');
                btn.textContent = '\\u2713';
                btn.disabled = true;
                btn.title = 'Saved';
            } catch(e) {}
        " style="
            background: none; border: none; cursor: pointer;
            font-size: 1.3em; opacity: 0.5; padding: 2px 6px;
        " title="Save card idea">&#128278;</button>
    </div>
    """ % (int(variant_id), int(variant_id), int(card_id), int(variant_id))


def _freeform_input_html() -> str:
    """HTML for the freeform text response area with inline grading placeholders."""
    return (
        '<div id="variant-response-area" style="'
        'margin-top: 20px; padding: 12px; border-top: 1px solid #ddd;">'
        '<div style="font-size: 0.85em; color: #666; margin-bottom: 6px;">'
        'Speak or type your response:</div>'
        '<textarea id="variant-response-input" rows="4" style="'
        'width: 100%; box-sizing: border-box; font-size: 1em; padding: 8px;'
        ' border: 1px solid #ccc; border-radius: 4px; resize: vertical;'
        ' font-family: inherit;"'
        ' placeholder="Click here, then use Wispr Flow or type..."'
        """ oninput="pycmd('variantResponse:' + this.value)" """
        ' onkeydown="'
        """if (event.key === 'Enter' && !event.shiftKey) {"""
        ' event.preventDefault(); event.stopPropagation();'
        """ pycmd('variantResponse:' + this.value);"""
        """ pycmd('startGrading');"""
        " this.disabled = true; this.style.opacity = '0.5';"
        '}"'
        '></textarea></div>'
        # Placeholders for inline grading results (filled by JS when ready)
        '<div id="variant-expected-answer" style="display: none;'
        ' margin-top: 12px; padding: 10px 12px; background: #f8f9fa;'
        ' border: 1px solid #dcdfe3; border-radius: 6px;'
        ' font-size: 0.9em; line-height: 1.45;"></div>'
        + (
            '<div id="variant-evaluation-header" style="display: none;'
            ' margin-top: 12px; margin-bottom: 4px; font-size: 0.84em;'
            ' color: #666;"><b>Feedback w.r.t. AI answer</b></div>'
            if CONFIG.get("feedback_mode", "both") != "canonical" else
            '<div id="variant-evaluation-header" style="display: none;'
            ' margin-top: 12px; margin-bottom: 4px; font-size: 0.84em;'
            ' color: #666;"><b>Feedback w.r.t. canonical content</b></div>'
        ) +
        '<div id="variant-evaluation" style="display: none;'
        ' margin-bottom: 8px; padding: 12px 14px;'
        ' background: #f8f9fa; border: 1px solid #e0e0e0;'
        ' border-radius: 6px; font-size: 0.9em; line-height: 1.5;"></div>'
        '<script>'
        '(function() {'
        ' if (window._proteusPollingId) { clearInterval(window._proteusPollingId); }'
        ' var ta = document.getElementById("variant-response-input");'
        ' if (ta) {'
        '  setTimeout(function() { ta.focus(); }, 100);'
        '  var _lastVal = "";'
        '  window._proteusPollingId = setInterval(function() {'
        '   if (ta.value !== _lastVal) { _lastVal = ta.value;'
        '    pycmd("variantResponse:" + ta.value); }'
        '  }, 500);'
        ' }'
        '})();'
        '</script>'
    )



def _variant_peek_html() -> str:
    """Hidden answer-side panel that can re-show the variant prompt on demand."""
    variant = html.escape(str(_current_variant or ""))
    response = html.escape(str(_user_response or "").strip())
    response_html = ""
    if response:
        response_html = (
            "<div style='margin-top: 8px; color: #666; font-size: 0.85em;'>"
            "<b>Your response:</b> "
            + response
            + "</div>"
        )
    return (
        '<div id="proteus-variant-peek" style="display: none; margin-top: 12px;'
        ' margin-bottom: 8px; padding: 10px 12px; background: #fafafa;'
        ' border: 1px solid #e6e6e6; border-radius: 6px; font-size: 0.9em; line-height: 1.5;">'
        "<div style='color: #666; font-size: 0.85em; margin-bottom: 6px;'>"
        "<b>Variant prompt (peek)</b>"
        "</div>"
        "<div>" + variant + "</div>"
        + response_html +
        "</div>"
    )


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_text(card, side: str) -> str:
    """Extract plain text from a card's question or answer side."""
    if side == "question":
        text = card.question()
    else:
        text = card.answer()

    # Strip style/script blocks (tags + content), then remaining HTML tags
    text = re.sub(r'<(style|script)[^>]*>.*?</\1>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ---------------------------------------------------------------------------
# Card ideas
# ---------------------------------------------------------------------------

_IDEA_REASON_TAGS = [
    ("", "No tag"),
    ("unclear", "Unclear"),
    ("too_easy", "Too easy"),
    ("too_hard", "Too hard"),
    ("awkward_wording", "Awkward wording"),
    ("duplicate_concept", "Duplicate concept"),
    ("promising_direction", "Promising direction"),
]


def _idea_working_text(idea):
    # type: (dict) -> str
    """Return edited draft if present, otherwise the original generated variant."""
    edited = idea.get("edited_variant_text")
    if edited and edited.strip():
        return edited.strip()
    return str(idea.get("variant_text", "")).strip()


def _idea_working_answer(idea):
    # type: (dict) -> str
    """Return edited answer draft if present, otherwise the original answer."""
    edited = idea.get("edited_answer_text")
    if edited and edited.strip():
        return edited.strip()
    return str(idea.get("original_answer", "")).strip()


def _card_shape_guardrail_issues(question_text, answer_text):
    # type: (str, str) -> list
    """Return blocking issues for non-atomic card shapes."""
    question = str(question_text or "").strip()
    answer = str(answer_text or "").strip()
    issues = []

    if question.count("?") > 1:
        issues.append("question has multiple asks (more than one '?').")

    # Catch patterns like "What ... and what ..." that usually encode two prompts.
    if re.search(r"\b(and|or)\s+(what|which|why|how|when|where|who)\b", question.lower()):
        issues.append("question appears to chain multiple prompts.")

    answer_lines = [ln.strip() for ln in answer.splitlines() if ln.strip()]
    if len(answer_lines) >= 3:
        issues.append("answer spans several lines and likely contains multiple facts.")

    answer_parts = [p.strip() for p in re.split(r"[;,]", answer) if p.strip()]
    if len(answer_parts) >= 4 and len(answer.split()) > 18:
        issues.append("answer looks like a list; split into separate cards.")

    if len(answer.split()) > 45:
        issues.append("answer is too long for a focused recall target.")

    return issues


def _regenerate_idea_variant(idea, instruction, current_text):
    # type: (dict, str, Optional[str]) -> Optional[str]
    """Regenerate a variant with an explicit human instruction."""
    if not CONFIG.get("api_key"):
        return None

    cfg = dict(CONFIG)
    base = cfg.get("system_prompt", "").strip()
    extra_parts = [
        "Human editing request for this single rewrite:",
        instruction,
    ]
    if current_text:
        extra_parts.extend([
            "",
            "Current draft variant:",
            current_text.strip()[:500],
        ])
    extra_parts.extend([
        "",
        "Keep the same underlying concept and answer target.",
        "Return only the rewritten question text.",
    ])
    extra = "\n".join(extra_parts)
    cfg["system_prompt"] = f"{base}\n\n{extra}" if base else extra

    return generate_variant(
        question=str(idea.get("original_question", "")),
        answer=str(idea.get("original_answer", "")),
        config=cfg,
    )


class _IdeaRegenerateWorker(QThread):
    """Background worker for directed idea regeneration in the card ideas dialog."""
    done = pyqtSignal(int, str)    # idea_id, regenerated_text
    failed = pyqtSignal(int, str)  # idea_id, error_message

    def __init__(self, idea_id, idea, instruction, current_text):
        # type: (int, dict, str, str) -> None
        super().__init__()
        self._idea_id = idea_id
        self._idea = dict(idea)
        self._instruction = instruction
        self._current_text = current_text

    def run(self):
        try:
            regenerated = _regenerate_idea_variant(
                self._idea,
                self._instruction,
                self._current_text,
            )
            if regenerated:
                self.done.emit(self._idea_id, regenerated)
            else:
                self.failed.emit(self._idea_id, "empty regeneration result")
        except Exception as e:
            self.failed.emit(self._idea_id, str(e))


def _decision_label(status):
    # type: (str) -> str
    labels = {
        "pending": "Pending",
        "edited_pending": "Edited (pending)",
        "accepted": "Accepted",
        "edited_accepted": "Edited + accepted",
        "rejected": "Rejected",
    }
    return labels.get(status, status.replace("_", " ").title())


def _selected_reason(combo):
    # type: (object) -> Optional[str]
    reason = combo.currentData()
    if not reason:
        return None
    return str(reason)


def _idea_has_feedback(idea):
    # type: (dict) -> bool
    """True only when an idea includes non-empty grading feedback."""
    evaluation = idea.get("evaluation")
    if evaluation is None:
        return False
    return bool(str(evaluation).strip())


def _save_current_idea(card_id=None, variant_id=None):
    # type: (Optional[int], Optional[int]) -> None
    """Save the current (or explicitly identified) variant as a card idea."""
    global _ideas_saved_this_session
    try:
        if not _cache:
            return

        target_card_id = card_id if card_id is not None else _current_card_id
        target_variant_id = variant_id if variant_id is not None else _current_variant_id
        variant_text = _current_variant
        rating = None

        if target_variant_id is not None:
            row = _cache.get_variant_by_id(target_variant_id)
            if row:
                db_card_id, db_variant_text, db_rating = row
                target_card_id = int(db_card_id)
                variant_text = str(db_variant_text or "")
                rating = db_rating

        if not variant_text or target_card_id is None:
            return

        card = mw.col.get_card(int(target_card_id))
        if not card:
            return
        orig_q = _extract_text(card, "question")
        orig_a = _extract_text(card, "answer")

        evaluation = None
        if _current_card_id == target_card_id:
            evaluation = _evaluation_text

        _cache.save_idea(
            card_id=target_card_id,
            variant_text=variant_text,
            original_question=orig_q,
            original_answer=orig_a,
            rating=rating,
            evaluation=evaluation,
        )
        _ideas_saved_this_session += 1
        tooltip("Card idea saved")
    except Exception as e:
        _log(f"save_idea failed: {e}")


def show_card_ideas_dialog():
    """Show dialog listing saved card ideas."""
    try:
        from aqt.qt import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel,
            QPushButton, QScrollArea, QWidget, QFrame,
            QComboBox, QPlainTextEdit,
        )

        if not _cache:
            showInfo("Proteus: cache not initialized")
            return

        dlg = QDialog(mw)
        dlg.setWindowTitle("Proteus: Card Ideas")
        dlg.setMinimumWidth(480)
        dlg.setMinimumHeight(400)
        outer = QVBoxLayout()

        header = QLabel()
        header.setWordWrap(True)
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        ideas_layout = QVBoxLayout()
        container.setLayout(ideas_layout)
        scroll.setWidget(container)
        outer.addWidget(scroll)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        outer.addWidget(close_btn)

        dlg.setLayout(outer)
        regen_workers = []  # type: list

        def refresh():
            # Clear existing widgets
            while ideas_layout.count():
                item = ideas_layout.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()

            ideas = _cache.get_ideas(include_used=False)
            header.setText(f"<b>{len(ideas)} pending card idea{'s' if len(ideas) != 1 else ''}</b>")

            if not ideas:
                empty = QLabel("<i>No pending ideas.</i>")
                empty.setWordWrap(True)
                ideas_layout.addWidget(empty)
                ideas_layout.addStretch()
                return

            for idea in ideas:
                frame = QFrame()
                frame.setFrameShape(QFrame.Shape.StyledPanel)
                frame.setStyleSheet("QFrame { margin-bottom: 6px; padding: 8px; }")
                fl = QVBoxLayout()

                raw_variant = str(idea.get("variant_text", ""))
                working_variant = _idea_working_text(idea)
                original_answer = str(idea.get("original_answer", ""))
                working_answer = _idea_working_answer(idea)
                decision_status = str(idea.get("decision_status", "pending"))
                decision_reason = idea.get("decision_reason")
                has_feedback = _idea_has_feedback(idea)

                status_lbl = QLabel(
                    f"<span style='color: #555;'><b>Status:</b> "
                    f"{html.escape(_decision_label(decision_status))}</span>"
                )
                status_lbl.setWordWrap(True)
                fl.addWidget(status_lbl)

                raw_lbl = QLabel(
                    f"<span style='color: #666;'><b>Original variant:</b> "
                    f"{html.escape(raw_variant[:260])}</span>"
                )
                raw_lbl.setWordWrap(True)
                fl.addWidget(raw_lbl)

                draft_lbl = QLabel("<span style='color: #444;'><b>Working draft (editable)</b></span>")
                draft_lbl.setWordWrap(True)
                fl.addWidget(draft_lbl)

                draft_edit = QPlainTextEdit()
                draft_edit.setPlainText(working_variant)
                draft_edit.setMinimumHeight(72)
                fl.addWidget(draft_edit)

                regen_row = QHBoxLayout()
                short_btn = QPushButton("Shorter")
                concrete_btn = QPushButton("More Concrete")
                jargon_btn = QPushButton("Less Jargon")
                contrast_btn = QPushButton("Add Contrast Case")
                regen_row.addWidget(short_btn)
                regen_row.addWidget(concrete_btn)
                regen_row.addWidget(jargon_btn)
                regen_row.addWidget(contrast_btn)
                fl.addLayout(regen_row)
                regen_buttons = [short_btn, concrete_btn, jargon_btn, contrast_btn]

                tag_row = QHBoxLayout()
                tag_row.addWidget(QLabel("Tag:"))
                reason_combo = QComboBox()
                for key, label in _IDEA_REASON_TAGS:
                    reason_combo.addItem(label, key)
                if decision_reason:
                    idx = reason_combo.findData(decision_reason)
                    if idx >= 0:
                        reason_combo.setCurrentIndex(idx)
                tag_row.addWidget(reason_combo)
                fl.addLayout(tag_row)

                orig_q_lbl = QLabel(
                    f"<span style='color: #888;'>Q: {html.escape(idea['original_question'][:200])}</span>"
                )
                orig_q_lbl.setWordWrap(True)
                fl.addWidget(orig_q_lbl)

                orig_a_lbl = QLabel(
                    f"<span style='color: #888;'>Original A: {html.escape(original_answer[:200])}</span>"
                )
                orig_a_lbl.setWordWrap(True)
                fl.addWidget(orig_a_lbl)

                answer_draft_lbl = QLabel(
                    "<span style='color: #444;'><b>Answer draft (editable)</b></span>"
                )
                answer_draft_lbl.setWordWrap(True)
                fl.addWidget(answer_draft_lbl)

                answer_edit = QPlainTextEdit()
                answer_edit.setPlainText(working_answer)
                answer_edit.setMinimumHeight(64)
                fl.addWidget(answer_edit)

                answer_btn_row = QHBoxLayout()
                answer_reset_btn = QPushButton("Reset Answer")
                answer_btn_row.addWidget(answer_reset_btn)
                fl.addLayout(answer_btn_row)

                if idea['rating'] is not None:
                    badge = "\U0001f44d" if idea['rating'] > 0 else "\U0001f44e"
                    rating_lbl = QLabel(f"Rating: {badge}")
                    fl.addWidget(rating_lbl)

                eval_json = idea.get('evaluation')
                if eval_json:
                    try:
                        eval_data = json.loads(eval_json)
                        overall = str(eval_data.get('overall', '')).strip()
                        alignment = str(eval_data.get("alignment", "aligned")).strip().lower()
                        if alignment not in ("aligned", "partial", "misaligned"):
                            alignment = "aligned"
                        coverage_pct = eval_data.get("coverage_pct")
                        eval_parts = []
                        if overall:
                            eval_parts.append(html.escape(overall))
                        if alignment == "misaligned":
                            eval_parts.append("drifted")
                        elif coverage_pct is not None:
                            eval_parts.append(f"{coverage_pct}% coverage")
                        if eval_parts:
                            eval_lbl = QLabel(
                                f"<span style='color: #555; font-style: italic;'>"
                                f"AI: {' &mdash; '.join(eval_parts)}</span>"
                            )
                            eval_lbl.setWordWrap(True)
                            fl.addWidget(eval_lbl)
                    except (json.JSONDecodeError, ValueError):
                        pass

                btn_row = QHBoxLayout()
                save_btn = QPushButton("Save Edit")
                create_btn = QPushButton("Create Card")
                dismiss_btn = QPushButton("Reject")
                if not has_feedback:
                    create_btn.setEnabled(False)
                    create_btn.setToolTip(
                        "Requires freeform grading feedback "
                        "(from Wispr/typed response)."
                    )

                idea_id = idea['id']

                def make_regen(i=idea, iid=idea_id, editor=draft_edit,
                               reason=reason_combo, status=status_lbl):
                    # type: (dict, int, QPlainTextEdit, QComboBox, QLabel) -> object
                    def set_regen_enabled(enabled):
                        # type: (bool) -> None
                        for btn in regen_buttons:
                            btn.setEnabled(enabled)

                    def on_click(instruction):
                        current = editor.toPlainText().strip()
                        if not current:
                            tooltip("Proteus: draft cannot be empty")
                            return
                        set_regen_enabled(False)
                        status.setText(
                            "<span style='color: #555;'><b>Status:</b> "
                            "Regenerating...</span>"
                        )
                        worker = _IdeaRegenerateWorker(iid, i, instruction, current)
                        regen_workers.append(worker)

                        def on_done(done_iid, regenerated):
                            # type: (int, str) -> None
                            if done_iid != iid:
                                return
                            _cache.update_idea_edit(iid, regenerated)
                            _cache.set_idea_decision(
                                iid,
                                "edited_pending",
                                _selected_reason(reason),
                                mark_used=False,
                            )
                            try:
                                editor.setPlainText(regenerated)
                                status.setText(
                                    "<span style='color: #555;'><b>Status:</b> "
                                    "Edited (pending)</span>"
                                )
                            except RuntimeError:
                                pass
                            tooltip("Proteus: regenerated draft saved")

                        def on_failed(failed_iid, msg):
                            # type: (int, str) -> None
                            if failed_iid != iid:
                                return
                            try:
                                status.setText(
                                    "<span style='color: #555;'><b>Status:</b> "
                                    "Pending</span>"
                                )
                            except RuntimeError:
                                pass
                            _log(f"idea regeneration failed for {iid}: {msg}")
                            tooltip("Proteus: regeneration failed")

                        def on_finished():
                            # type: () -> None
                            global _idea_orphan_workers
                            try:
                                set_regen_enabled(True)
                            except RuntimeError:
                                pass
                            try:
                                regen_workers.remove(worker)
                            except ValueError:
                                pass
                            try:
                                _idea_orphan_workers.remove(worker)
                            except ValueError:
                                pass
                            worker.deleteLater()

                        worker.done.connect(on_done)
                        worker.failed.connect(on_failed)
                        worker.finished.connect(on_finished)
                        worker.start()
                    return on_click

                def make_save(iid=idea_id, editor=draft_edit, reason=reason_combo,
                              original_text=raw_variant, answer_editor=answer_edit,
                              original_answer_text=original_answer):
                    # type: (int, QPlainTextEdit, QComboBox, str, QPlainTextEdit, str) -> object
                    def on_click():
                        edited = editor.toPlainText().strip()
                        edited_answer = answer_editor.toPlainText().strip()
                        if not edited:
                            tooltip("Proteus: draft cannot be empty")
                            return
                        if not edited_answer:
                            tooltip("Proteus: answer draft cannot be empty")
                            return
                        question_changed = edited != original_text.strip()
                        answer_changed = edited_answer != original_answer_text.strip()
                        status = "edited_pending" if (question_changed or answer_changed) else "pending"
                        _cache.update_idea_edit(iid, edited)
                        _cache.update_idea_answer_edit(iid, edited_answer)
                        _cache.set_idea_decision(
                            iid,
                            status,
                            _selected_reason(reason),
                            mark_used=False,
                        )
                        tooltip("Proteus: draft saved")
                        refresh()
                    return on_click

                def make_create(i=idea, iid=idea_id, editor=draft_edit,
                                reason=reason_combo, original_text=raw_variant,
                                answer_editor=answer_edit, original_answer_text=original_answer):
                    # type: (dict, int, QPlainTextEdit, QComboBox, str, QPlainTextEdit, str) -> object
                    def on_click():
                        if not _idea_has_feedback(i):
                            tooltip("Proteus: Create Card requires freeform feedback first")
                            return
                        edited = editor.toPlainText().strip()
                        edited_answer = answer_editor.toPlainText().strip()
                        if not edited:
                            tooltip("Proteus: draft cannot be empty")
                            return
                        if not edited_answer:
                            tooltip("Proteus: answer draft cannot be empty")
                            return
                        issues = _card_shape_guardrail_issues(edited, edited_answer)
                        if issues:
                            suffix = ""
                            if len(issues) > 1:
                                suffix = " (+{} more)".format(len(issues) - 1)
                            tooltip(
                                "Proteus: guardrail blocked create: {}{}".format(
                                    issues[0], suffix
                                )
                            )
                            return
                        edited_accept = (
                            edited != original_text.strip()
                            or edited_answer != original_answer_text.strip()
                        )
                        status = "edited_accepted" if edited_accept else "accepted"
                        _cache.update_idea_edit(iid, edited)
                        _cache.update_idea_answer_edit(iid, edited_answer)
                        _cache.set_idea_decision(
                            iid,
                            status,
                            _selected_reason(reason),
                            mark_used=True,
                        )
                        idea_for_create = dict(i)
                        idea_for_create["variant_text"] = edited
                        idea_for_create["edited_variant_text"] = edited
                        idea_for_create["edited_answer_text"] = edited_answer
                        _open_add_note_with_idea(idea_for_create)
                        refresh()
                    return on_click

                def make_dismiss(iid=idea_id, editor=draft_edit, reason=reason_combo,
                                 answer_editor=answer_edit):
                    # type: (int, QPlainTextEdit, QComboBox, QPlainTextEdit) -> object
                    def on_click():
                        edited = editor.toPlainText().strip()
                        edited_answer = answer_editor.toPlainText().strip()
                        if edited:
                            _cache.update_idea_edit(iid, edited)
                        if edited_answer:
                            _cache.update_idea_answer_edit(iid, edited_answer)
                        _cache.set_idea_decision(
                            iid,
                            "rejected",
                            _selected_reason(reason),
                            mark_used=True,
                        )
                        refresh()
                    return on_click

                def make_reset_answer(answer_editor=answer_edit, original_answer_text=original_answer):
                    # type: (QPlainTextEdit, str) -> object
                    def on_click():
                        answer_editor.setPlainText(original_answer_text)
                    return on_click

                regen_handler = make_regen()
                short_btn.clicked.connect(lambda _=False, h=regen_handler: h(
                    "Rewrite this to be shorter and simpler without losing the tested concept."
                ))
                concrete_btn.clicked.connect(lambda _=False, h=regen_handler: h(
                    "Rewrite with a concrete real-world scenario while testing the same idea."
                ))
                jargon_btn.clicked.connect(lambda _=False, h=regen_handler: h(
                    "Rewrite with less jargon and clearer wording at the same difficulty."
                ))
                contrast_btn.clicked.connect(lambda _=False, h=regen_handler: h(
                    "Rewrite by adding a contrast or failure case that still targets the same answer."
                ))

                save_btn.clicked.connect(make_save())
                create_btn.clicked.connect(make_create())
                dismiss_btn.clicked.connect(make_dismiss())
                answer_reset_btn.clicked.connect(make_reset_answer())
                btn_row.addWidget(save_btn)
                btn_row.addWidget(create_btn)
                btn_row.addWidget(dismiss_btn)
                fl.addLayout(btn_row)

                frame.setLayout(fl)
                ideas_layout.addWidget(frame)

            ideas_layout.addStretch()

        def _wait_for_regen_workers(_result=0):
            # type: (int) -> None
            global _idea_orphan_workers
            for worker in list(regen_workers):
                if worker.isRunning():
                    worker.wait(5000)
                if worker.isRunning():
                    # Keep a reference so a running QThread is not destroyed.
                    _idea_orphan_workers.append(worker)

                    def _cleanup_orphan(w=worker):
                        # type: (object) -> None
                        global _idea_orphan_workers
                        try:
                            _idea_orphan_workers.remove(w)
                        except ValueError:
                            pass
                        w.deleteLater()

                    worker.finished.connect(_cleanup_orphan)
                    continue
                worker.deleteLater()
            regen_workers[:] = []

        dlg.finished.connect(_wait_for_regen_workers)
        refresh()
        dlg.exec()
    except Exception as e:
        showInfo(f"Proteus: card ideas dialog error: {e}")


def _open_add_note_with_idea(idea):
    """Open the Add Note dialog pre-filled with an idea's content."""
    try:
        if not _idea_has_feedback(idea):
            tooltip("Proteus: cannot create card without freeform feedback")
            return
        from aqt.addcards import AddCards

        add_dlg = AddCards(mw)
        try:
            note = add_dlg.editor.note
            if note and len(note.fields) >= 2:
                front = idea.get('edited_variant_text') or idea.get('variant_text', '')
                back = idea.get('edited_answer_text') or idea.get('original_answer', '')
                note.fields[0] = front
                note.fields[1] = back
                add_dlg.editor.loadNote()
        except Exception:
            pass  # different note types may have different layouts
    except Exception as e:
        showInfo(f"Proteus: could not open Add Note: {e}")


# ---------------------------------------------------------------------------
# Usage stats dialog
# ---------------------------------------------------------------------------

def _estimate_cost(input_t: int, output_t: int) -> float:
    """Estimate USD cost using Sonnet pricing: $3/M input, $15/M output."""
    return (input_t * 3.0 + output_t * 15.0) / 1_000_000


def _usage_stats_text(usage: dict, budget: float) -> str:
    """Build rich-text for the stats label (no bar — QLabel can't render divs)."""
    input_t = usage["input_tokens"]
    output_t = usage["output_tokens"]
    calls = usage["api_calls"]
    est_cost = _estimate_cost(input_t, output_t)

    return (
        f"<b>API Calls:</b> {calls:,}<br>"
        f"<b>Input Tokens:</b> {input_t:,}<br>"
        f"<b>Output Tokens:</b> {output_t:,}<br>"
        f"<b>Total Tokens:</b> {input_t + output_t:,}<br><br>"
        f"<b>Estimated Cost:</b> ${est_cost:.4f} / ${budget:.2f} budget"
    )


def _budget_pct(usage: dict, budget: float) -> int:
    """Return budget usage as an integer percentage (capped at 100)."""
    if budget <= 0:
        return 0
    est_cost = _estimate_cost(usage["input_tokens"], usage["output_tokens"])
    return min(int(est_cost / budget * 100), 100)


def _budget_bar_text(pct: int) -> str:
    """Build a text-based progress bar: [████████░░░░] 42%"""
    width = 20
    filled = int(width * pct / 100)
    empty = width - filled
    if pct < 75:
        color = "#4caf50"
    elif pct < 100:
        color = "#ff9800"
    else:
        color = "#f44336"
    bar = "\u2588" * filled + "\u2591" * empty
    return (
        f"<code><span style='color: {color};'>{bar}</span></code> "
        f"<b>{pct}%</b> of budget"
    )


def show_usage_dialog():
    """Show a dialog with accumulated API usage stats and budget bar."""
    try:
        from aqt.qt import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout

        usage = get_usage()
        budget = CONFIG.get("usage_budget", 5.00)
        pct = _budget_pct(usage, budget)

        dlg = QDialog(mw)
        dlg.setWindowTitle("Proteus Usage Stats")
        dlg.setMinimumWidth(340)
        layout = QVBoxLayout()

        label = QLabel(_usage_stats_text(usage, budget))
        label.setWordWrap(True)
        layout.addWidget(label)

        bar_label = QLabel(_budget_bar_text(pct))
        bar_label.setWordWrap(True)
        layout.addWidget(bar_label)

        footer = QLabel(
            "<span style='color: #888; font-size: 0.85em;'>"
            "Based on Sonnet pricing ($3/M in, $15/M out)<br>"
            "Budget configurable in Proteus Settings</span>"
        )
        footer.setWordWrap(True)
        layout.addWidget(footer)

        btn_layout = QHBoxLayout()
        reset_btn = QPushButton("Reset")
        close_btn = QPushButton("Close")

        def on_reset():
            reset_usage()
            new_usage = get_usage()
            new_pct = _budget_pct(new_usage, budget)
            label.setText(_usage_stats_text(new_usage, budget))
            bar_label.setText(_budget_bar_text(new_pct))
            tooltip("Proteus: usage stats reset")

        reset_btn.clicked.connect(on_reset)
        close_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(reset_btn)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        dlg.setLayout(layout)
        dlg.exec()
    except Exception as e:
        showInfo(f"Proteus: usage dialog error: {e}")


# ---------------------------------------------------------------------------
# Config dialog
# ---------------------------------------------------------------------------

def show_config_dialog():
    """Open the addon config in Anki's built-in config editor."""
    addon_module = __name__.split(".")[0]
    try:
        from aqt.addons import ConfigEditor
        dialog = ConfigEditor(mw, addon_module)
        if dialog.exec():
            _on_config_updated(None)
    except Exception:
        showInfo("Open config via: Tools → Add-ons → select Proteus → Config")


def _on_config_updated(conf):
    """Reload config and update cache settings."""
    global CONFIG
    CONFIG = load_config()
    if _cache:
        _cache._max_variants = CONFIG.get("max_cached_variants", 3)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

_initialized = False

def _try_init():
    global _initialized
    if _initialized:
        return
    try:
        init_addon()
        _initialized = True
    except Exception as e:
        showInfo(f"Proteus: init failed: {e}")

def _try_cleanup():
    """Close cache on profile unload."""
    global _cache
    if _cache:
        _cache.close()
        _cache = None

gui_hooks.profile_did_open.append(_try_init)
gui_hooks.profile_will_close.append(_try_cleanup)

# Also try after a short delay in case profile_did_open already fired
from aqt.qt import QTimer
QTimer.singleShot(3000, lambda: _try_init() if mw.col else None)
