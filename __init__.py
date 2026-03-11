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
import time as _time
from aqt import mw, gui_hooks
from aqt.utils import showInfo, tooltip
from aqt.qt import QAction, QThread, pyqtSignal, QObject
from aqt.webview import AnkiWebView

from .generator import generate_variant, grade_response, get_usage, reset_usage
from .cache import VariantCache
from .prefetch import PrefetchWorker
from .batch_prefetch import BatchPrefetchManager

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ADDON_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(ADDON_DIR, "config.json")
_LOG_PATH = os.path.join(ADDON_DIR, "proteus_diag.log")

def _log(msg: str):
    """Append a timestamped line to the diag log (only when debug_logging is on)."""
    if not CONFIG.get("debug_logging", False):
        return
    ts = _time.strftime("%H:%M:%S")
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass

def load_config():
    """Load config, merging user overrides with defaults."""
    defaults = {
        "api_key": "",
        "model": "claude-sonnet-4-20250514",
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
_ideas_saved_this_session: int = 0

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


# ---------------------------------------------------------------------------
# Card eligibility
# ---------------------------------------------------------------------------

def _is_excluded_note_type(card) -> bool:
    """Check if a card's note type is in the exclusion list."""
    try:
        note_name = card.note_type()["name"].lower()
        excluded = CONFIG.get("exclude_note_types", [])
        return any(e.lower() in note_name for e in excluded)
    except Exception:
        return False


def should_transform(card) -> bool:
    """Decide whether this card should get a variant."""
    if not CONFIG.get("api_key"):
        return False

    if _is_excluded_note_type(card):
        return False

    # Check deck filter
    active = CONFIG.get("active_decks", [])
    if active:
        deck_name = mw.col.decks.name(card.did)
        if not any(a.lower() in deck_name.lower() for a in active):
            return False

    # Check interval threshold
    min_ivl = CONFIG.get("min_interval_days", 0)
    if card.ivl < min_ivl:
        return False

    # Probabilistic transform
    import random
    pct = CONFIG.get("transform_percent", 80)
    if random.randint(1, 100) > pct:
        return False

    return True


def should_prefetch(card) -> bool:
    """
    Decide whether a card is eligible for pre-generation.

    Same as should_transform() but WITHOUT the random roll — we always
    prefetch eligible cards.  The random roll at review time decides
    whether to actually show the variant or the original.
    """
    if not CONFIG.get("api_key"):
        return False

    if _is_excluded_note_type(card):
        return False

    # Check deck filter
    active = CONFIG.get("active_decks", [])
    if active:
        deck_name = mw.col.decks.name(card.did)
        if not any(a.lower() in deck_name.lower() for a in active):
            return False

    # Check interval threshold
    min_ivl = CONFIG.get("min_interval_days", 0)
    if card.ivl < min_ivl:
        return False

    return True


# ---------------------------------------------------------------------------
# Core hook: replace question HTML
# ---------------------------------------------------------------------------

def on_card_will_show(text: str, card, kind: str) -> str:
    """Intercept card display. Replace question with variant if eligible."""
    global _current_variant, _current_variant_id, _current_card_id
    global _evaluation_text, _user_response

    if kind.endswith("Question"):
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
            styled_variant += _feedback_buttons_html()
            if CONFIG.get("response_mode") == "freeform":
                styled_variant += _freeform_input_html()
            return styled_variant

        return text

    elif kind.endswith("Answer"):
        if _current_variant and _current_variant_id is not None:
            extra = ""
            if CONFIG.get("response_mode") == "freeform":
                extra = _evaluation_html()
            return extra + _feedback_buttons_html() + text

        return text

    return text


# ---------------------------------------------------------------------------
# Post-show hooks: pre-fetch next card & handle grading
# ---------------------------------------------------------------------------

def on_question_shown(card):
    """After showing question, pre-fetch variant for next card."""
    _prefetch_next_card()


class _GradingWorker(QThread):
    """Background worker for LLM grading so the UI doesn't freeze."""
    done = pyqtSignal(int, str)  # (card_id, evaluation_text)

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
                self.done.emit(self._card_id, result)
        except Exception as e:
            print(f"[Proteus] Grading failed: {e}")


def _cleanup_grading_worker():
    """Disconnect and schedule cleanup of any prior grading worker."""
    global _grading_worker
    if _grading_worker is not None:
        try:
            _grading_worker.done.disconnect()
        except (TypeError, RuntimeError):
            pass
        _grading_worker.deleteLater()
        _grading_worker = None


def _on_grading_done(card_id, evaluation):
    """Callback on main thread when grading finishes. Only inject if card still matches."""
    global _evaluation_text
    if card_id != _current_card_id:
        return  # user advanced past this card — discard stale result
    _evaluation_text = evaluation
    if mw.reviewer and mw.reviewer.web:
        escaped = html.escape(evaluation).replace("\n", "<br>")
        escaped_js = escaped.replace("\\", "\\\\").replace("'", "\\'")
        js = f"""
        (function() {{
            var el = document.getElementById('variant-evaluation');
            if (el) {{
                el.innerHTML = '{escaped_js}';
                el.style.display = 'block';
            }}
        }})();
        """
        mw.reviewer.web.eval(js)


def on_answer_shown(card):
    """After answer shown in freeform mode, trigger async grading."""
    global _grading_worker

    if (CONFIG.get("response_mode") == "freeform"
            and _current_variant
            and _user_response.strip()):

        _cleanup_grading_worker()

        answer_text = _extract_text(card, "answer")
        _grading_worker = _GradingWorker(
            card_id=card.id,
            variant_question=_current_variant,
            user_response=_user_response,
            canonical_answer=answer_text,
            config=CONFIG,
        )
        _grading_worker.done.connect(_on_grading_done)
        _grading_worker.start()


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
            rating = int(message[len("variantFeedback:"):])
            if _current_variant_id and _cache:
                _cache.record_feedback(_current_variant_id, rating)
        except Exception:
            pass
        return (True, None)

    if message == "saveCardIdea":
        _save_current_idea()
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

            _prefetch_worker = PrefetchWorker(
                card_id=next_card.id,
                question=question,
                answer=answer,
                config=CONFIG,
                cache=_cache,
            )
            _prefetch_worker.start()
    except Exception:
        # Pre-fetching is best-effort; never break the review flow
        pass


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
        _batch_manager.cancel()
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


def _feedback_buttons_html() -> str:
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
                pycmd('variantFeedback:1');
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
                pycmd('variantFeedback:-1');
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
                pycmd('saveCardIdea');
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
    """


def _freeform_input_html() -> str:
    """HTML for the freeform text response area."""
    return """
    <div id="variant-response-area" style="
        margin-top: 20px;
        padding: 12px;
        border-top: 1px solid #ddd;
    ">
        <div style="font-size: 0.85em; color: #666; margin-bottom: 6px;">
            Speak or type your response:
        </div>
        <textarea id="variant-response-input"
            rows="4"
            style="
                width: 100%;
                box-sizing: border-box;
                font-size: 1em;
                padding: 8px;
                border: 1px solid #ccc;
                border-radius: 4px;
                resize: vertical;
                font-family: inherit;
            "
            placeholder="Click here, then use Wispr Flow or type..."
            oninput="pycmd('variantResponse:' + this.value)"
        ></textarea>
    </div>
    """


def _evaluation_html() -> str:
    """Placeholder HTML for the LLM evaluation (populated via JS after grading)."""
    return """
    <div id="variant-evaluation" style="
        display: none;
        margin-bottom: 16px;
        padding: 12px;
        background: #f0f7ff;
        border-left: 3px solid #4a90d9;
        border-radius: 4px;
        font-size: 0.95em;
    ">
        Evaluating...
    </div>
    """


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_text(card, side: str) -> str:
    """Extract plain text from a card's question or answer side."""
    if side == "question":
        text = card.question()
    else:
        text = card.answer()

    # Strip HTML tags for LLM consumption
    import re
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ---------------------------------------------------------------------------
# Card ideas
# ---------------------------------------------------------------------------

def _save_current_idea():
    """Save the current variant as a card idea."""
    global _ideas_saved_this_session
    try:
        if not _current_variant or _current_card_id is None or not _cache:
            return

        card = mw.col.get_card(_current_card_id)
        orig_q = _extract_text(card, "question")
        orig_a = _extract_text(card, "answer")

        # Read current rating from variants table if available
        rating = None
        if _current_variant_id is not None:
            with _cache._lock:
                row = _cache._conn.execute(
                    "SELECT rating FROM variants WHERE id = ?",
                    (_current_variant_id,),
                ).fetchone()
                if row:
                    rating = row[0]

        _cache.save_idea(
            card_id=_current_card_id,
            variant_text=_current_variant,
            original_question=orig_q,
            original_answer=orig_a,
            rating=rating,
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

                variant_lbl = QLabel(f"<b>{html.escape(idea['variant_text'])}</b>")
                variant_lbl.setWordWrap(True)
                fl.addWidget(variant_lbl)

                orig_q_lbl = QLabel(
                    f"<span style='color: #888;'>Q: {html.escape(idea['original_question'][:200])}</span>"
                )
                orig_q_lbl.setWordWrap(True)
                fl.addWidget(orig_q_lbl)

                orig_a_lbl = QLabel(
                    f"<span style='color: #888;'>A: {html.escape(idea['original_answer'][:200])}</span>"
                )
                orig_a_lbl.setWordWrap(True)
                fl.addWidget(orig_a_lbl)

                if idea['rating'] is not None:
                    badge = "\U0001f44d" if idea['rating'] > 0 else "\U0001f44e"
                    rating_lbl = QLabel(f"Rating: {badge}")
                    fl.addWidget(rating_lbl)

                btn_row = QHBoxLayout()
                create_btn = QPushButton("Create Card")
                dismiss_btn = QPushButton("Dismiss")

                idea_id = idea['id']

                def make_create(i=idea, iid=idea_id):
                    def on_click():
                        _open_add_note_with_idea(i)
                        _cache.mark_idea_used(iid)
                        refresh()
                    return on_click

                def make_dismiss(iid=idea_id):
                    def on_click():
                        _cache.mark_idea_used(iid)
                        refresh()
                    return on_click

                create_btn.clicked.connect(make_create())
                dismiss_btn.clicked.connect(make_dismiss())
                btn_row.addWidget(create_btn)
                btn_row.addWidget(dismiss_btn)
                fl.addLayout(btn_row)

                frame.setLayout(fl)
                ideas_layout.addWidget(frame)

            ideas_layout.addStretch()

        refresh()
        dlg.exec()
    except Exception as e:
        showInfo(f"Proteus: card ideas dialog error: {e}")


def _open_add_note_with_idea(idea):
    """Open the Add Note dialog pre-filled with an idea's content."""
    try:
        from aqt.addcards import AddCards

        add_dlg = AddCards(mw)
        try:
            note = add_dlg.editor.note
            if note and len(note.fields) >= 2:
                note.fields[0] = idea['variant_text']
                note.fields[1] = idea['original_answer']
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

gui_hooks.profile_did_open.append(_try_init)

# Also try after a short delay in case profile_did_open already fired
from aqt.qt import QTimer
QTimer.singleShot(3000, lambda: _try_init() if mw.col else None)
