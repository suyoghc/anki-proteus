# Anki Proteus

An Anki addon that uses LLMs to generate novel question variants at review time, so you're testing understanding rather than pattern-matching card shapes.

## How it works

When a card comes up for review, the addon:

1. Sends the original question + answer to the Anthropic API
2. Gets back a variant question that tests the **same concept** differently
3. Displays the variant instead of the original question
4. You flip and see the **original answer**, then grade normally

Anki's scheduler is completely untouched — FSRS/SM-2 works as usual. The variant is ephemeral; only the original card gets scored.

## Two Review Modes

### Flip mode (default)
Standard Anki flow with transformed questions. Fast, zero friction.

### Freeform mode
After seeing the variant question, you get a text input area. Speak your answer using a dictation tool like [Wispr Flow](https://www.wispr.flow/) (or type it), then flip. The LLM evaluates your response against the canonical answer and shows brief feedback alongside the original answer.

Toggle between modes anytime with **Ctrl+Shift+V** or in the config.

## Compatibility

Tested on Anki 25.02+ (Python 3.9, Qt 6). Uses `gui_hooks.card_will_show` with context strings (`reviewQuestion`, `reviewAnswer`).

## Installation

1. Find your Anki addons folder:
   - **Tools → Add-ons → Open Add-ons Folder** in Anki
   - Or navigate to `~/.local/share/Anki2/addons21/` (Linux), `~/Library/Application Support/Anki2/addons21/` (Mac), `%APPDATA%\Anki2\addons21\` (Windows)

2. Copy the `anki_proteus` folder into the addons folder (or symlink it for development)

3. Restart Anki

4. Configure your API key:
   - **Tools → Add-ons** → select "Proteus" → **Config**
   - Set `"api_key"` to your Anthropic API key (must be in double quotes)

## Configuration

Edit via **Tools → Add-ons → Config** or directly in `config.json`:

```json
{
    "api_key": "",
    "model": "claude-sonnet-4-20250514",
    "response_mode": "flip",
    "active_decks": [],
    "transform_percent": 80,
    "min_interval_days": 0,
    "max_cached_variants": 3,
    "system_prompt": ""
}
```

### Key settings

**`active_decks`**: Filter which decks get variants. Supports partial matching — `["Immunology"]` will match any deck with "Immunology" in the name. Leave empty for all decks.

**`transform_percent`**: Set below 100 to occasionally see original questions as a sanity check. 80 means ~1 in 5 reviews shows the original.

**`min_interval_days`**: Pattern-matching risk is highest on mature cards. Set to 7 or 14 to only generate variants for cards you've already reviewed several times.

**`system_prompt`**: Domain-specific context that improves variant quality. Examples:

```
"The learner is a medical student. Frame scenarios using
clinical vignettes and patient presentations."
```

```
"The learner is studying introductory chemistry. Use
everyday examples and avoid jargon beyond the course level."
```

## Cost

- **Flip mode**: ~1 API call per transformed review (cached variants reduce this)
- **Freeform mode**: ~1 additional API call per review for grading
- Using `claude-sonnet-4-20250514` at ~300 tokens per variant ≈ $0.003/variant
- 50 reviews/day ≈ $0.12–0.25/day depending on mode

Pre-fetching and caching minimize live API calls during review.

## Keyboard Shortcuts

- **Ctrl+Shift+V**: Toggle between flip and freeform mode mid-session

## Files

```
anki_proteus/
├── __init__.py       # Hooks, UI injection, review flow
├── generator.py      # LLM API calls (variant generation + grading)
├── cache.py          # SQLite cache for pre-generated variants
├── prefetch.py       # Background pre-fetching thread
├── config.json       # Default configuration
├── manifest.json     # Anki addon metadata
└── README.md
```

## Development

Initial prototype generated with Claude, iterated with Claude Code.

For development, symlink the repo into your addons folder:

```bash
ln -s /path/to/anki-proteus ~/Library/Application\ Support/Anki2/addons21/anki_proteus
```

Changes take effect on Anki restart. The addon initializes via `profile_did_open` hook with a QTimer fallback for single-profile setups.

## Future Directions

These aren't built yet but the architecture supports them:

- **Concept grouping**: Tag cards with a concept ID to avoid generating variants that overlap with sibling cards
- **Knowledge graph**: Prerequisite-aware variant generation and diagnostic backtracking on failures
- **Transformation taxonomy**: Weighted random selection of variant types (rephrase, apply-to-scenario, find-the-error, explain-to-a-student)
- **Difficulty scaling**: Tie variant type to card maturity — rephrases for new cards, application scenarios for mature ones
- **Variant quality feedback**: Flag bad variants to improve prompts over time
