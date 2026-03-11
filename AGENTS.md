# AGENTS.md — Anki Proteus

## Project
Anki addon that generates LLM-powered question variants at review time. Uses Anthropic API.

## Critical Anki 25.x Constraints
- **Python 3.9** — use `Optional[str]`, NOT `str | None`
- **Hook contexts** — `card_will_show` passes `"reviewQuestion"` / `"reviewAnswer"`, use `.endswith("Question")` / `.endswith("Answer")`
- **JS bridge** — `webview_did_receive_js_message` handlers must return `(True, None)` (2-tuple), not `(True,)`
- **ConfigEditor** — `ConfigEditor(mw, addon_module)` with try/except fallback
- **Collection access** — `card.question()`, `card.answer()`, `mw.col.get_card()` must run on main thread
- **Bootstrap** — use `gui_hooks.profile_did_open` + `QTimer.singleShot(3000, ...)` fallback for single-profile setups

## Architecture
- `__init__.py` — hooks, UI injection, review flow
- `generator.py` — Anthropic API calls (stateless, thread-safe)
- `cache.py` — thread-safe SQLite cache (`check_same_thread=False` + `threading.Lock`)
- `prefetch.py` — single-card background QThread prefetch
- `batch_prefetch.py` — batch session prefetch (queue-based worker pool)

## Testing
- No automated tests; verify manually in Anki after changes
- Always run `python3 -m compileall -q .` before committing
- Known working baseline: commit `b80be5e`

## Commits
- No Codex co-author line
- Commit and push only when explicitly asked
