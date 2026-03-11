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

from .generator import generate_variant, grade_response
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
_current_card_id: int = None
_user_response: str = ""              # captured from freeform text input
_evaluation_text: str = None          # LLM grading result

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


def toggle_response_mode():
    """Toggle between flip and freeform mode mid-session."""
    global CONFIG
    if CONFIG["response_mode"] == "flip":
        CONFIG["response_mode"] = "freeform"
        tooltip("Proteus: freeform mode (speak/type responses)")
    else:
        CONFIG["response_mode"] = "flip"
        tooltip("Proteus: flip mode (standard review)")

    # Re-render the current question so the input box appears/disappears
    if _current_variant and mw.reviewer:
        mw.reviewer._showQuestion()


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
    global _current_variant, _current_card_id, _evaluation_text, _user_response

    if kind.endswith("Question"):
        _evaluation_text = None
        _current_variant = None
        _user_response = ""
        _current_card_id = card.id

        if not should_transform(card):
            return text

        # Try cache first, then generate synchronously as fallback
        question_text = _extract_text(card, "question")
        answer_text = _extract_text(card, "answer")

        variant = _cache.get_variant(card.id)
        if not variant:
            variant = generate_variant(
                question=question_text,
                answer=answer_text,
                config=CONFIG,
            )
            if variant:
                _cache.store_variant(card.id, variant)
            else:
                tooltip("Proteus: variant generation failed — check API key and debug console")

        if variant:
            _current_variant = variant
            styled_variant = _wrap_variant_html(variant)
            if CONFIG.get("response_mode") == "freeform":
                styled_variant += _freeform_input_html()
            return styled_variant

        return text

    elif kind.endswith("Answer"):
        if _current_variant and CONFIG.get("response_mode") == "freeform":
            eval_html = _evaluation_html()
            return eval_html + text

        return text

    return text


# ---------------------------------------------------------------------------
# Post-show hooks: pre-fetch next card & handle grading
# ---------------------------------------------------------------------------

def on_question_shown(card):
    """After showing question, pre-fetch variant for next card."""
    _prefetch_next_card()


def on_answer_shown(card):
    """After answer shown in freeform mode, trigger grading."""
    global _evaluation_text

    if (CONFIG.get("response_mode") == "freeform"
            and _current_variant
            and _user_response.strip()):

        answer_text = _extract_text(card, "answer")
        _evaluation_text = grade_response(
            variant_question=_current_variant,
            user_response=_user_response,
            canonical_answer=answer_text,
            config=CONFIG,
        )

        # Inject evaluation into webview
        if _evaluation_text and mw.reviewer and mw.reviewer.web:
            escaped = html.escape(_evaluation_text).replace("\n", "<br>")
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


# ---------------------------------------------------------------------------
# JS bridge: capture freeform text input
# ---------------------------------------------------------------------------

def on_js_message(handled: tuple, message: str, context):
    """Receive messages from injected JavaScript."""
    global _user_response

    if message.startswith("variantResponse:"):
        _user_response = message[len("variantResponse:"):]
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
    _log(f"state_did_change: {old_state} -> {new_state}")
    if new_state == "review":
        _start_batch_prefetch()
    elif old_state == "review":
        _cancel_batch_prefetch()


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
    _initialized = True
    try:
        init_addon()
    except Exception as e:
        showInfo(f"Proteus: init failed: {e}")

gui_hooks.profile_did_open.append(_try_init)

# Also try after a short delay in case profile_did_open already fired
from aqt.qt import QTimer
QTimer.singleShot(3000, lambda: _try_init() if mw.col else None)
