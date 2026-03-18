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
After seeing the variant question, you get a text input area with a submit button. Speak your answer using a dictation tool like [Wispr Flow](https://www.wispr.flow/) (or type it), then press Enter or click the submit button. The AI answer target appears instantly (pre-fetched), and feedback fills in when the grading call completes. Then flip to see canonical feedback and the original card.

Toggle between modes anytime with **Ctrl+Shift+V** (Cmd+Shift+V on macOS) or in the config.

## Features

- **Batch prefetching**: Pre-generates variants + AI answer targets for upcoming cards in parallel background threads
- **Variant caching**: SQLite-backed cache stores multiple variants per card, reducing live API calls
- **Configurable variant styles**: 10 research-backed question styles selectable via **Tools → Proteus → Variant Styles...**
- **Personal learning context**: Describe yourself and your current focus to get personally relevant question framing
- **Dual-perspective grading**: Freeform responses evaluated against AI answer target (question page) and canonical answer (answer page). Configurable via `feedback_mode`.
- **Pre-fetched AI answer targets**: Expected answers generated alongside variants — shown instantly on submit, no waiting for grading
- **Card ideas**: Save interesting variant questions as card ideas (bookmark button), review and create cards from them via Card Ideas dialog
- **Quick save**: 💾 button directly saves variant Q+A as a new card. 📋 button opens Add Note pre-filled. ➕ button opens blank Add Note.
- **Human-in-the-loop editing**: In Card Ideas dialog, edit draft wording, apply reason tags, regenerate with targeted instructions
- **Quick master toggle**: `Ctrl+Shift+P` toggles Proteus on/off for the current session
- **Back to question**: `Ctrl+Shift+Left` navigates from answer side back to question side with state preserved
- **Refresh variant cache**: Clear and regenerate all cached variants from the Proteus menu
- **Usage budget**: Set a dollar cap to track API spend

## Variant Styles

Selectable via **Tools → Proteus → Variant Styles...** — pick multiple and each card randomly gets one.

### Core
| Style | What it does | Based on |
|---|---|---|
| **Wozniak + Matuschak** | One concept, unambiguous, retrieval-focused | Minimum information principle |
| **Bloom's Taxonomy** | Cognitive level scales with card maturity (interval) | Bloom's revised taxonomy |
| **Elaborative** | Why/how questions forcing causal reasoning | Elaborative interrogation research |
| **Feynman** | "Explain simply" — clarity over precision | Feynman technique |
| **Cloze Generation** | Fill-in-the-blank producing the key term | Generation effect (desirable difficulties) |

### Contrast & Context
| Style | What it does | Based on |
|---|---|---|
| **Discrimination** | "How does X differ from Y?" with genuinely confusable concepts | Contrast/discrimination learning |
| **Real-World Examples** | Identify the concept from a real named case | Concrete examples principle |

### Transfer
| Style | What it does | Based on |
|---|---|---|
| **Transfer: Code** | Debug, predict, or identify technique in a code snippet | Transfer-appropriate processing |
| **Transfer: Stats** | Interpret model output or diagnose assumptions | Transfer-appropriate processing |
| **Transfer: Math** | Find error or identify technique in an equation | Transfer-appropriate processing |

### Visual
| Style | What it does | Based on |
|---|---|---|
| **Diagram Labeling** | SVG diagram with labeled blanks (A, B, C) to identify | Dual coding theory |

### Per-style length limits
| Style | Max words | Max chars |
|---|---|---|
| Wozniak + Matuschak | 26 | 180 |
| Bloom, Elaborative, Discrimination, Cloze, Transfer | 30 | 210 |
| Real-World | 35 | 250 |
| Feynman | 50 | 350 |

### Bloom's maturity mapping
| Card interval | Cognitive level |
|---|---|
| ≤ 7 days | Remember / Understand |
| ≤ 30 days | Understand / Apply |
| ≤ 90 days | Apply / Analyze |
| > 90 days | Analyze / Evaluate |

## Behavior Decisions

### 1) Master toggles

- **Proteus on/off**: `Ctrl+Shift+P` (Cmd+Shift+P on macOS)
- **Review mode**: `Ctrl+Shift+V` (Cmd+Shift+V on macOS) toggles `flip` vs `freeform`
- **Back to question**: `Ctrl+Shift+Left` (Cmd+Shift+Left on macOS)

### 2) Which cards can get a variant

A card is eligible only if all checks pass:

- `enabled` is `true`
- API key is set
- note type is not excluded (`exclude_note_types`)
- extracted question text length is at least 10 characters
- deck matches `active_decks` (when configured)
- interval meets `min_interval_days`
- random roll passes `transform_percent`

Review-time replacement uses cached variants only (the UI is never blocked by a synchronous API call).

### 3) Feedback modes

`feedback_mode` controls which evaluation perspectives are available:

- `"ai"` — question page only. Response compared against AI answer target.
- `"canonical"` — answer page only. Response compared against canonical flashcard answer.
- `"both"` — both perspectives. One API call returns both.

### 4) Grading semantics

- Coverage donut uses grayscale (dark = covered, light = uncovered)
- If the variant is **misaligned**: `Question drifted from canonical target.`
- AI coverage donut controlled by `show_ai_coverage` config (default: off)

### 5) Variant generation

- All styles enforce short, direct sentences (max 12 words per sentence)
- No preamble, no subordinate clauses, no filler words
- Visual styles (diagram, transfer) can opt out if the concept isn't suited — falls back to text

### 6) Card creation buttons

- 🔖 Save card idea (for later review in Card Ideas dialog)
- ➕ Add new card (blank Add Note dialog)
- 💾 Quick save (directly saves variant Q + AI answer as new card)
- 📋 Create from variant (pre-filled Add Note dialog for editing)

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
   - Set `"api_key"` to your Anthropic API key

## Configuration

Edit via **Tools → Add-ons → Config** or directly in `config.json`:

```json
{
    "enabled": true,
    "api_key": "",
    "model": "claude-sonnet-4-20250514",
    "response_mode": "flip",
    "active_decks": [],
    "transform_percent": 80,
    "min_interval_days": 0,
    "max_cached_variants": 3,
    "system_prompt": "",
    "learner_context": "",
    "exclude_note_types": ["Image Occlusion"],
    "variant_style": ["wozniak_matuschak"],
    "batch_prefetch_count": 15,
    "batch_prefetch_concurrency": 3,
    "show_prefetch_progress": true,
    "debug_logging": false,
    "usage_budget": 5.00,
    "grading_model": "",
    "grading_max_tokens": 280,
    "grading_timeout_s": 10,
    "feedback_mode": "both",
    "show_ai_coverage": false
}
```

### Key settings

**`variant_style`**: List of styles to sample from. Each card gets a randomly selected style. Set via **Tools → Proteus → Variant Styles...** or in config. Valid values: `wozniak_matuschak`, `bloom`, `elaborative`, `feynman`, `cloze_generation`, `discrimination`, `real_world`, `transfer_code`, `transfer_stats`, `transfer_math`, `diagram_labeling`.

**`learner_context`**: Personal context injected into all variant prompts. Describe yourself and what's currently relevant. Set via the Variant Styles dialog.

**`feedback_mode`**: Controls grading perspectives. `"both"` (default), `"ai"`, or `"canonical"`.

**`show_ai_coverage`**: Show AI coverage donut on question side. Default: `false`.

**`active_decks`**: Filter which decks get variants. Supports partial matching. Leave empty for all decks.

**`transform_percent`**: Set below 100 to occasionally see original questions. 80 = ~1 in 5 reviews shows the original.

**`min_interval_days`**: Only transform cards above this interval. Set to 7 or 14 for mature cards only.

**`system_prompt`**: Domain-specific context for variant quality.

**`grading_model`**: Optional faster/cheaper model for grading only.

**`grading_max_tokens`**: Max output tokens for grading. Default: 280.

**`debug_logging`**: Write diagnostic logs to `proteus_diag.log`.

## Coverage & Gaps Output Schema

**Canonical perspective** (answer page):
- `alignment`: `aligned | partial | misaligned`
- `canonical_points`, `covered_points`, `missed_points`, `coverage_pct`

**AI answer perspective** (question page, `"ai"` or `"both"` mode):
- `expected_answer`, `ai_covered_points`, `ai_missed_points`, `ai_coverage_pct`

**Shared**: `learning_feedback`, `incorrect`, `overall`

## Cost

- **Flip mode**: ~1 API call per transformed review (cached variants reduce this)
- **Freeform mode**: ~1 additional API call per review for grading
- Text variant: ~$0.003/variant. Visual/transfer: ~$0.006-0.010/variant (more output tokens)
- Freeform grading: ~$0.006/call (`"ai"` or `"canonical"`), ~$0.009/call (`"both"`)
- 50 reviews/day ≈ $0.12–0.25/day flip, $0.30–0.50/day freeform

## Keyboard Shortcuts

- **Ctrl+Shift+P**: Toggle Proteus on/off (`Cmd+Shift+P` on macOS)
- **Ctrl+Shift+V**: Toggle flip/freeform mode (`Cmd+Shift+V` on macOS)
- **Ctrl+Shift+Left**: Back to question from answer side (`Cmd+Shift+Left` on macOS)
- **Enter** / submit button (in freeform): Submit response and start grading

## Tools Menu

```
Tools → Proteus →
    Card Ideas
    Usage Stats
    Variant Styles...
    Refresh Variant Cache
    ─────────────
    Toggle On/Off         Ctrl+Shift+P
    Toggle Flip/Freeform  Ctrl+Shift+V
    Back to Question      Ctrl+Shift+Left
```

## Files

```
anki_proteus/
├── __init__.py         # Hooks, UI injection, review flow
├── generator.py        # LLM API calls, variant styles, grading
├── cache.py            # SQLite cache (schema v9) for variants + card ideas
├── prefetch.py         # Single-card background pre-fetching thread
├── batch_prefetch.py   # Parallel batch pre-generation at session start
├── config.json         # Default configuration
├── manifest.json       # Anki addon metadata
├── tests/test_core.py  # Unit tests (55 tests)
└── README.md
```

## Development

Symlink the repo into your addons folder:

```bash
ln -s /path/to/anki-proteus ~/Library/Application\ Support/Anki2/addons21/anki_proteus
```

Changes take effect on Anki restart. Run tests:

```bash
pytest -v
```

## Future Directions

- **Web search for discrimination**: Search for commonly confused concepts to build better contrasts
- **Chrome extension**: Generate flashcards from web page content using the same prompt engineering
- **Visual styles**: Error detection, flowchart completion, graph interpretation
- **Keyword similarity**: Client-side sanity-check on LLM coverage verdicts
- **Pretesting**: Show variants before first review (productive failure)
